"""T: one tamper scenario per integrity.storage.* code; reindex rebuilds an identical tree.

Contracts B4.1, B13.1, R.6, section 4.2; plan D9, D10.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.admin_context import admin_context, key_rings, key_settings
from support.drafts import intent
from support.graphs import publish_ontology
from support.services import ServiceEnv
from support.stores import StoreEnv, snapshot_state
from support.trees import tree_hash

from graph_cli.audit.verify import purges
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId, GraphId
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.services import write as write_service
from graph_cli.services.files import FileAddRequest
from graph_cli.storage import finding_codes
from graph_cli.storage.files.index import IndexReader
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore
from graph_cli_admin.services_admin.purge import purge

CONTENT = b"synthetic stored bytes\n"


def setup(tmp_path: Path) -> tuple[StoreEnv, FileStore, FileStoreMaintenance]:
    env = StoreEnv("files", "inventory", tmp_path, seed=5)
    env.random(12)
    env.write_blobs(lambda d: d.create(), [CONTENT])
    assert isinstance(env.store, FileStore)
    publish_ontology(env.store.layout.root, env.graph_id, "inventory", env.syncer)
    return env, env.store, FileStoreMaintenance(tmp_path, syncer=env.syncer)


def verify(maintenance: FileStoreMaintenance, graph: GraphId) -> list[str]:
    """Storage ``verify`` plus the full blob check, each code once."""
    with maintenance.maintenance_session(graph, timeout_s=5) as session:
        codes = [f.code for f in session.verify()]
        extra = [f.code for f in session.verify_blobs(rehash=True)]
    return [*codes, *(c for c in dict.fromkeys(extra) if c not in codes)]


def stored_blob(store: FileStore) -> Path:
    (path,) = store.layout.blobs.glob("sha256/*/*")
    return path


def delete_blob(store: FileStore) -> None:
    stored_blob(store).unlink()


def flip_blob_byte(store: FileStore) -> None:
    path = stored_blob(store)
    path.chmod(0o644)
    data = bytearray(path.read_bytes())
    data[0] ^= 0x01
    path.write_bytes(bytes(data))


def add_orphan_blob(store: FileStore) -> None:
    data = b"bytes no tx names\n"
    path = store.layout.blob_path("sha256:" + hashlib.sha256(data).hexdigest())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def add_staging_leftover(store: FileStore) -> None:
    (store.layout.blobs / ".tmp-leftover").write_bytes(CONTENT)


def revision_files(store: FileStore) -> list[Path]:
    return sorted(store.layout.entities.rglob("r*.json"))


def edit_interior_tx_line(store: FileStore) -> None:
    segment = store.txlog.path(store.txlog.segments()[0][1])
    lines = segment.read_bytes().splitlines(keepends=True)
    lines[3] = (
        lines[3].replace(b'"reason"', b'"reasom"')
        if b'"reason"' in lines[3]
        else (lines[3].replace(b'"apply"', b'"apply2"', 1))
    )
    segment.write_bytes(b"".join(lines))


def edit_revision_file(store: FileStore) -> None:
    path = revision_files(store)[0]
    document = json.loads(path.read_text())
    document["attrs"]["tampered"] = "yes"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def delete_revision_file(store: FileStore) -> None:
    revision_files(store)[0].unlink()


def add_revision_file(store: FileStore) -> None:
    source = revision_files(store)[0]
    shutil.copy(source, source.with_name("r000099.json"))


def edit_head(store: FileStore) -> None:
    document = json.loads(store.layout.head.read_text())
    document["alias_counters"] = {"ZZ": 1}
    store.layout.head.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def edit_index(store: FileStore) -> None:
    with sqlite3.connect(store.layout.index) as db:
        db.execute("UPDATE entity SET type = 'ex:Tampered' WHERE id = (SELECT min(id) FROM entity)")


def edit_pinned_release(store: FileStore) -> None:
    (compiled,) = store.layout.ontology.glob("*/compiled.json")
    document = json.loads(compiled.read_text())
    document["classes"] = {}
    compiled.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


SCENARIOS: dict[str, Callable[[FileStore], None]] = {
    finding_codes.TX_CHAIN_BROKEN: edit_interior_tx_line,
    finding_codes.REVISION_HASH_MISMATCH: edit_revision_file,
    finding_codes.REVISION_MISSING: delete_revision_file,
    finding_codes.REVISION_UNEXPECTED: add_revision_file,
    finding_codes.HEAD_INCONSISTENT: edit_head,
    finding_codes.INDEX_INCONSISTENT: edit_index,
    finding_codes.PIN_MISMATCH: edit_pinned_release,
    finding_codes.BLOB_MISSING: delete_blob,
    finding_codes.BLOB_HASH_MISMATCH: flip_blob_byte,
    finding_codes.BLOB_ORPHAN: add_orphan_blob,
    finding_codes.BLOB_STAGING_LEFTOVER: add_staging_leftover,
}


# Sealed graphs (T16): codes only ``verify_blobs(decrypt=True)`` produces.

SEALED_CONTENT = (b"%PDF-1.4\n% first\n", b"%PDF-1.4\n% second\n")


def sealed_setup(tmp_path: Path) -> tuple[ServiceEnv, FileStoreMaintenance]:
    svc = ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "root", version="1.1.0")
    add_file(svc, tmp_path, 0)
    return svc, FileStoreMaintenance(svc.root, rings=key_rings(svc.root))


def add_file(svc: ServiceEnv, tmp_path: Path, which: int) -> None:
    path = tmp_path / f"input{which}.pdf"
    path.write_bytes(SEALED_CONTENT[which])
    request = RequestMeta.of("file add", {"args": {"path": path.name}})
    svc.make_files().add(FileAddRequest(svc.actor, svc.name, path, request))


def sealed_verify(maintenance: FileStoreMaintenance, graph: GraphId) -> list[str]:
    with maintenance.maintenance_session(graph, timeout_s=5) as session:
        codes = [f.code for f in session.verify()]
        codes += [f.code for f in session.verify_blobs(rehash=True, decrypt=True)]
    return list(dict.fromkeys(codes))


def drop_file_keys(svc: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(key_settings(svc.root).store_path(svc.entry.id)) as db:
        db.execute("DELETE FROM dek WHERE scope = 'file'")


def bind_another_blob(svc: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A writer bug: the second file's revision names the first file's blob."""
    (first,) = svc.txs()[0].blobs
    original = write_service._with_blob_names

    def wrong(*args: Any, **kw: Any) -> tuple[Revision, ...]:
        return tuple(replace(r, blobs=dict.fromkeys(r.blobs, first)) for r in original(*args, **kw))

    monkeypatch.setattr(write_service, "_with_blob_names", wrong)
    add_file(svc, tmp_path, 1)


