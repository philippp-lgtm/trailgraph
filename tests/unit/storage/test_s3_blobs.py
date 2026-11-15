"""S3Blobs against the fake object store (object storage O5, O6, O8, O9, step 4).

The write path (promote, publish with its fallback, the commit check's existence set and
its listing for many hashes), verified reads, verify, gc by the store's clock, the store
marker with its per-process cache, fail-closed refusals on blob access only, the error
mapping at the backend and redaction. The client and the fake share one clock, so a test
can move the store's time by hours.
"""

import hashlib
import inspect
import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.clock import FixedClock
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.ids import SeededIdProvider
from support.s3_graphs import blob_key
from support.stores import StoreEnv, snapshot_state

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.storage import CAPABILITY_SNAPSHOT_AT_SEQ
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage import finding_codes
from graph_cli.storage.files.blob_backend import BlobBackend, BlobBackends, blob_manifest
from graph_cli.storage.files.layout import STAGING_PREFIX
from graph_cli.storage.files.s3_blobs import S3Blobs
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.files.store_marker import (
    IDENTITY_TTL_S,
    MARKER_KEY,
    ensure_store_marker,
    read_store_marker,
    reset_identity_cache,
)
from graph_cli.storage.s3.client import ObjectStoreClient, RetryPolicy, Timeouts
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

CONTENT = b"%PDF-1.4\nsynthetic bytes in an object store\n" * 2
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
OTHER_ID = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"
CHUNK = 16


class Ticks:
    """A monotonic clock for the store marker cache."""

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


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


def backends(settings: ObjectStoreSettings | None, clock: FixedClock, ticks: Ticks) -> BlobBackends:
    def client(values: ObjectStoreSettings) -> ObjectStoreClient:
        return ObjectStoreClient(
            values,
            clock=clock,
            timeouts=Timeouts(connect_s=2.0, read_s=2.0),
            retry=RetryPolicy(max_retries=0),
            chunk_size=CHUNK,
        )

    return BlobBackends(settings, client_factory=client, monotonic=ticks)


def make_env(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, ticks: Ticks, **settings: Any
) -> StoreEnv:
    env = StoreEnv("s3", "inventory", tmp_path, object_store=fake.settings(**settings))
    store_of(env).blob_backends = backends(env.object_store, clock, ticks)
    return env


@pytest.fixture
def env(tmp_path: Path, fake: FakeS3, clock: FixedClock, ticks: Ticks) -> StoreEnv:
    return make_env(tmp_path, fake, clock, ticks)


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return env.store


def backend_of(env: StoreEnv) -> S3Blobs:
    backend = store_of(env).blob_backend()
    assert isinstance(backend, S3Blobs)
    return backend


def key(env: StoreEnv, digest: str = DIGEST) -> str:
    return blob_key(
        env.graph_id, digest, prefix=env.object_store.prefix if env.object_store else ""
    )


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def count(fake: FakeS3, method: str, *, since: int = 0, marker: bool = False) -> int:
    """Requests of ``method`` after the ``since``-th; the marker's own only with ``marker``."""
    calls = fake.requests[since:]
    return sum(r.method == method and (r.key or "").endswith(MARKER_KEY) is marker for r in calls)


def listings(fake: FakeS3, *, since: int = 0) -> int:
    return sum(r.method == "GET" and "list-type" in r.query for r in fake.requests[since:])


def staging_files(env: StoreEnv) -> list[str]:
    blobs = store_of(env).layout.blobs
    return sorted(p.name for p in blobs.iterdir() if p.name.startswith(STAGING_PREFIX))


def refused(touch: Any) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        touch()
    return caught.value


def tx_naming(env: StoreEnv, digests: list[str]) -> Any:
    """A session context that commits a create listing ``digests`` (not published)."""

    class Commit:
        def __enter__(self) -> tuple[Any, AuthorizedTx, Any]:
            self.cm = store_of(env).session(timeout_s=5)
            session = self.cm.__enter__()
            draft = env.builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            draft.create()
            tx = replace(draft.build(), blobs=tuple(sorted(digests)))
            receipt = session.append_audit(env.intent(tx, session.snapshot()))
            return session, tx, receipt

        def __exit__(self, *exc: object) -> None:
            self.cm.__exit__(*exc)  # type: ignore[arg-type]

    return Commit()


