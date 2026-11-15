"""The blob seam of FileStore (object storage plan O1, step 3): which backend serves a graph.

``LocalBlobs`` implements ``BlobBackend`` over ``storage.files.blobs``; a store resolves
the backend from the manifest per read, a write session binds it on first blob use under
the lock, and the commit check asks the bound backend. A manifest that names an object
store gets ``S3Blobs`` (``test_s3_blobs.py``), which refuses blob access without usable
settings. Parsed manifests are cached by the file's identity.
"""

import hashlib
import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.drafts import draft
from support.stores import StoreEnv

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId, TermRef
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.ports.storage import CAPABILITY_BLOB_LOCAL_PATH, CAPABILITY_SNAPSHOT_AT_SEQ
from graph_cli.kernel.records import BlobInfo, OntologyPin
from graph_cli.storage.files import blob_backend
from graph_cli.storage.files.blob_backend import (
    BlobBackend,
    BlobBackends,
    LocalBlobs,
    blob_manifest,
)
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.s3_blobs import S3Blobs
from graph_cli.storage.files.store import FileStore

CONTENT = b"stored bytes\n"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
STORE_ID = "4b0c9f3e-2d1a-4c5b-9e8f-7a6b5c4d3e2f"
OBJECT_STORE = StoreInfo("files", 1, "s3", STORE_ID)


@pytest.fixture
def env(tmp_path: Path) -> StoreEnv:
    return StoreEnv("files", "inventory", tmp_path, seed=3)


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def write_manifest(env: StoreEnv, store: StoreInfo) -> None:
    entry = GraphEntry(
        id=env.graph_id,
        name="example_graph",
        state="active",
        pin=OntologyPin("inventory", "1.0.0", "sha256:" + "0" * 64),
        alias_prefixes={TermRef("inv:Item"): "ITEM"},
        extensions=(),
    )
    data = ManifestData(entry, datetime(2030, 1, 1, tzinfo=UTC), store)
    store_of(env).layout.manifest.write_bytes(format_manifest(data))


def test_local_blobs_has_every_member_of_the_protocol() -> None:
    """mypy checks the assignment in ``BlobBackends.backend``; this pins the signatures."""
    members = {
        name
        for name, _ in inspect.getmembers(BlobBackend)
        if not name.startswith("_") and name in BlobBackend.__dict__
    }
    assert members == {
        "location",
        "local_paths",
        "stage",
        "promote",
        "publish",
        "info",
        "read",
        "local_path",
        "missing",
        "absent",
        "listing",
        "staging",
        "verify",
        "unavailable",
        "collectable",
        "remove",
        "store_id",
        "hash_of",
        "remove_misplaced",
        "receive",
        "target",
    }
    for name in sorted(members):
        wanted = getattr(BlobBackend, name)
        actual = getattr(LocalBlobs, name)
        if callable(wanted):
            assert inspect.signature(actual) == inspect.signature(wanted), name
        else:
            assert hasattr(LocalBlobs, name), name
    assert (LocalBlobs.location, LocalBlobs.local_paths) == ("local", True)


@pytest.mark.parametrize("manifest", ["absent", "local", "unreadable"])
def test_a_store_without_an_object_store_manifest_uses_local_blobs(
    env: StoreEnv, manifest: str
) -> None:
    if manifest == "local":
        write_manifest(env, StoreInfo("files", 1))
    elif manifest == "unreadable":
        store_of(env).layout.manifest.write_text("broken = ")
    store = store_of(env)
    assert isinstance(store.blob_backend(), LocalBlobs)
    assert store.capabilities() == {CAPABILITY_SNAPSHOT_AT_SEQ, CAPABILITY_BLOB_LOCAL_PATH}
    env.write_blobs(lambda d: d.create(), [CONTENT])
    assert store.blob(DIGEST) == BlobInfo(DIGEST, len(CONTENT))
    assert store.blob_local_path(DIGEST) == store.layout.blob_path(DIGEST)
    assert b"".join(store.read_blob(DIGEST)) == CONTENT


def test_an_object_store_manifest_without_settings_is_refused_on_blob_access_only(
    env: StoreEnv,
) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    write_manifest(env, OBJECT_STORE)
    store = store_of(env)
    assert isinstance(store.blob_backend(), S3Blobs)  # built without any I/O or check
    staged = store.stage_blob([CONTENT], max_bytes=100)  # staging stays local
    try:
        for touch in (
            lambda: store.blob(DIGEST),
            lambda: store.read_blob(DIGEST),
            lambda: store.promote_blobs([staged]),
            lambda: list(store.blob_backend().listing()),
        ):
            with pytest.raises(GraphCliError) as caught:
                touch()
            assert caught.value.code == "backend.unavailable"
            assert caught.value.details["store_id"] == STORE_ID
            assert str(env.graph_id) in caught.value.message
    finally:
        staged.discard()
    assert store.blob_local_path(DIGEST) is None  # no local paths, nothing to ask
    # reads and writes that touch no file need no object store
    assert store.capabilities() == {CAPABILITY_SNAPSHOT_AT_SEQ}
    assert store.snapshot().seq == 1
    store.promote_blobs([])
    env.write(lambda d: d.create())
    assert store.head().seq == 2


