"""K at store level: kill -9 at the blob crash points KB0-KB2 (section 4.1, files slice 1).

The child stages a file before the lock, publishes it under the lock, then
appends the intent and commits a tx that lists the blob. It sends itself
SIGKILL at the point. Nothing is committed at any of the three points; the
parent checks what is durable, what a reader sees, the blob findings, and
that a retry with the same idempotency key commits exactly once and leaves
no orphan.

The same for a graph whose stored files are in the fake object store (object storage
O6): KB0, KB0s (``blob.after_upload`` after the promote), KB1s (the same point after
the publish fallback, without a promote) and ``blob.before_staging_removal``. The object
is an orphan until the retry commits it.
"""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from support.audit_events import finding_codes_of, verify_graph_stream
from support.crash_runner import run_child
from support.fake_s3 import FakeS3
from support.s3_graphs import blob_key
from support.stores import StoreEnv, snapshot_state

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.storage import finding_codes
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.layout import STAGING_PREFIX
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow

KEY = "k-blob-crash"
CONTENT = b"%PDF-1.4\nsynthetic crash bytes\n"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()

# point -> (staging files left, blob stored, blob findings right after the kill)
ROWS = {
    CrashPoint.BLOB_AFTER_STAGE: (1, False, [finding_codes.BLOB_STAGING_LEFTOVER]),  # KB0
    CrashPoint.BLOB_BEFORE_RENAME: (1, False, [finding_codes.BLOB_STAGING_LEFTOVER]),  # KB1
    CrashPoint.BLOB_AFTER_RENAME: (0, True, [finding_codes.BLOB_ORPHAN]),  # KB2
}


def prepare(
    tmp_path: Path, fake: FakeS3 | None = None
) -> tuple[StoreEnv, AuthorizedTx, AuditEventDraft, Path]:
    env = StoreEnv(
        "s3" if fake is not None else "files",
        "inventory",
        tmp_path,
        real_audit=True,
        object_store=fake.settings() if fake is not None else None,
    )
    env.write(lambda d: d.create())
    with env.store.session(timeout_s=5) as session:
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        draft.create()
        tx = replace(draft.build(idempotency_key=KEY), blobs=(DIGEST,))
        audit = env.intent(tx, session.snapshot())
    source = tmp_path / "input file.pdf"
    source.write_bytes(CONTENT)
    return env, tx, audit, source


def staging_files(store: FileStore) -> int:
    blobs = store.layout.blobs
    return sum(p.name.startswith(STAGING_PREFIX) for p in blobs.iterdir()) if blobs.is_dir() else 0


def blob_codes(env: StoreEnv) -> list[str]:
    maintenance = FileStoreMaintenance(env.root, object_store=env.object_store)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        return sorted(f.code for f in session.verify_blobs(rehash=True).findings)


@pytest.mark.parametrize("point", list(ROWS), ids=str)
def test_blob_crash_point(tmp_path: Path, point: CrashPoint) -> None:
    env, tx, audit, source = prepare(tmp_path)
    leftovers, stored, findings = ROWS[point]
    assert isinstance(env.store, FileStore)
    store = FileStore(env.store.layout.root, env.graph_id)
    child = run_child(
        store.layout.root,
        str(env.graph_id),
        point.value,
        tx=tx,
        audit_body=audit.body,
        blob_file=source,
    )
    assert child.returncode == -9, child.stderr

    # durable state: no intent, no tx; the staged or stored bytes only
    assert env.audit_head()[0] == 1
    assert store.txlog.scan().end.seq == 1
    assert staging_files(store) == leftovers
    assert (store.blob(DIGEST) is not None) is stored
    assert store.snapshot().seq == 1
    assert blob_codes(env) == findings

    # the retry: stage again, publish, intent, commit once
    staged = store.stage_blob([source.read_bytes()], max_bytes=len(CONTENT))
    with store.session(timeout_s=5) as session:
        assert session.lookup_idempotency(KEY, tx.actor.principal.id or "") is None
        session.publish_blob(staged)
        session.commit(tx, session.append_audit(audit))
    records = store.changes_since(0)
    assert [r.seq for r in records] == [1, 2]
    assert records[-1].blobs == (DIGEST,)
    assert b"".join(store.read_blob(DIGEST)) == CONTENT
    # a leftover of KB0/KB1 stays until gc; KB2's orphan is referenced now
    assert blob_codes(env) == findings[:leftovers]
    assert staging_files(store) == leftovers
    assert finding_codes_of(verify_graph_stream(store, env.audit, env.graph_id)) == []


ORPHAN_AND_LEFTOVER = sorted([finding_codes.BLOB_ORPHAN, finding_codes.BLOB_STAGING_LEFTOVER])
# (point, promote) -> (object stored, blob findings right after the kill)
S3_ROWS = {
    (CrashPoint.BLOB_AFTER_STAGE, True): (False, [finding_codes.BLOB_STAGING_LEFTOVER]),  # KB0
    (CrashPoint.BLOB_AFTER_UPLOAD, True): (True, ORPHAN_AND_LEFTOVER),  # KB0s
    (CrashPoint.BLOB_AFTER_UPLOAD, False): (True, ORPHAN_AND_LEFTOVER),  # KB1s
    (CrashPoint.BLOB_BEFORE_STAGING_REMOVAL, True): (True, ORPHAN_AND_LEFTOVER),
}


@pytest.mark.parametrize(
    ("point", "promote"),
    list(S3_ROWS),
    ids=lambda v: str(v) if isinstance(v, CrashPoint) else ("promoted" if v else "fallback"),
)
def test_object_store_blob_crash_point(tmp_path: Path, point: CrashPoint, promote: bool) -> None:
    with FakeS3() as fake:
        env, tx, audit, source = prepare(tmp_path, fake)
        stored, findings = S3_ROWS[(point, promote)]
        assert isinstance(env.store, FileStore)
        store = env.store
        child = run_child(
            store.layout.root,
            str(env.graph_id),
            point.value,
            tx=tx,
            audit_body=audit.body,
            blob_file=source,
            object_store=env.object_store,
            promote=promote,
        )
        assert child.returncode == -9, child.stderr

        # durable state: no intent, no tx; the staging file and maybe the object
        assert env.audit_head()[0] == 1
        assert store.txlog.scan().end.seq == 1
        assert staging_files(store) == 1
        assert (blob_key(env.graph_id, DIGEST) in fake.objects) is stored
        assert store.snapshot().seq == 1
        assert blob_codes(env) == findings

        # the retry with the same key: stage, promote, publish, intent, commit once
        staged = store.stage_blob([source.read_bytes()], max_bytes=len(CONTENT))
        store.promote_blobs([staged])
        with store.session(timeout_s=5) as session:
            assert session.lookup_idempotency(KEY, tx.actor.principal.id or "") is None
            session.publish_blob(staged)
            session.commit(tx, session.append_audit(audit))
        records = store.changes_since(0)
        assert [r.seq for r in records] == [1, 2]
        assert [r.tx_id for r in records if r.idempotency_key == KEY] == [tx.tx_id]
        assert b"".join(store.read_blob(DIGEST)) == CONTENT
        assert (
            sum(
                r.method == "PUT" and r.key == blob_key(env.graph_id, DIGEST) for r in fake.requests
            )
            == 1
        )  # the retry found the object
        # the crash's staging file stays until gc; the object is referenced now
        assert blob_codes(env) == [finding_codes.BLOB_STAGING_LEFTOVER]
        assert finding_codes_of(verify_graph_stream(store, env.audit, env.graph_id)) == []
