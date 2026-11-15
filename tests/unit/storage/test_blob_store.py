"""FileStore blobs: layout, staging leftovers, listing and removal (files slice 1)."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.drafts import draft
from support.stores import StoreEnv

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage.files.maintenance import FileMaintenanceSession, FileStoreMaintenance
from graph_cli.storage.files.store import FileStore

KEPT = b"referenced bytes\n"
LOOSE = b"unreferenced bytes\n"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path) -> StoreEnv:
    env = StoreEnv("files", "inventory", tmp_path, seed=2)
    env.write_blobs(lambda d: d.create(), [KEPT])
    return env


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def add_orphan(env: StoreEnv) -> None:
    staged = env.store.stage_blob([LOOSE], max_bytes=100)
    with env.store.session(timeout_s=5) as session:
        session.publish_blob(staged)  # stored, but no tx names it (KB2)


def receipt(env: StoreEnv, session: FileMaintenanceSession) -> AuditReceipt:
    event = draft(env.stream, EventId(env.ids.new_id()), env.clock.now(), actions=("maint.repair",))
    return session.append_audit(event)


def test_graph_dirs_have_no_blobs_dir_until_a_file_is_stored(tmp_path: Path) -> None:
    env = StoreEnv("files", "inventory", tmp_path)
    env.write(lambda d: d.create())
    assert not store_of(env).layout.blobs.exists()


def test_readers_ignore_staging_files(env: StoreEnv) -> None:
    store = store_of(env)
    leftover = store.layout.blobs / ".tmp-leftover"
    leftover.write_bytes(LOOSE)
    maintenance = FileStoreMaintenance(env.root, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert list(session.blobs()) == [BlobInfo(digest(KEPT), len(KEPT))]
        assert [name for name, _ in session.staging()] == [".tmp-leftover"]
        assert [f.code for f in session.verify()] == [
            "integrity.storage.pin_mismatch"
        ]  # no release
    assert store.blob(digest(LOOSE)) is None
    assert store.snapshot().seq == 1


def test_listing_skips_strays_and_is_in_hash_order(env: StoreEnv) -> None:
    store = store_of(env)
    add_orphan(env)
    (store.layout.blobs / "sha256" / "zz").mkdir()
    (store.layout.blob_path(digest(KEPT)).parent / "notes.txt").write_text("stray")
    maintenance = FileStoreMaintenance(env.root, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert [b.hash for b in session.blobs()] == sorted([digest(KEPT), digest(LOOSE)])


def test_staging_reports_the_file_time(env: StoreEnv) -> None:
    store = store_of(env)
    old = store.layout.blobs / ".tmp-old"
    old.write_bytes(LOOSE)
    os.utime(old, (1_800_000_000, 1_800_000_000))
    maintenance = FileStoreMaintenance(env.root, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert dict(session.staging()) == {".tmp-old": datetime(2027, 1, 15, 8, 0, tzinfo=UTC)}


def test_remove_deletes_orphans_and_staging_only(env: StoreEnv) -> None:
    store = store_of(env)
    add_orphan(env)
    (store.layout.blobs / ".tmp-leftover").write_bytes(LOOSE)
    maintenance = FileStoreMaintenance(env.root, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        for hashes, names in (([digest(KEPT)], []), ([], ["../x"]), ([], ["data"])):
            with pytest.raises(GraphCliError) as info:
                session.remove_blobs(hashes, names, receipt(env, session))
            assert info.value.code == "internal.error"
        session.remove_blobs(
            [digest(LOOSE)],
            [".tmp-leftover"],
            receipt(env, session),
        )
        assert [b.hash for b in session.blobs()] == [digest(KEPT)]
        assert list(session.staging()) == []
        assert session.verify_blobs(rehash=True).findings == ()
    assert b"".join(store.read_blob(digest(KEPT))) == KEPT


def test_remove_claims_the_receipt_once(env: StoreEnv) -> None:
    maintenance = FileStoreMaintenance(env.root, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        once = receipt(env, session)
        session.remove_blobs([], [], once)
        with pytest.raises(GraphCliError) as info:
            session.remove_blobs([], [], once)
    assert info.value.details["rule"] == "receipt_reused"