# ---- shape -----------------------------------------------------------------


def test_s3_blobs_has_every_member_of_the_protocol() -> None:
    for name, wanted in BlobBackend.__dict__.items():
        if name.startswith("_"):
            continue
        actual = getattr(S3Blobs, name)
        if callable(wanted):
            assert inspect.signature(actual) == inspect.signature(wanted), name
    assert (S3Blobs.location, S3Blobs.local_paths) == ("s3", False)


def test_keys_and_targets_name_the_object_only(
    tmp_path: Path, fake: FakeS3, clock: FixedClock, ticks: Ticks
) -> None:
    env = make_env(tmp_path, fake, clock, ticks, prefix="tenant/")
    env.write_blobs(lambda d: d.create(), [CONTENT])
    hexdigest = DIGEST.removeprefix("sha256:")
    expected = f"tenant/graphs/{env.graph_id}/blobs/sha256/{hexdigest[:2]}/{hexdigest}"
    assert expected in fake.objects
    assert "tenant/" + MARKER_KEY in fake.objects  # the marker at the prefix root
    backend = backend_of(env)
    assert backend.key(DIGEST) == expected.removeprefix("tenant/")
    assert backend.target(DIGEST) == expected
    assert "127.0.0.1" not in backend.target(DIGEST)
    assert b"".join(store_of(env).read_blob(DIGEST)) == CONTENT


def test_building_the_backend_needs_no_store(env: StoreEnv, fake: FakeS3) -> None:
    before = len(fake.requests)
    store = store_of(env)
    store.blob_backends = BlobBackends()  # not even settings
    assert isinstance(store.blob_backend(), S3Blobs)
    assert store.capabilities() == {CAPABILITY_SNAPSHOT_AT_SEQ}  # no blob_local_path
    assert store.blob_local_path(DIGEST) is None
    store.stage_blob([CONTENT], max_bytes=len(CONTENT)).discard()
    store.promote_blobs([])
    assert len(fake.requests) == before


# ---- the write path ----------------------------------------------------------


def test_promote_uploads_and_publish_confirms(env: StoreEnv, fake: FakeS3) -> None:
    store = store_of(env)
    staged = store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    start = len(fake.requests)
    store.promote_blobs([staged, staged])  # one hash, one upload
    assert (count(fake, "HEAD", since=start), count(fake, "PUT", since=start)) == (1, 1)
    assert fake.get(key(env)) == CONTENT
    assert staging_files(env) == [staged.path.name]  # kept until publish
    published = len(fake.requests)
    with store.session(timeout_s=5) as session:
        assert session.publish_blob(staged) == BlobInfo(DIGEST, len(CONTENT))
        assert staging_files(env) == []  # removed once the object is confirmed
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        draft.create()
        tx = replace(draft.build(), blobs=(DIGEST,))
        checked = len(fake.requests)
        session.commit(tx, session.append_audit(env.intent(tx, session.snapshot())))
        assert count(fake, "HEAD", since=checked) == 0  # confirmed by this session
    assert (count(fake, "HEAD", since=published), count(fake, "PUT", since=published)) == (1, 0)


def test_publish_uploads_what_promote_did_not(env: StoreEnv, fake: FakeS3) -> None:
    """The fallback under the lock: a gc raced the promote, or the graph moved."""
    start = len(fake.requests)
    env.write_blobs(lambda d: d.create(), [CONTENT])  # no promote
    assert (count(fake, "HEAD", since=start), count(fake, "PUT", since=start)) == (1, 1)
    assert fake.get(key(env)) == CONTENT
    assert staging_files(env) == []


