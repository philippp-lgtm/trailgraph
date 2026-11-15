"""Maintenance of stored files in an object store (object storage O7, O8, step 6), through
``FileStoreMaintenance`` with an injected client factory and marker-cache clock, so the
fake's clock (and with it the store's ``Date``) can move by hours.

Verify by one listing (missing, orphans, damaged bytes with ``rehash``), misplaced copies in
both directions, a store that cannot be reached or is another store as one
``blob_store_unavailable`` finding (never a stop, never a secret), the audit-verify blob check
by listing, gc by the store's clock and the removal of misplaced copies only after the
location the manifest names confirmed the same hash and size.
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from support.clock import FixedClock
from support.drafts import draft
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.graphs import publish_ontology
from support.s3_graphs import blob_key, create_marker, object_store_manifest
from support.stores import StoreEnv

from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.storage import MisplacedCopies
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage import finding_codes
from graph_cli.storage.files.maintenance import FileMaintenanceSession, FileStoreMaintenance
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.files.store_marker import MARKER_KEY, reset_identity_cache
from graph_cli.storage.s3.client import ObjectStoreClient, RetryPolicy, Timeouts
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

KEPT = b"%PDF-1.4\nreferenced bytes in an object store\n"
LOOSE = b"bytes no tx names\n"
OTHER_ID = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime.now(UTC))


@pytest.fixture
def fake(clock: FixedClock) -> Iterator[FakeS3]:
    reset_identity_cache()
    with FakeS3(clock=clock) as store:
        yield store
    reset_identity_cache()


def maintenance_of(
    root: Path, settings: ObjectStoreSettings | None, clock: FixedClock, env: StoreEnv
) -> FileStoreMaintenance:
    def client(values: ObjectStoreSettings) -> ObjectStoreClient:
        return ObjectStoreClient(
            values,
            clock=clock,
            timeouts=Timeouts(connect_s=2.0, read_s=2.0),
            retry=RetryPolicy(max_retries=0),
        )

    return FileStoreMaintenance(
        root, syncer=env.syncer, object_store=settings, client_factory=client, monotonic=Ticks()
    )


def s3_graph(tmp_path: Path, fake: FakeS3) -> StoreEnv:
    """A graph in the fake store with one referenced file and a manifest with a pin."""
    settings = fake.settings()
    env = StoreEnv("s3", "inventory", tmp_path, seed=3, object_store=settings)
    env.write_blobs(lambda d: d.create(), [KEPT])
    layout = store_of(env).layout
    publish_ontology(layout.root, env.graph_id, "inventory", env.syncer)
    object_store_manifest(layout, env.graph_id, create_marker(settings))
    return env


def local_graph(tmp_path: Path) -> StoreEnv:
    env = StoreEnv("files", "inventory", tmp_path, seed=4)
    env.write_blobs(lambda d: d.create(), [KEPT])
    publish_ontology(store_of(env).layout.root, env.graph_id, "inventory", env.syncer)
    return env


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def local_copy(env: StoreEnv, data: bytes) -> Path:
    path = store_of(env).layout.blob_path(digest(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def listings(fake: FakeS3, since: int = 0) -> int:
    return sum(r.method == "GET" and "list-type" in r.query for r in fake.requests[since:])


def heads(fake: FakeS3, since: int = 0) -> int:
    return sum(
        r.method == "HEAD" and not (r.key or "").endswith(MARKER_KEY) for r in fake.requests[since:]
    )


def codes(findings: tuple[Finding, ...] | list[Finding]) -> list[tuple[str, str]]:
    return sorted((f.code, str(f.target.get("path"))) for f in findings)


def judge(
    maintenance: FileStoreMaintenance, env: StoreEnv, recorded: str | None = None
) -> MisplacedCopies:
    """The misplaced copies as gc judges them, before the lock; ``recorded`` is the store
    the graph's audit trail records it left."""
    copies = maintenance.misplaced_blobs(env.graph_id, recorded_store=lambda: recorded)
    assert copies is not None
    return copies


