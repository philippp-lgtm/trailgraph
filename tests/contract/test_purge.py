"""``graph-admin graph purge`` on sealed graphs (plan T16, step 6) through the admin service.

A purge retracts the entities with empty values, shreds the keys of their
revisions, of the txs that wrote them and (with files) of their stored files,
and deletes those files. Afterwards no value can be read from the data root,
from a copy of it taken before the purge, or from the key store, while every
other entity stays readable and ``audit verify`` stays clean.
"""

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.admin_context import admin_context, key_settings
from support.services import ServiceEnv

from graph_cli.catalog.file_copies import FileCopyDir
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, Uuid7Provider
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest
from graph_cli.storage.files.keyrings import KeyRings
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli_admin.services_admin import audit
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.purge import purge

LABEL, REASON, QUALIFIER = "LABELMARK71c4", "REASONMARK2e9d", "QUALMARK5a0b"
FILE_MARKER = b"FILEMARK8f3e"
CONTENT = b"%PDF-1.4\n% synthetic " + FILE_MARKER + b"\n%%EOF\n"
TOPIC, NOTE = "nas:Topic", "nas:Note"
IWE = "integrity.audit.intent_without_effect"


@pytest.fixture
def svc(tmp_path: Path) -> ServiceEnv:
    return ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "root", version="1.1.0")


@pytest.fixture
def ctx(svc: ServiceEnv) -> AdminContext:
    return admin_context(svc.root)


def topic(svc: ServiceEnv, label: str, **kw: Any) -> EntityId:
    result = svc.apply({"op": "create", "type": TOPIC, "attrs": {"label": label}}, **kw)
    entity = result.entities[0].id
    assert entity is not None
    return entity


def add_file(svc: ServiceEnv, tmp_path: Path, content: bytes = CONTENT) -> EntityId:
    path = tmp_path / f"input-{len(list(tmp_path.glob('input-*')))}.pdf"
    path.write_bytes(content)
    request = RequestMeta.of("file add", {"args": {"path": path.name}})
    result = svc.make_files().add(FileAddRequest(svc.actor, svc.name, path, request))
    return EntityId(UUID(str(result.entity["id"])))


def run(ctx: AdminContext, *refs: Any, **kw: Any) -> dict[str, Any]:
    kw.setdefault("with_files", False)
    kw.setdefault("reason", "ticket 42")
    kw.setdefault("dry_run", False)
    outcome = purge(ctx, graph_ref="example_graph", refs=[str(r) for r in refs], **kw)
    return outcome.data


def verify_codes(ctx: AdminContext) -> list[str]:
    verification = audit.verify(ctx)
    verification.raise_on_errors()
    return sorted(f.code for f in verification.findings)


def get(svc: ServiceEnv, entity: EntityId, **kw: Any) -> dict[str, Any]:
    reader = svc.make_reader()
    view = reader.open(svc.actor, svc.entry, "read.get", RequestMeta.of("get", {}))
    data, _ = reader.get(view, str(entity), **kw)
    return data


def history(svc: ServiceEnv, entity: EntityId) -> list[Any]:
    reader = svc.make_reader()
    view = reader.open(svc.actor, svc.entry, "read.history", RequestMeta.of("history", {}))
    data, _, _ = reader.history(view, svc.actor, str(entity), limit=10, offset=0)
    return list(data["items"])