@pytest.mark.parametrize(("age_h", "uploads"), [(0, 0), (11, 0), (13, 1)])
def test_promote_refreshes_an_old_object(
    env: StoreEnv, fake: FakeS3, clock: FixedClock, age_h: int, uploads: int
) -> None:
    fake.put(key(env), CONTENT, last_modified=clock.now() - timedelta(hours=age_h))
    store = store_of(env)
    staged = store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    start = len(fake.requests)
    store.promote_blobs([staged])
    staged.discard()
    assert count(fake, "PUT", since=start) == uploads


def test_a_dedup_head_of_an_old_orphan_reuploads_so_gc_cannot_race_it(
    env: StoreEnv, fake: FakeS3, clock: FixedClock
) -> None:
    """Regression: a deduplicated add of an orphan older than 12 hours by the store's clock
    refreshes it, so a gc between the promote and the commit keeps it."""
    fake.put(key(env), CONTENT, last_modified=clock.now() - timedelta(hours=30))
    backend = backend_of(env)
    assert backend.collectable(frozenset()) == [BlobInfo(DIGEST, len(CONTENT))]
    store = store_of(env)
    staged = store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    store.promote_blobs([staged])
    clock.advance(timedelta(hours=1))
    assert backend.collectable(frozenset()) == []
    assert backend.remove([DIGEST], [], referenced=frozenset()) == {"blobs": 0, "staging": 0}
    assert fake.get(key(env)) == CONTENT
    with store.session(timeout_s=5) as session:
        session.publish_blob(staged)
    assert store.blob(DIGEST) == BlobInfo(DIGEST, len(CONTENT))


def test_a_discarded_staged_blob_is_not_published(env: StoreEnv, fake: FakeS3) -> None:
    staged = store_of(env).stage_blob([CONTENT], max_bytes=len(CONTENT))
    staged.discard()
    with store_of(env).session(timeout_s=5) as session:
        assert refused(lambda: session.publish_blob(staged)).code == "internal.error"
    assert key(env) not in fake.objects


@pytest.mark.parametrize("lag", [False, True])
def test_a_listing_replaces_heads_above_64_hashes(env: StoreEnv, fake: FakeS3, lag: bool) -> None:
    """Regression: a listing that lags (it misses new objects) still commits, because every
    hash it lacks is confirmed by HEAD."""
    contents = [f"file number {i}\n".encode() for i in range(65)]
    for data in contents:
        fake.put(key(env, digest_of(data)), data)
    if lag:
        fake.listing_lag = timedelta(hours=1)
    with tx_naming(env, [digest_of(d) for d in contents]) as (session, tx, receipt):
        start = len(fake.requests)
        session.commit(tx, receipt)
    assert listings(fake, since=start) == 1
    assert count(fake, "HEAD", since=start) == (65 if lag else 0)
    assert store_of(env).head().seq == 1


def test_up_to_64_hashes_are_asked_by_head(env: StoreEnv, fake: FakeS3) -> None:
    contents = [f"file number {i}\n".encode() for i in range(64)]
    for data in contents:
        fake.put(key(env, digest_of(data)), data)
    with tx_naming(env, [digest_of(d) for d in contents]) as (session, tx, receipt):
        start = len(fake.requests)
        session.commit(tx, receipt)
    assert (listings(fake, since=start), count(fake, "HEAD", since=start)) == (0, 64)


def test_the_commit_check_refuses_an_absent_object(env: StoreEnv, fake: FakeS3) -> None:
    contents = [f"file number {i}\n".encode() for i in range(70)]
    for data in contents[1:]:
        fake.put(key(env, digest_of(data)), data)
    with tx_naming(env, [digest_of(d) for d in contents]) as (session, tx, receipt):
        error = refused(lambda: session.commit(tx, receipt))
    assert (error.code, error.details["blob"]) == ("internal.error", digest_of(contents[0]))
    assert store_of(env).head().seq == 0


# ---- reads -----------------------------------------------------------------