def test_parsed_manifests_are_cached_by_the_file_identity(
    env: StoreEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(env, StoreInfo("files", 1))
    layout = store_of(env).layout
    parsed: list[Path] = []
    real = blob_backend.read_manifest

    def counting(layout: GraphLayout) -> Any:
        parsed.append(layout.manifest)
        return real(layout)

    monkeypatch.setattr(blob_backend, "read_manifest", counting)
    first = blob_manifest(layout)
    assert blob_manifest(layout) is first
    assert len(parsed) == 1
    # replaced by rename (a new inode), as graph-admin replaces it
    data = format_manifest(ManifestData(first.entry, first.created_at, OBJECT_STORE))  # type: ignore[union-attr]
    tmp = layout.manifest.with_name("manifest.toml.tmp")
    tmp.write_bytes(data)
    tmp.replace(layout.manifest)
    second = blob_manifest(layout)
    assert second is not None and second.store == OBJECT_STORE
    assert len(parsed) == 2
    layout.manifest.unlink()
    assert blob_manifest(layout) is None


def test_the_session_binds_the_manifest_location_under_the_lock(env: StoreEnv) -> None:
    store = store_of(env)
    staged = store.stage_blob([CONTENT], max_bytes=100)  # staged while the graph was local
    write_manifest(env, OBJECT_STORE)  # the manifest names another location at lock time
    with store.session(timeout_s=5) as session:
        with pytest.raises(GraphCliError) as caught:
            session.publish_blob(staged)
        assert caught.value.code == "backend.unavailable"
    staged.discard()


@dataclass
class Spy:
    """``BlobBackends`` whose backends record what they are asked."""

    calls: list[tuple[str, Any]] = field(default_factory=list)
    absent: frozenset[str] = frozenset()
    real: BlobBackends = field(default_factory=BlobBackends)

    def backend(self, layout: Any, manifest: Any, *, syncer: Any, faults: Any) -> BlobBackend:
        self.calls.append(("bind", None))
        local = self.real.backend(layout, manifest, syncer=syncer, faults=faults)
        spy = self

        class Recording(LocalBlobs):
            def publish(self, staged: Any) -> BlobInfo:
                spy.calls.append(("publish", staged.hash))
                return local.publish(staged)

            def missing(self, referenced: Any) -> list[str]:
                spy.calls.append(("missing", sorted(referenced)))
                return sorted(set(local.missing(referenced)) | (set(referenced) & spy.absent))

        return Recording(layout, syncer=syncer, faults=faults)

    def capabilities(self, manifest: Any) -> frozenset[str]:
        return self.real.capabilities(manifest)


def spied(env: StoreEnv, spy: Spy) -> FileStore:
    store = store_of(env)
    store.blob_backends = spy  # type: ignore[assignment]
    return store


def test_a_session_binds_once_and_the_commit_check_asks_the_bound_backend(
    env: StoreEnv,
) -> None:
    spy = Spy()
    spied(env, spy)
    env.write(lambda d: d.create())
    assert spy.calls == []  # a tx without blobs never needs the backend
    env.write_blobs(lambda d: d.create(), [CONTENT])
    assert spy.calls == [
        ("bind", None),  # stage_blob resolves per call, lock-free
        ("bind", None),  # the session binds on publish
        ("publish", DIGEST),
        ("missing", [DIGEST]),  # the commit check, through the same binding
    ]


def test_the_commit_check_refuses_what_the_bound_backend_lacks(env: StoreEnv) -> None:
    spy = Spy(absent=frozenset({DIGEST}))
    spied(env, spy)
    with pytest.raises(GraphCliError) as caught:
        env.write_blobs(lambda d: d.create(), [CONTENT])
    assert caught.value.code == "internal.error"
    assert (caught.value.details["rule"], caught.value.details["blob"]) == ("blobs", DIGEST)
    assert store_of(env).head().seq == 0


def test_a_replaced_manifest_rebinds_the_maintenance_session(env: StoreEnv) -> None:
    write_manifest(env, StoreInfo("files", 1))
    staged = store_of(env).stage_blob([CONTENT], max_bytes=100)
    maintenance = FileStoreMaintenance(env.root, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.snapshot()  # the write session exists
        event = draft(
            env.stream,
            EventId(env.ids.new_id()),
            env.clock.now(),
            actions=("maint.graph_configure",),
        )
        receipt = session.append_audit(event)
        manifest = session.manifest()
        session.replace_manifest(
            ManifestData(manifest.entry, manifest.created_at, OBJECT_STORE), receipt
        )
        with pytest.raises(GraphCliError) as caught:
            session.publish_blob(staged)
        assert caught.value.code == "backend.unavailable"
    staged.discard()