SEALED_SCENARIOS: dict[str, Callable[[ServiceEnv, Path, pytest.MonkeyPatch], None]] = {
    finding_codes.BLOB_UNDECRYPTABLE: drop_file_keys,
    finding_codes.BLOB_CONTENT_MISMATCH: bind_another_blob,
}


# Sealed graphs (T16): the key store against the records and the purge events.


def keys_verify(svc: ServiceEnv, maintenance: FileStoreMaintenance) -> list[str]:
    with maintenance.maintenance_session(svc.entry.id, timeout_s=5) as session:
        found = session.verify_keys(purges(svc.graph_events()), orphans=True)
    return list(dict.fromkeys(f.code for f in found))


def drop_tx_key(svc: ServiceEnv, tmp_path: Path) -> None:
    with sqlite3.connect(key_settings(svc.root).store_path(svc.entry.id)) as db:
        db.execute("DELETE FROM dek WHERE scope = 'tx'")


def shred_without_purge(svc: ServiceEnv, tmp_path: Path) -> None:
    (revision,) = svc.txs()[0].revisions
    assert revision.sealed is not None
    svc.store.key_ring().shred([revision.sealed.kid], EventId(UUID(int=7)))


def restore_purged_blob(svc: ServiceEnv, tmp_path: Path) -> None:
    (name,) = svc.txs()[0].blobs
    assert isinstance(svc.store, FileStore)
    path = svc.store.layout.blob_path(name)
    kept = path.read_bytes()
    (revision,) = svc.txs()[0].revisions
    purge(
        admin_context(svc.root),
        graph_ref=svc.name,
        refs=[str(revision.id)],
        with_files=True,
        reason="ticket 1",
        dry_run=False,
    )
    path.write_bytes(kept)  # a copy put back from a backup