def test_a_referenced_object_deleted_behind_the_stores_back_is_corrupt(
    env: StoreEnv, fake: FakeS3
) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    del fake.objects[key(env)]
    store = store_of(env)
    error = refused(lambda: store.read_blob(DIGEST))
    assert (error.code, error.details) == ("backend.corrupt", {"blob": DIGEST})
    assert store.blob(DIGEST) is None


def test_a_hash_mismatching_body_is_corrupt_without_its_tail(env: StoreEnv, fake: FakeS3) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    damaged = bytes(reversed(CONTENT))
    fake.corrupt_get(key(env), damaged)
    delivered: list[bytes] = []
    with pytest.raises(GraphCliError) as caught:
        for chunk in store_of(env).read_blob(DIGEST):
            delivered.append(chunk)
    assert (caught.value.code, caught.value.details) == ("backend.corrupt", {"blob": DIGEST})
    last = len(damaged) % CHUNK or CHUNK
    assert b"".join(delivered) == damaged[: len(damaged) - last]  # never the last chunk


def test_reads_stream_in_chunks(env: StoreEnv) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    chunks = list(store_of(env).read_blob(DIGEST))
    assert len(chunks) > 1 and b"".join(chunks) == CONTENT
    assert store_of(env).blob("not a hash") is None
    assert refused(lambda: store_of(env).read_blob("not a hash")).code == "internal.error"


# ---- verify and gc -----------------------------------------------------------


def test_verify_lists_once_and_rehash_reads_every_object(env: StoreEnv, fake: FakeS3) -> None:
    gone, orphan, damaged = b"gone\n", b"orphan\n", b"damaged\n"
    env.write_blobs(lambda d: d.create(), [CONTENT, gone])
    del fake.objects[key(env, digest_of(gone))]
    fake.put(key(env, digest_of(orphan)), orphan)
    fake.put(key(env, digest_of(damaged)), b"not the bytes of that hash\n")
    fake.put(f"graphs/{env.graph_id}/blobs/sha256/zz/stray", b"x")  # not a blob key
    staged = store_of(env).stage_blob([b"leftover"], max_bytes=100)
    backend = backend_of(env)
    referenced = frozenset({DIGEST, digest_of(gone)})
    start = len(fake.requests)
    checked = backend.verify(referenced, rehash=False)
    plain = checked.findings
    assert listings(fake, since=start) == 1
    assert count(fake, "HEAD", since=start) == 1  # the one hash the listing lacks
    assert checked.stored is not None
    assert sorted(b.hash for b in checked.stored) == sorted(
        [DIGEST, digest_of(orphan), digest_of(damaged)]
    )
    full = backend.verify(referenced, rehash=True).findings
    codes = sorted((f.code, f.target["path"]) for f in full)
    assert codes == sorted(
        [
            (finding_codes.BLOB_MISSING, key(env, digest_of(gone))),
            (finding_codes.BLOB_ORPHAN, key(env, digest_of(orphan))),
            (finding_codes.BLOB_ORPHAN, key(env, digest_of(damaged))),
            (finding_codes.BLOB_HASH_MISMATCH, key(env, digest_of(damaged))),
            (finding_codes.BLOB_STAGING_LEFTOVER, f"blobs/{staged.path.name}"),
        ]
    )
    assert len(plain) == len(full) - 1
    mismatch = next(f for f in full if f.code == finding_codes.BLOB_HASH_MISMATCH)
    assert mismatch.expected == digest_of(damaged)
    assert mismatch.actual == digest_of(b"not the bytes of that hash\n")
    assert sorted(b.hash for b in backend.listing()) == sorted(
        [DIGEST, digest_of(orphan), digest_of(damaged)]
    )
    staged.discard()