def hashes(blobs: tuple[BlobInfo, ...]) -> list[str]:
    return [b.hash for b in blobs]


def receipt(env: StoreEnv, session: FileMaintenanceSession) -> AuditReceipt:
    event = draft(env.stream, EventId(env.ids.new_id()), env.clock.now(), actions=("maint.repair",))
    return session.append_audit(event)


# ---- verify -------------------------------------------------------------------------


def test_verify_lists_the_graphs_prefix_once(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    env = s3_graph(tmp_path, fake)
    gone = b"gone\n"
    env.write_blobs(lambda d: d.create(), [gone])
    del fake.objects[blob_key(env.graph_id, digest(gone))]
    fake.put(blob_key(env.graph_id, digest(LOOSE)), LOOSE)
    damaged = b"damaged\n"
    fake.put(blob_key(env.graph_id, digest(damaged)), b"other bytes\n")
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        start = len(fake.requests)
        plain = session.verify_blobs(rehash=False)
        assert listings(fake, start) == 1
        assert heads(fake, start) == 1  # the one referenced hash the listing lacks
        full = session.verify_blobs(rehash=True)
    assert codes(plain.findings) == sorted(
        [
            (finding_codes.BLOB_MISSING, blob_key(env.graph_id, digest(gone))),
            (finding_codes.BLOB_ORPHAN, blob_key(env.graph_id, digest(LOOSE))),
            (finding_codes.BLOB_ORPHAN, blob_key(env.graph_id, digest(damaged))),
        ]
    )
    assert codes(full.findings) == sorted(
        [
            *codes(plain.findings),
            (finding_codes.BLOB_HASH_MISMATCH, blob_key(env.graph_id, digest(damaged))),
        ]
    )
    assert plain.stored is not None
    assert sorted(b.hash for b in plain.stored) == sorted(
        [digest(KEPT), digest(LOOSE), digest(damaged)]
    )


def test_audit_verify_checks_the_referenced_files_by_one_listing(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    env = s3_graph(tmp_path, fake)
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        start = len(fake.requests)
        assert session.verify() == []
        assert (listings(fake, start), heads(fake, start)) == (1, 0)
        del fake.objects[blob_key(env.graph_id, digest(KEPT))]
        (finding,) = session.verify()
    assert finding.code == finding_codes.BLOB_MISSING


def test_a_local_copy_of_a_graph_in_the_object_store_is_misplaced(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    env = s3_graph(tmp_path, fake)
    confirmed = local_copy(env, KEPT)
    unconfirmed = local_copy(env, LOOSE)  # the object store lacks it
    (store_of(env).layout.blobs / ".tmp-leftover").write_bytes(LOOSE)  # staging, not a copy
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        found = session.verify_blobs(rehash=False).findings
    copies = judge(maintenance, env)
    misplaced = {str(f.target["path"]): f for f in found if f.code == finding_codes.BLOB_MISPLACED}
    root = store_of(env).layout.root
    assert set(misplaced) == {str(p.relative_to(root)) for p in (confirmed, unconfirmed)}
    assert all(f.severity.value == "info" for f in misplaced.values())
    assert "files gc removes the copy" in misplaced[str(confirmed.relative_to(root))].message
    assert "files gc keeps the copy" in misplaced[str(unconfirmed.relative_to(root))].message
    assert "the object store" in misplaced[str(confirmed.relative_to(root))].message
    assert (hashes(copies.removable), hashes(copies.kept)) == ([digest(KEPT)], [digest(LOOSE)])
    assert (copies.location, copies.store_id) == ("local", None)
    assert {f.code for f in found} == {
        finding_codes.BLOB_MISPLACED,
        finding_codes.BLOB_STAGING_LEFTOVER,
    }


def test_an_object_of_a_local_graph_is_misplaced_when_a_store_is_configured(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """After a recorded move from this store back to the data root (the recorded store
    is the bucket's marker id) its objects are the graph's own: confirmed ones may go."""
    env = local_graph(tmp_path)
    store_id = create_marker(fake.settings())
    fake.put(blob_key(env.graph_id, digest(KEPT)), KEPT)
    fake.put(blob_key(env.graph_id, digest(LOOSE)), LOOSE)
    without = maintenance_of(tmp_path, None, clock, env)
    with without.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert session.verify_blobs(rehash=True).findings == ()  # no store: never asked
    assert without.misplaced_blobs(env.graph_id) is None
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        found = session.verify_blobs(rehash=True, recorded_store=lambda: store_id).findings
    copies = judge(maintenance, env, store_id)
    assert codes(found) == sorted(
        (finding_codes.BLOB_MISPLACED, blob_key(env.graph_id, digest(data)))
        for data in (KEPT, LOOSE)
    )
    assert "the data root" in found[0].message
    assert "not this graph's recorded store" not in found[0].message
    assert (hashes(copies.removable), hashes(copies.kept)) == ([digest(KEPT)], [digest(LOOSE)])
    assert (copies.location, copies.store_id) == ("s3", store_id)


@pytest.mark.parametrize("recorded", [None, OTHER_ID])
def test_objects_in_a_store_the_local_graph_was_not_recorded_to_leave_are_report_only(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, recorded: str | None
) -> None:
    """A cloned or restored installation may share the bucket and the graph id: without a
    recorded switch from this very store (or with another marker id), its objects are
    reported, never removable, and gc refuses to delete them even when asked."""
    env = local_graph(tmp_path)
    create_marker(fake.settings())
    key = blob_key(env.graph_id, digest(KEPT))
    fake.put(key, KEPT)  # the data root holds the same bytes
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        (found,) = session.verify_blobs(rehash=True, recorded_store=lambda: recorded).findings
    assert found.code == finding_codes.BLOB_MISPLACED
    assert "not this graph's recorded store" in found.message
    assert "files gc keeps the copy" in found.message
    copies = judge(maintenance, env, recorded)
    assert (hashes(copies.removable), hashes(copies.kept), copies.store_id) == (
        [],
        [digest(KEPT)],
        None,
    )
    forged = replace(copies, removable=copies.kept, store_id=recorded)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.remove_blobs([], [], receipt(env, session), misplaced=forged)
    assert fake.get(key) == KEPT


@pytest.mark.parametrize("problem", ["no marker", "unreachable", "no credentials"])
def test_a_local_graph_never_fails_because_of_the_object_store(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, problem: str
) -> None:
    env = local_graph(tmp_path)
    settings = fake.settings()
    if problem == "unreachable":
        settings = replace(settings, endpoint_url="http://127.0.0.1:9")
    elif problem == "no credentials":
        settings = replace(settings, credentials=None)
    maintenance = maintenance_of(tmp_path, settings, clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert session.verify_blobs(rehash=True).findings == ()
        assert session.verify() == []
    assert maintenance.misplaced_blobs(env.graph_id, recorded_store=lambda: OTHER_ID) is None


@pytest.mark.parametrize("problem", ["other store", "no settings", "no credentials", "500"])
def test_an_unusable_store_is_one_finding_in_both_verifies(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, problem: str
) -> None:
    env = s3_graph(tmp_path, fake)
    (store_of(env).layout.blobs / ".tmp-leftover").write_bytes(LOOSE)
    settings: ObjectStoreSettings | None = fake.settings()
    if problem == "other store":
        document = {"schema": "graph_cli.object_store/1", "store_id": OTHER_ID, "created_at": "x"}
        fake.put(MARKER_KEY, json.dumps(document).encode())
    elif problem == "no settings":
        settings = None
    elif problem == "no credentials":
        settings = replace(fake.settings(), credentials=None)
    else:
        fake.fail(500, "InternalError", times=10, method="GET")
    maintenance = maintenance_of(tmp_path, settings, clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        (storage,) = session.verify()
        checked = session.verify_blobs(rehash=True)
    assert storage.code == finding_codes.BLOB_STORE_UNAVAILABLE
    assert storage.severity.value == "error"
    assert storage.target == {"path": f"graphs/{env.graph_id}/blobs/"}
    assert checked.stored is None
    assert sorted(f.code for f in checked.findings) == [
        finding_codes.BLOB_STAGING_LEFTOVER,
        finding_codes.BLOB_STORE_UNAVAILABLE,
    ]
    assert storage in checked.findings
    assert isinstance(storage.actual, dict) and storage.actual["code"] == "backend.unavailable"
    if problem == "other store":
        assert "not in that bucket" in storage.message and OTHER_ID in storage.message


def test_no_secret_in_a_store_unavailable_finding(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """Regression: the fake's error bodies carry the access key id and the string to sign."""
    env = s3_graph(tmp_path, fake)
    wrong = ObjectStoreCredentials(ACCESS_KEY_ID, "wrong/" + SECRET_ACCESS_KEY)
    settings = replace(fake.settings(), credentials=wrong)
    maintenance = maintenance_of(tmp_path, settings, clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        findings = [*session.verify(), *session.verify_blobs(rehash=True).findings]
    assert {f.code for f in findings} == {finding_codes.BLOB_STORE_UNAVAILABLE}
    text = repr([f.to_json() for f in findings])
    assert "credentials" in text
    assert ACCESS_KEY_ID not in text and SECRET_ACCESS_KEY not in text
    assert "AWS4-HMAC-SHA256" not in text


def test_an_unreadable_manifest_judges_no_blob(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """A hand-broken format-2 manifest would read as local: no storm of missing files,
    only the manifest itself (``pin_mismatch``)."""
    env = s3_graph(tmp_path, fake)
    layout = store_of(env).layout
    layout.manifest.write_text(layout.manifest.read_text().replace("store_id", "store_ld"))
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        assert [f.code for f in session.verify()] == [finding_codes.PIN_MISMATCH]


# ---- gc -----------------------------------------------------------------------------


@pytest.mark.parametrize(("age_h", "gone"), [(23, False), (25, True)])
def test_gc_removes_orphans_by_the_stores_clock(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, age_h: int, gone: bool
) -> None:
    env = s3_graph(tmp_path, fake)
    key = blob_key(env.graph_id, digest(LOOSE))
    fake.put(key, LOOSE)
    clock.advance(timedelta(hours=age_h))  # the store's clock: Date of its answers
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        orphans = list(session.orphan_blobs())
        assert [b.hash for b in orphans] == ([digest(LOOSE)] if gone else [])
        session.remove_blobs([b.hash for b in orphans], [], receipt(env, session))
    assert (key in fake.objects) is not gone
    assert blob_key(env.graph_id, digest(KEPT)) in fake.objects


def test_gc_removes_a_misplaced_copy_only_after_confirming_it(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    env = s3_graph(tmp_path, fake)
    confirmed = local_copy(env, KEPT)
    unconfirmed = local_copy(env, LOOSE)
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    copies = judge(maintenance, env)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.remove_blobs([], [], receipt(env, session), misplaced=copies)
    assert not confirmed.exists()
    assert unconfirmed.exists()
    assert blob_key(env.graph_id, digest(KEPT)) in fake.objects


def test_gc_confirms_again_under_the_removal(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    """The hash is confirmed once, before the lock (immutable content addresses); under
    the lock only presence and size are checked again, by one listing and without reading
    any file: the named location lost the file after it was judged, so the copy stays."""
    env = s3_graph(tmp_path, fake)
    copy = local_copy(env, KEPT)
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    start = len(fake.requests)
    copies = judge(maintenance, env)
    assert hashes(copies.removable) == [digest(KEPT)]
    reads = [r for r in fake.requests[start:] if r.method == "GET" and "list-type" not in r.query]
    assert [r.key for r in reads if not (r.key or "").endswith(MARKER_KEY)] == [
        blob_key(env.graph_id, digest(KEPT))
    ]
    smaller = replace(copies, removable=(BlobInfo(digest(KEPT), len(KEPT) - 1),))
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        start = len(fake.requests)
        session.remove_blobs([], [], receipt(env, session), misplaced=smaller)
        assert copy.exists()  # another size is no confirmation
        del fake.objects[blob_key(env.graph_id, digest(KEPT))]
        session.remove_blobs([], [], receipt(env, session), misplaced=copies)
        blob_gets = [
            r
            for r in fake.requests[start:]
            if r.method == "GET" and "list-type" not in r.query and "/blobs/" in (r.key or "")
        ]
        assert blob_gets == []  # nothing is read in full under the lock
    assert copy.exists()


def test_gc_removes_a_local_graphs_confirmed_objects(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    env = local_graph(tmp_path)
    store_id = create_marker(fake.settings())
    referenced, loose = blob_key(env.graph_id, digest(KEPT)), blob_key(env.graph_id, digest(LOOSE))
    fake.put(referenced, KEPT)
    fake.put(loose, LOOSE)
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    copies = judge(maintenance, env, store_id)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.remove_blobs([], [], receipt(env, session), misplaced=copies)
    assert referenced not in fake.objects  # the data root holds the file
    assert loose in fake.objects  # the data root lacks it: kept
    assert store_of(env).blob(digest(KEPT)) == BlobInfo(digest(KEPT), len(KEPT))
    assert hashes(copies.kept) == [digest(LOOSE)]


def damaged(data: bytes) -> bytes:
    """Other bytes of the same size (bit rot: the listing cannot see it)."""
    return bytes([data[0] ^ 0x01]) + data[1:]


@pytest.mark.parametrize("named", ["s3", "local"])
def test_a_damaged_named_copy_keeps_the_misplaced_copy(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, named: str
) -> None:
    """Never lose a file: gc confirms the graph's location by the hash of its bytes (a
    verified GET, a rehash), not by the listing's size; a damaged copy there keeps the
    good misplaced one, in both directions."""
    recorded = None
    if named == "s3":
        env = s3_graph(tmp_path, fake)
        key = blob_key(env.graph_id, digest(KEPT))
        fake.put(key, damaged(KEPT))
        misplaced = local_copy(env, KEPT)
    else:
        env = local_graph(tmp_path)
        recorded = create_marker(fake.settings())
        key = blob_key(env.graph_id, digest(KEPT))
        fake.put(key, KEPT)
        path = store_of(env).layout.blob_path(digest(KEPT))
        path.chmod(0o644)
        path.write_bytes(damaged(KEPT))
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    copies = judge(maintenance, env, recorded)
    assert (hashes(copies.removable), hashes(copies.kept)) == ([], [digest(KEPT)])
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.remove_blobs([], [], receipt(env, session), misplaced=copies)
        found = session.verify_blobs(rehash=True, recorded_store=lambda: recorded).findings
    if named == "s3":
        assert misplaced.read_bytes() == KEPT
    else:
        assert fake.get(key) == KEPT
    (copy,) = [f for f in found if f.code == finding_codes.BLOB_MISPLACED]
    assert "files gc keeps the copy" in copy.message
    assert finding_codes.BLOB_HASH_MISMATCH in {f.code for f in found}


def test_an_unreadable_named_copy_keeps_the_misplaced_copy(
    tmp_path: Path, fake: FakeS3, clock: FixedClock
) -> None:
    env = s3_graph(tmp_path, fake)
    misplaced = local_copy(env, KEPT)
    maintenance = maintenance_of(tmp_path, fake.settings(), clock, env)
    fake.fail(500, "InternalError", method="GET", key=blob_key(env.graph_id, digest(KEPT)))
    copies = judge(maintenance, env)
    assert (hashes(copies.removable), hashes(copies.kept)) == ([], [digest(KEPT)])
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.remove_blobs([], [], receipt(env, session), misplaced=copies)
    assert misplaced.read_bytes() == KEPT