def wrap_under_another_kek(svc: ServiceEnv, tmp_path: Path) -> None:
    """A key whose row names a KEK other than the current one (a rotation not run)."""
    earlier = "0" * 32
    with sqlite3.connect(key_settings(svc.root).store_path(svc.entry.id)) as db:
        db.execute("INSERT INTO kek (kek_id, created_at) VALUES (?, ?)", (earlier, "2026"))
        db.execute("UPDATE dek SET kek_id = ? WHERE scope = 'tx'", (earlier,))


def mint_unused_key(svc: ServiceEnv, tmp_path: Path) -> None:
    """A file key whose staged copy was discarded: no record and no stored file names it."""
    svc.store.key_ring().file_key()


KEY_SCENARIOS: dict[str, Callable[[ServiceEnv, Path], None]] = {
    finding_codes.KEYS_MISSING: drop_tx_key,
    finding_codes.KEYS_SHREDDED_UNRECORDED: shred_without_purge,
    finding_codes.KEYS_BLOB_NOT_REMOVED: restore_purged_blob,
    finding_codes.KEYS_KEK_NOT_CURRENT: wrap_under_another_kek,
    finding_codes.KEYS_ORPHAN: mint_unused_key,
}


def test_clean_key_store_verifies_clean(tmp_path: Path) -> None:
    svc, maintenance = sealed_setup(tmp_path)
    assert keys_verify(svc, maintenance) == []
    (revision,) = svc.txs()[0].revisions
    purge(
        admin_context(svc.root),
        graph_ref=svc.name,
        refs=[str(revision.id)],
        with_files=True,
        reason="ticket 1",
        dry_run=False,
    )
    assert keys_verify(svc, maintenance) == []


@pytest.mark.parametrize("code", sorted(KEY_SCENARIOS))
def test_key_tamper_scenario_triggers_only_its_code(tmp_path: Path, code: str) -> None:
    svc, maintenance = sealed_setup(tmp_path)
    KEY_SCENARIOS[code](svc, tmp_path)
    assert keys_verify(svc, maintenance) == [code]


def test_every_storage_code_has_one_scenario() -> None:
    """R.6: registry and tamper scenarios match one to one."""
    scenarios = (SCENARIOS, SEALED_SCENARIOS, KEY_SCENARIOS)
    assert sum(len(s) for s in scenarios) == len(set().union(*scenarios))
    codes = {spec.code for spec in (*finding_codes.FINDING_CODES, *finding_codes.KEY_FINDING_CODES)}
    assert set().union(*scenarios) == codes


def test_clean_sealed_graph_verifies_clean(tmp_path: Path) -> None:
    svc, maintenance = sealed_setup(tmp_path)
    add_file(svc, tmp_path, 1)
    assert sealed_verify(maintenance, svc.entry.id) == []