def test_a_purged_entity_reads_shredded_and_others_stay(svc: ServiceEnv, ctx: AdminContext) -> None:
    gone = topic(svc, LABEL, reason=REASON)
    kept = topic(svc, "kept topic")
    svc.apply({"op": "update", "id": str(gone), "set": {"label": LABEL + " again"}})
    data = run(ctx, gone)
    assert data["purged"] and data["retracted"] == [str(gone)]
    assert data["keys"] == {"entity": 1, "tx": 3, "file": 0, "pending": 4}
    entity = get(svc, gone)["entity"]
    assert entity["shredded"] is True and entity["attrs"] == {}
    assert entity["record_status"] == "retracted" and entity["rev"] == 3
    assert get(svc, gone, rev=1)["entity"]["shredded"] is True
    items = history(svc, gone)
    assert [item.get("shredded") for item in items] == [True, True, True]
    assert {item["reason"] for item in items} == {None}
    assert items[0]["command"] == "graph purge"
    assert "shredded" not in get(svc, kept)["entity"]
    assert get(svc, kept)["entity"]["attrs"] == {"label": "kept topic"}
    assert verify_codes(ctx) == []
    (event,) = [e for e in svc.graph_events() if e["actions"] == ["maint.purge"]]
    assert event["change"]["purge"]["entities"] == [str(gone)]
    assert event["reason"] == "ticket 42"  # maintenance reasons stay readable
    # nothing left to do: no event, no tx
    seq, events = svc.head_seq(), len(svc.graph_events())
    again = run(ctx, gone)
    assert not again["purged"] and again["keys"]["pending"] == 0
    assert (svc.head_seq(), len(svc.graph_events())) == (seq, events)


def test_an_entity_in_a_shared_tx_leaves_the_other_readable(
    svc: ServiceEnv, ctx: AdminContext
) -> None:
    result = svc.apply(
        {"op": "create", "type": TOPIC, "local": "a", "attrs": {"label": LABEL}},
        {"op": "create", "type": TOPIC, "local": "b", "attrs": {"label": "neighbour"}},
    )
    gone, kept = (e.id for e in result.entities)
    assert gone is not None and kept is not None
    run(ctx, gone)
    assert get(svc, kept)["entity"]["attrs"] == {"label": "neighbour"}
    assert "shredded" not in get(svc, kept)["entity"]
    # the shared tx's change set went with the purge; the neighbour's revision did not
    assert svc.txs()[0].shredded
    assert verify_codes(ctx) == []


def test_edges_to_a_purged_entity_stay_without_values(svc: ServiceEnv, ctx: AdminContext) -> None:
    source_type = svc.factory.concrete("nas:Source")
    created = svc.apply(*svc.factory.create(NOTE, "n"), *svc.factory.create(source_type, "s"))
    note = next(e.id for e in created.entities if e.type == NOTE)
    source = next(e.id for e in created.entities if e.type == source_type)
    assert note is not None and source is not None
    svc.apply(
        {
            "op": "link",
            "id": str(note),
            "rel": "nas:cites",
            "target": str(source),
            "qualifiers": {"locator": QUALIFIER},
        }
    )
    run(ctx, note)
    entity = get(svc, note)["entity"]
    assert str(source) in [edge["target"] for edge in entity["edges"]]
    assert all("qualifiers" not in edge for edge in entity["edges"])
    assert get(svc, source)["entity"].get("shredded") is None
    assert verify_codes(ctx) == []


def test_with_files_deletes_the_file_and_nothing_survives_a_copy(
    svc: ServiceEnv, ctx: AdminContext, tmp_path: Path
) -> None:
    gone = topic(svc, LABEL, reason=REASON)
    file = add_file(svc, tmp_path)
    other = add_file(svc, tmp_path, b"another synthetic document\n")
    (stored,) = svc.txs()[1].blobs
    copies = FileCopyDir(ctx.roots.state_root / "files", ctx.clock)
    current = svc.snapshot().get(file)
    assert current is not None
    copy = copies.root / str(svc.entry.id) / str(current.attrs["content"]).removeprefix("sha256:")
    before = tmp_path / "before"
    shutil.copytree(svc.root, before)
    copy.parent.mkdir(parents=True)  # a plaintext copy ``file path`` left on this machine
    copy.write_bytes(CONTENT)

    data = run(ctx, gone, file, with_files=True)
    assert [b["hash"] for b in data["blobs_removed"]] == [stored]
    assert data["keys"]["file"] == 1 and data["copies_removed"] == 1
    assert not copy.exists()
    assert svc.store.blob(stored) is None
    assert b"".join(svc.store.read_blob(svc.txs()[2].blobs[0])).startswith(b"another")
    assert get(svc, other)["entity"].get("shredded") is None
    assert verify_codes(ctx) == []

    markers = (LABEL.encode(), REASON.encode(), FILE_MARKER)
    keys = key_settings(svc.root).dir
    for root in (svc.root, before, keys):
        for path in root.rglob("*"):
            if path.is_file():
                assert not any(m in path.read_bytes() for m in markers), path
    # the data root as it was before the purge, opened with the key store as it is now
    clock = SystemClock()
    rings = KeyRings(key_settings(svc.root), clock=clock, ids=Uuid7Provider(clock))
    old = FileStoreMaintenance(before, rings=rings).store(svc.entry.id)
    snapshot = old.snapshot()
    for entity in (gone, file):
        revision = snapshot.get(entity)
        assert revision is not None and revision.shredded and not revision.attrs
    assert all(tx.shredded for tx in old.changes_since(0)[:2])
    with pytest.raises(GraphCliError) as caught:
        b"".join(old.read_blob(stored))
    assert caught.value.details["reason"] == "shredded"