@pytest.mark.parametrize(("age_h", "removed"), [(23, 0), (25, 1)])
def test_gc_keeps_orphans_younger_than_24_hours_by_the_stores_clock(
    env: StoreEnv, fake: FakeS3, clock: FixedClock, age_h: int, removed: int
) -> None:
    fake.put(key(env), CONTENT, last_modified=clock.now() - timedelta(hours=age_h))
    staged = store_of(env).stage_blob([b"leftover"], max_bytes=100)
    backend = backend_of(env)
    assert len(backend.collectable(frozenset())) == removed
    counts = backend.remove([DIGEST], [staged.path.name], referenced=frozenset())
    assert counts == {"blobs": removed, "staging": 1}
    assert (key(env) in fake.objects) is (removed == 0)
    assert staging_files(env) == []


def test_gc_never_removes_a_referenced_blob(env: StoreEnv) -> None:
    error = refused(lambda: backend_of(env).remove([DIGEST], [], referenced=frozenset({DIGEST})))
    assert error.code == "internal.error"


# ---- the store marker ---------------------------------------------------------


def test_the_marker_is_created_once_and_never_overwritten(fake: FakeS3, clock: FixedClock) -> None:
    settings = fake.settings(prefix="other/")
    client = ObjectStoreClient(settings, clock=clock)
    assert read_store_marker(client) is None
    ids = SeededIdProvider(clock, 7)
    first = ensure_store_marker(client, settings, clock=clock, ids=ids)
    assert ensure_store_marker(client, settings, clock=clock, ids=ids) == first
    document = json.loads(fake.get("other/" + MARKER_KEY) or b"")
    assert document["schema"] == "graph_cli.object_store/1"
    assert document["store_id"] == first
    assert sum(r.method == "PUT" for r in fake.requests) == 1


def test_an_unreadable_marker_is_refused(fake: FakeS3, clock: FixedClock) -> None:
    fake.put(MARKER_KEY, b"<html>not a marker</html>")
    client = ObjectStoreClient(fake.settings(), clock=clock)
    error = refused(lambda: read_store_marker(client))
    assert error.code == "backend.unavailable"
    assert MARKER_KEY in error.message


def marker_of(store_id: str) -> bytes:
    return json.dumps(
        {"schema": "graph_cli.object_store/1", "store_id": store_id, "created_at": "x"}
    ).encode()


def file_free_reads_work(env: StoreEnv) -> None:
    store = store_of(env)
    assert store.snapshot().seq == 1
    assert store.capabilities() == {CAPABILITY_SNAPSHOT_AT_SEQ}
    env.write(lambda d: d.create())  # a write without files


@pytest.mark.parametrize("case", ["missing marker", "other store id", "wrong bucket"])
def test_a_foreign_bucket_is_unavailable_on_blob_access(
    env: StoreEnv, fake: FakeS3, clock: FixedClock, ticks: Ticks, case: str
) -> None:
    """Regression: never an empty listing or a missing-file storm."""
    env.write(lambda d: d.create())
    store = store_of(env)
    settings = env.object_store
    assert settings is not None
    reset_identity_cache()
    if case == "missing marker":
        del fake.objects[MARKER_KEY]
    elif case == "other store id":
        fake.put(MARKER_KEY, marker_of(OTHER_ID))
    else:
        store.blob_backends = backends(replace(settings, bucket="another-bucket"), clock, ticks)
    backend = backend_of(env)
    for touch in (
        lambda: store.blob(DIGEST),
        lambda: store.read_blob(DIGEST),
        lambda: list(backend.listing()),
        lambda: backend.verify(frozenset({DIGEST}), rehash=False),
        lambda: backend.missing(frozenset({DIGEST})),
    ):
        error = refused(touch)
        assert error.code == "backend.unavailable", error
        assert error.details.get("store_id") or error.details.get("s3_code") == "NoSuchBucket"
        if case != "wrong bucket":
            assert str(env.graph_id) in error.message
            assert "not in that bucket" in error.message
    if case == "other store id":
        assert OTHER_ID in refused(lambda: store.blob(DIGEST)).message
    file_free_reads_work(env)