@pytest.mark.parametrize("code", sorted(SEALED_SCENARIOS))
def test_sealed_tamper_scenario_triggers_only_its_code(
    tmp_path: Path, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, maintenance = sealed_setup(tmp_path)
    SEALED_SCENARIOS[code](svc, tmp_path, monkeypatch)
    assert sealed_verify(maintenance, svc.entry.id) == [code]


def test_clean_graph_verifies_clean(tmp_path: Path) -> None:
    env, _, maintenance = setup(tmp_path)
    assert verify(maintenance, env.graph_id) == []


@pytest.mark.parametrize("code", sorted(SCENARIOS))
def test_tamper_scenario_triggers_only_its_code(tmp_path: Path, code: str) -> None:
    env, store, maintenance = setup(tmp_path)
    SCENARIOS[code](store)
    assert set(verify(maintenance, env.graph_id)) == {code}


def test_more_head_inconsistencies(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    original = store.layout.head.read_text()
    for change in (
        {"seq": 99},
        {"hash": "sha256:" + "0" * 64},
        {"txlog": {"segment": "000000000001.jsonl", "offset": 3}},
        {"audit": {"seq": 2, "hash": "sha256:" + "0" * 64}},
    ):
        document = {**json.loads(original), **change}
        store.layout.head.write_text(json.dumps(document))
        assert verify(maintenance, env.graph_id) == [finding_codes.HEAD_INCONSISTENT], change
    store.layout.head.write_text("{broken")
    assert verify(maintenance, env.graph_id) == [finding_codes.HEAD_INCONSISTENT]


def test_more_index_inconsistencies(tmp_path: Path) -> None:
    """The writer-side meta of format 2: the log digest and the last full replay."""
    env, store, maintenance = setup(tmp_path)
    original = store.layout.index.read_bytes()
    for sql in (
        "UPDATE meta SET value = '[[\"000000000001.jsonl\",3,\"00\"]]' WHERE key = 'log_digest'",
        "UPDATE meta SET value = '0' WHERE key = 'full_verified_seq'",
        "UPDATE meta SET value = '999' WHERE key = 'full_verified_seq'",
        "INSERT INTO idempotency VALUES ('test:9', 'key-9', 1)",
    ):
        store.layout.index.write_bytes(original)
        with sqlite3.connect(store.layout.index) as db:
            db.execute(sql)
        assert verify(maintenance, env.graph_id) == [finding_codes.INDEX_INCONSISTENT], sql


def test_more_pin_mismatches(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    manifest = store.layout.manifest.read_text()
    store.layout.manifest.write_text(manifest.replace('version = "1.0.0"', 'version = "9.0.0"'))
    assert verify(maintenance, env.graph_id) == [finding_codes.PIN_MISMATCH]
    store.layout.manifest.write_text("broken = ")
    assert verify(maintenance, env.graph_id) == [finding_codes.PIN_MISMATCH]
    store.layout.manifest.unlink()
    assert verify(maintenance, env.graph_id) == [finding_codes.PIN_MISMATCH]


def test_audit_damage_is_left_to_the_audit_verifier(tmp_path: Path) -> None:
    """No cascade: an edited audit line behind the checkpoint is no storage finding."""
    env, store, maintenance = setup(tmp_path)
    segment = store.auditlog.path(store.auditlog.segments()[0][1])
    lines = segment.read_bytes().splitlines(keepends=True)
    lines[1] = lines[1].replace(b'"ts":"', b'"ts":"9', 1)
    segment.write_bytes(b"".join(lines))
    assert verify(maintenance, env.graph_id) == []


def test_verify_does_not_recover(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    segment = store.txlog.path(store.txlog.segments()[-1][1])
    with open(segment, "ab") as handle:
        handle.write(b'{"torn')
    before = tree_hash(store.layout.root)
    assert verify(maintenance, env.graph_id) == []
    assert tree_hash(store.layout.root) == before
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        report = session.recover()
        assert report.recovered
        assert not session.recover().recovered
    assert segment.read_bytes().endswith(b"\n")


def _derived_tree(store: FileStore) -> str:
    return tree_hash(store.layout.entities) + store.layout.head.read_text()


def _index_seq(store: FileStore) -> int | None:
    reader = IndexReader.open(store.layout.index)
    if reader is None:
        return None
    reader.close()
    return reader.meta.seq


def test_reindex_rebuilds_an_identical_tree(tmp_path: Path) -> None:
    """C, B4.1: delete entities/ and head.json, rebuild from the log."""
    env, store, maintenance = setup(tmp_path)
    before = _derived_tree(store)
    shutil.rmtree(store.layout.entities)
    store.layout.head.unlink()
    shutil.rmtree(store.layout.index.parent)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.reindex()
    assert _derived_tree(store) == before
    assert _index_seq(store) == store.head().seq
    edit_index(store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.reindex()  # also from a complete tree, replacing a damaged index
        assert session.verify() == []
    assert _derived_tree(store) == before


def test_writer_recovery_also_rebuilds_the_derived_tree(tmp_path: Path) -> None:
    _, store, _ = setup(tmp_path)
    before = _derived_tree(store)
    shutil.rmtree(store.layout.entities)
    store.layout.head.unlink()
    with store.session(timeout_s=5):
        pass
    assert _derived_tree(store) == before


def test_reindex_refuses_a_damaged_log(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    edit_interior_tx_line(store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        with pytest.raises(GraphCliError) as info:
            session.reindex()
        assert info.value.code == "backend.corrupt"
        with pytest.raises(GraphCliError) as info:
            session.recover()
        assert info.value.code == "backend.corrupt"
        assert [f.code for f in session.verify()] == [finding_codes.TX_CHAIN_BROKEN]


def test_maintenance_session_writes_like_a_write_session(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    env.store = store
    head = store.head()
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert session.head() == head
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        draft.create()
        tx = draft.build(idempotency_key="maint-1")
        session.commit(tx, session.append_audit(intent(tx)))
        assert session.lookup_idempotency("maint-1", "test:1") is not None
    assert store.head().seq == head.seq + 1
    assert verify(maintenance, env.graph_id) == []


def test_maintenance_session_holds_the_graph_lock(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5):
        with pytest.raises(GraphCliError) as info, store.session(timeout_s=0.05):
            pass
        assert info.value.code == "backend.lock_timeout"


def test_unknown_graph_is_unavailable(tmp_path: Path) -> None:
    env, _, maintenance = setup(tmp_path)
    other = GraphId(env.ids.new_id())
    with pytest.raises(GraphCliError) as info, maintenance.maintenance_session(other, timeout_s=1):
        pass
    assert info.value.code == "backend.unavailable"
    assert not (tmp_path / "graphs" / str(other)).exists()


def test_only_rehash_reads_the_stored_bytes(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    flip_blob_byte(store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert session.verify() == []
        assert session.verify_blobs(rehash=False) == []
        (finding,) = session.verify_blobs(rehash=True)
    assert finding.code == finding_codes.BLOB_HASH_MISMATCH
    assert finding.target == {"path": str(stored_blob(store).relative_to(store.layout.root))}
    assert finding.expected == "sha256:" + hashlib.sha256(CONTENT).hexdigest()


def test_storage_verify_reports_a_missing_blob(tmp_path: Path) -> None:
    """``verify`` (and so ``audit verify``) sees a missing referenced blob, not the rest."""
    env, store, maintenance = setup(tmp_path)
    delete_blob(store)
    add_staging_leftover(store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert [f.code for f in session.verify()] == [finding_codes.BLOB_MISSING]


def test_blob_findings_beyond_damage_name_no_orphans(tmp_path: Path) -> None:
    env, store, maintenance = setup(tmp_path)
    edit_interior_tx_line(store)
    add_orphan_blob(store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert session.verify_blobs(rehash=False) == []
        unused = AuditReceipt(store.stream, EventId(env.ids.new_id()), 1, "", object())
        with pytest.raises(GraphCliError) as info:
            session.remove_blobs([], [], unused)
    assert info.value.code == "backend.corrupt"
