"""Sealed graphs (store format 2, plan T16) through the services, on both backends.

The rest of the contract suite runs on sealed backends too; these tests cover
what only a sealed graph does: no value in any file of the data root, keyed
hashes in the audit stream, refusals that keep nothing of the request,
idempotent retries under the previous tx's key, and shreds seen by readers.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from support.admin_context import key_rings, key_settings
from support.services import ServiceEnv

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.changeset import changeset_hash, parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, parse_id
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.kernel.records import RequestMeta, revision_content_hash

MARKERS = ("LABELMARK3c9e", "QUALMARK8d2a", "REASONMARK1f7b", "NEWLABELMARK6e0c")
TOPIC, NOTE, SOURCE = "nas:Topic", "nas:Note", "nas:Source"


@pytest.fixture(params=("memory-sealed", "files-sealed"))
def svc(request: pytest.FixtureRequest, tmp_path: Path) -> ServiceEnv:
    return ServiceEnv(str(request.param), "notes_and_sources", tmp_path / "root")


@pytest.fixture
def files(tmp_path: Path) -> ServiceEnv:
    return ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "root")


def topic(svc: ServiceEnv, label: str, **kw: Any) -> EntityId:
    result = svc.apply({"op": "create", "type": TOPIC, "attrs": {"label": label}}, **kw)
    entity = result.entities[0].id
    assert entity is not None
    return entity


def graph_events(svc: ServiceEnv) -> list[Any]:
    return list(svc.audit.read(graph_stream(str(svc.entry.id))))


def test_no_value_reaches_a_file_of_the_data_root(files: ServiceEnv) -> None:
    label, locator, reason, new_label = MARKERS
    subject = topic(files, label, reason=reason)
    source_type = files.factory.concrete(SOURCE)
    created = files.apply(*files.factory.create(NOTE, "n"), *files.factory.create(source_type, "s"))
    note = next(e.id for e in created.entities if e.type == NOTE)
    source = next(e.id for e in created.entities if e.type == source_type)
    files.apply(
        {
            "op": "link",
            "id": str(note),
            "rel": "nas:cites",
            "target": str(source),
            "qualifiers": {"locator": locator},
        },
        reason=reason,
    )
    files.apply({"op": "update", "id": str(subject), "set": {"label": new_label}})
    stored = {p: p.read_bytes() for p in files.root.rglob("*") if p.is_file()}
    assert any("index.sqlite" in p.name for p in stored)  # the index is among them
    for path, data in stored.items():
        for marker in MARKERS:
            assert marker.encode() not in data, (path, marker)
    for path in key_settings(files.root).dir.rglob("*"):
        if path.is_file():
            assert not any(m.encode() in path.read_bytes() for m in MARKERS), path
    # positive controls: a reader with the keys sees every value
    snapshot = files.snapshot()
    current, first = snapshot.get(subject), snapshot.get_revision(subject, 1)
    assert current is not None and current.attrs["label"] == new_label
    assert first is not None and first.attrs["label"] == label
    assert assert_cited(snapshot.incoming(source), note) == {"locator": locator}
    assert files.txs()[0].reason == reason


def assert_cited(incoming: Any, note: EntityId | None) -> Any:
    (edge,) = [edge for found, edge in incoming if found == note and edge.rel == "nas:cites"]
    return dict(edge.qualifiers)


def test_write_intents_carry_keyed_hashes_and_no_reason(svc: ServiceEnv) -> None:
    subject = topic(svc, "sealed topic", reason="why it was written")
    (tx,) = svc.txs()
    intent = graph_events(svc)[-1]
    assert "reason" not in intent
    assert tx.reason == "why it was written"  # sealed in the tx instead
    change = intent["change"]
    assert change["changeset_hash"] == tx.changeset_hash
    cs = parse_changeset(dict(tx.changeset))
    assert tx.changeset_hash != changeset_hash(cs)  # not the plain hash
    plain = RequestMeta.of("apply", {"ops": 1})
    assert intent["request"]["args_digest"] == tx.request.args_digest != plain.args_digest
    (revision,) = tx.revisions
    assert revision.id == subject
    assert change["entities"][0]["after"] == revision.content_hash
    assert revision.content_hash != revision_content_hash(revision)
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]


def test_a_refusal_keeps_nothing_the_request_carried(svc: ServiceEnv) -> None:
    for _ in range(2):
        error = svc.refuse({"op": "create", "type": TOPIC, "attrs": {}}, reason="a reason")
        assert error.code == "write.invalid"
    first, second = graph_events(svc)[-2:]
    for event in (first, second):
        assert event["result"]["status"] == "invalid"
        assert "reason" not in event
        assert event["request"]["args_digest"] != RequestMeta.of("apply", {"ops": 1}).args_digest
    assert first["request"]["args_digest"] != second["request"]["args_digest"]  # one-time keys


def test_idempotent_retries_compare_under_the_previous_tx_key(svc: ServiceEnv) -> None:
    op = {"op": "create", "type": TOPIC, "attrs": {"label": "once"}}
    first = svc.apply(op, idempotency_key="k-1")
    again = svc.apply(op, idempotency_key="k-1")
    assert again.idempotent_replay and again.seq == first.seq
    other = svc.refuse({**op, "attrs": {"label": "twice"}}, idempotency_key="k-1")
    assert other.code == "conflict.idempotency_key_reuse"
    (tx,) = svc.txs()
    assert tx.sealed is not None
    svc.store.key_ring().shred([tx.sealed.kid], EventId(parse_id(str(svc.ids.new_id()))))
    purged = svc.refuse(op, idempotency_key="k-1")  # nothing left to compare with
    assert purged.code == "conflict.idempotency_key_reuse"


def test_a_dry_run_creates_no_key(files: ServiceEnv) -> None:
    topic(files, "existing")
    path = key_settings(files.root).store_path(files.entry.id)

    def keys() -> int:
        with sqlite3.connect(path) as db:
            return int(db.execute("SELECT count(*) FROM dek").fetchone()[0])

    before = keys()
    result = files.apply({"op": "create", "type": TOPIC, "attrs": {"label": "x"}}, dry_run=True)
    assert result.dry_run and keys() == before
    files.apply({"op": "create", "type": TOPIC, "attrs": {"label": "x"}})
    assert keys() == before + 2  # the entity's key and the tx's key


def test_a_shred_by_another_process_is_seen_by_the_next_read(files: ServiceEnv) -> None:
    subject = topic(files, "to be purged")
    other = FileCatalog(files.root, rings=key_rings(files.root))  # another process's view
    reader = other.store_for(files.entry.id)[0]
    found = reader.snapshot().get(subject)
    assert found is not None and found.attrs["label"] == "to be purged"
    assert found.sealed is not None
    files.store.key_ring().shred([found.sealed.kid], EventId(parse_id(str(files.ids.new_id()))))
    gone = reader.snapshot().get(subject)
    assert gone is not None and gone.shredded and gone.attrs == {}
    assert gone.alias == found.alias and gone.record_status == "active"


def test_a_sealed_graph_needs_its_key_store(files: ServiceEnv) -> None:
    topic(files, "kept")
    key_settings(files.root).store_path(files.entry.id).rename(files.root.parent / "moved")
    with pytest.raises(GraphCliError) as info:
        FileCatalog(files.root, rings=key_rings(files.root)).store_for(files.entry.id)
    assert info.value.code == "config.invalid"
    with pytest.raises(GraphCliError) as info:
        FileCatalog(files.root).store_for(files.entry.id)
    assert info.value.code == "config.invalid"


def test_a_long_lived_store_drops_what_it_opened_once_a_key_is_shredded(svc: ServiceEnv) -> None:
    """A server keeps one store: its replay cache must not keep serving purged values."""
    subject = topic(svc, "cached value")
    store: Any = svc.store
    seq = store.head().seq

    def cached() -> Any:  # the replay view a store keeps between reads
        return store.view_at(seq).current(subject) if hasattr(store, "view_at") else None

    before = store.snapshot().get(subject)
    assert before is not None and before.sealed is not None
    assert cached() is None or cached().attrs["label"] == "cached value"
    store.key_ring().shred([before.sealed.kid], EventId(parse_id(str(svc.ids.new_id()))))
    after = store.snapshot().get(subject)  # a read begins: the store refreshes its keys
    assert after is not None and after.shredded and after.attrs == {}
    assert cached() is None or cached().shredded