def test_missing_credentials_are_unavailable_on_blob_access(
    env: StoreEnv, clock: FixedClock, ticks: Ticks
) -> None:
    env.write(lambda d: d.create())
    settings = env.object_store
    assert settings is not None
    store = store_of(env)
    store.blob_backends = backends(replace(settings, credentials=None), clock, ticks)
    error = refused(lambda: store.blob(DIGEST))
    assert error.code == "backend.unavailable"
    assert str(env.graph_id) in error.message
    assert error.details["missing"] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    file_free_reads_work(env)


def test_the_identity_check_is_cached_for_ten_minutes(
    env: StoreEnv, fake: FakeS3, ticks: Ticks
) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    reset_identity_cache()
    store = store_of(env)
    start = len(fake.requests)
    store.blob(DIGEST)
    store.blob(DIGEST)  # a new backend per read, one cache per process
    b"".join(store.read_blob(DIGEST))
    assert count(fake, "GET", since=start, marker=True) == 1
    ticks.now += IDENTITY_TTL_S - 1
    store.blob(DIGEST)
    assert count(fake, "GET", since=start, marker=True) == 1
    ticks.now += 2
    store.blob(DIGEST)
    assert count(fake, "GET", since=start, marker=True) == 2
    del fake.objects[key(env)]  # a 404 of a referenced object reads the marker again
    refused(lambda: store.read_blob(DIGEST))
    assert count(fake, "GET", since=start, marker=True) == 3
    fake.put(MARKER_KEY, marker_of(OTHER_ID))  # the bucket was swapped: not corrupt
    assert refused(lambda: store.read_blob(DIGEST)).code == "backend.unavailable"


def test_the_identity_cache_is_shared_by_threads(env: StoreEnv, fake: FakeS3) -> None:
    env.write_blobs(lambda d: d.create(), [CONTENT])
    reset_identity_cache()
    store = store_of(env)
    results: list[BlobInfo | None] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            results.append(store.blob(DIGEST))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert results == [BlobInfo(DIGEST, len(CONTENT))] * 8


# ---- error mapping and redaction ------------------------------------------------


def test_store_errors_pass_through(env: StoreEnv, fake: FakeS3) -> None:
    store = store_of(env)
    staged = store.stage_blob([CONTENT], max_bytes=len(CONTENT))
    fake.fail(403, "AccessDenied", method="PUT")
    assert refused(lambda: store.promote_blobs([staged])).code == "files.store_refused"
    fake.fail(500, "InternalError", method="HEAD")
    assert refused(lambda: store.promote_blobs([staged])).code == "backend.unavailable"
    staged.discard()


def test_no_secret_in_any_error_finding_or_log(
    env: StoreEnv, fake: FakeS3, clock: FixedClock, ticks: Ticks, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the fake's error bodies carry the access key id and the string to sign."""
    caplog.set_level(logging.DEBUG)
    env.write_blobs(lambda d: d.create(), [CONTENT])
    settings = env.object_store
    assert settings is not None
    store = store_of(env)
    seen: list[str] = []
    wrong = ObjectStoreCredentials(ACCESS_KEY_ID, "wrong/" + SECRET_ACCESS_KEY)
    reset_identity_cache()
    store.blob_backends = backends(replace(settings, credentials=wrong), clock, ticks)
    for touch in (lambda: store.blob(DIGEST), lambda: store.read_blob(DIGEST)):
        error = refused(touch)
        assert error.code == "backend.unavailable"
        assert "credentials" in error.message
        seen += [error.message, repr(error.details), repr(error), str(error.__cause__)]
    store.blob_backends = backends(settings, clock, ticks)
    fake.fail(403, "SignatureDoesNotMatch", method="HEAD")
    seen.append(repr(refused(lambda: store.blob(DIGEST)).details))
    findings = backend_of(env).verify(frozenset({DIGEST, digest_of(b"x")}), rehash=True).findings
    seen += [repr(f) for f in findings]
    seen.append(caplog.text)
    seen.append(repr(blob_manifest(store.layout)))
    text = "\n".join(seen)
    assert ACCESS_KEY_ID not in text
    assert SECRET_ACCESS_KEY not in text
    assert "AWS4-HMAC-SHA256" not in text