def test_without_files_the_file_stays(svc: ServiceEnv, ctx: AdminContext, tmp_path: Path) -> None:
    file = add_file(svc, tmp_path)
    (stored,) = svc.txs()[0].blobs
    data = run(ctx, file)
    assert data["blobs_removed"] == [] and data["keys"]["file"] == 0
    assert b"".join(svc.store.read_blob(stored)) == CONTENT  # its key lives
    assert verify_codes(ctx) == []
    # but no reader can tell which file was the entity's
    reader = svc.make_reader()
    service = svc.make_files(reader=reader, copies=FileCopyDir(tmp_path / "copies", ctx.clock))
    request = RequestMeta.of("file path", {"args": {}})
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    with pytest.raises(GraphCliError) as caught:
        service.path(view, str(file), origin=False, actor=svc.actor, request=request)
    assert caught.value.code == "file.erased"
    # its revisions read empty now, but the txs that wrote them still list the file
    data = run(ctx, file, with_files=True)
    assert [b["hash"] for b in data["blobs_removed"]] == [stored]
    assert data["retracted"] == [] and data["keys"]["pending"] == 1
    assert svc.store.blob(stored) is None
    assert verify_codes(ctx) == []


def test_a_dry_run_writes_nothing(svc: ServiceEnv, ctx: AdminContext) -> None:
    gone = topic(svc, LABEL)
    events = len(svc.graph_events())
    data = run(ctx, gone, dry_run=True)
    assert data["dry_run"] and not data["purged"] and data["retracted"] == [str(gone)]
    assert data["keys"] == {"entity": 1, "tx": 1, "file": 0, "pending": 2}
    assert (svc.head_seq(), len(svc.graph_events())) == (1, events)
    assert get(svc, gone)["entity"]["attrs"] == {"label": LABEL}


def test_refusals(tmp_path: Path) -> None:
    plain = ServiceEnv("files", "notes_and_sources", tmp_path / "plain")
    entity = topic(plain, LABEL)
    with pytest.raises(GraphCliError) as caught:
        run(admin_context(plain.root), entity)
    assert caught.value.code == "admin.graph_not_sealed"
    sealed = ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "sealed")
    ctx = admin_context(sealed.root)
    with pytest.raises(GraphCliError) as caught:
        run(ctx, "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9")
    assert caught.value.code == "entity.not_found"
    assert not any(e["actions"] == ["maint.purge"] for e in sealed.graph_events())


def test_writes_after_a_purge(svc: ServiceEnv, ctx: AdminContext) -> None:
    gone = topic(svc, LABEL, idempotency_key="k-1")
    run(ctx, gone)
    # a retry of the purged write matches nothing any more
    error = svc.refuse(
        {"op": "create", "type": TOPIC, "attrs": {"label": LABEL}}, idempotency_key="k-1"
    )
    assert error.code == "conflict.idempotency_key_reuse"
    # a purged entity is retracted: nothing changes it again
    error = svc.refuse({"op": "update", "id": str(gone), "set": {"label": "back"}})
    findings: Any = error.details["findings"]
    assert [f["code"] for f in findings] == ["data.temporal.record_not_active"]
    # its id stays a valid edge target
    other = topic(svc, "other")
    svc.apply({"op": "link", "id": str(other), "rel": "nas:broader", "target": str(gone)})
    assert verify_codes(ctx) == []
