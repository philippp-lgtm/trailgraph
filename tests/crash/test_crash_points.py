"""K: kill -9 at every named crash point (contracts section 4.1, 4.2, K.a-K.d).

The child commits a three-revision tx with the real syncer and sends itself
SIGKILL at the point. The parent then checks the durable state lock-free,
what a reader sees, the state after recovery, and that a retry with the
same idempotency key commits exactly once (storage side of K.a). The audit
verifier gives the table's verify column, after the crash and after the
retry. kill -9 keeps the page cache, so a line written before the kill is
complete; torn lines are proven by the tamper tests (K.b).
"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from support.audit_events import finding_codes_of, verify_graph_stream
from support.crash_runner import run_child
from support.stores import ALL_STATUSES, StoreEnv, snapshot_state

from graph_cli.audit.finding_codes import INTENT_WITHOUT_EFFECT
from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import parse_json
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.common.faults import MAINT_POINTS, CrashPoint
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow

KEY = "k-crash"
EVERYTHING = Query(record_status=ALL_STATUSES, limit=1000)
BASE_SEQ = 2


@dataclass(frozen=True)
class Row:
    """Expected durable state right after the kill (section 4.1 table, kill -9 column)."""

    audit_events: int
    committed: bool
    revisions: str  # none | partial | all
    head_seq: int
    recovers: bool  # recovery has something to do
    verify: tuple[str, ...]  # audit verify findings: none, or the benign orphan intent


CLEAN: tuple[str, ...] = ()
ORPHAN = (INTENT_WITHOUT_EFFECT,)
ROWS = {
    CrashPoint.AUDIT_BEFORE_WRITE: Row(2, False, "none", 2, False, CLEAN),  # K0
    CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC: Row(3, False, "none", 2, False, ORPHAN),  # K1
    CrashPoint.AUDIT_AFTER_SYNC: Row(3, False, "none", 2, False, ORPHAN),  # K2
    CrashPoint.TX_BEFORE_WRITE: Row(3, False, "none", 2, False, ORPHAN),  # K2 (extra point)
    CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC: Row(3, True, "none", 2, True, CLEAN),  # K2, K.e
    CrashPoint.TX_AFTER_SYNC: Row(3, True, "none", 2, True, CLEAN),  # K3
    CrashPoint.MATERIALIZE_AFTER_FIRST_FILE: Row(3, True, "partial", 2, True, CLEAN),  # K3
    CrashPoint.MATERIALIZE_BEFORE_DIR_SYNC: Row(3, True, "all", 2, True, CLEAN),  # K3
    CrashPoint.HEAD_BEFORE_RENAME: Row(3, True, "all", 2, True, CLEAN),  # K4
    CrashPoint.HEAD_AFTER_RENAME: Row(3, True, "all", 3, False, CLEAN),  # K5
    CrashPoint.INDEX_BEFORE_RENAME: Row(3, True, "all", 3, False, CLEAN),  # K5, index behind
    CrashPoint.INDEX_AFTER_RENAME: Row(3, True, "all", 3, False, CLEAN),  # K5
}


def prepare(tmp_path: Path) -> tuple[StoreEnv, AuthorizedTx, AuditEventDraft]:
    """Two committed txs, then a third tx (two creates and one update) built but not run,
    with its write intent."""
    env = StoreEnv("files", "inventory", tmp_path, real_audit=True)
    created: list[EntityId] = []
    env.write(lambda d: created.append(d.create()))
    env.write(lambda d: d.create())
    with env.store.session(timeout_s=5) as session:
        draft = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        draft.create()
        draft.create()
        draft.update(created[0])
        tx = draft.build(idempotency_key=KEY)
        audit = env.intent(tx, session.snapshot())
    assert len(tx.revisions) == 3
    return env, tx, audit


def store_of(env: StoreEnv) -> FileStore:
    assert isinstance(env.store, FileStore)
    return FileStore(env.store.layout.root, env.graph_id)


def head_seq(store: FileStore) -> int:
    document = parse_json(store.layout.head.read_bytes())
    assert isinstance(document, dict)
    seq = document["seq"]
    assert isinstance(seq, int)
    return seq


def revision_files(store: FileStore, tx: AuthorizedTx) -> int:
    return sum(store.layout.revision_path(r.id, r.rev).exists() for r in tx.revisions)


def retry(store: FileStore, tx: AuthorizedTx, audit: AuditEventDraft, committed: bool) -> None:
    with store.session(timeout_s=5) as session:
        found = session.lookup_idempotency(KEY, tx.actor.principal.id or "")
        if committed:
            assert found is not None and found.tx_id == tx.tx_id
            return
        assert found is None
        # The services mint a fresh event id for the retry (W.9); the store only
        # needs the receipt to match the tx.
        session.commit(tx, session.append_audit(audit))


def audit_verify(env: StoreEnv, store: FileStore) -> tuple[str, ...]:
    return tuple(finding_codes_of(verify_graph_stream(store, env.audit, env.graph_id)))


@pytest.mark.parametrize("point", list(ROWS), ids=str)
def test_crash_point(tmp_path: Path, point: CrashPoint) -> None:
    env, tx, audit = prepare(tmp_path)
    row = ROWS[point]
    store = store_of(env)
    child = run_child(
        store.layout.root, str(env.graph_id), point.value, tx=tx, audit_body=audit.body
    )
    assert child.returncode == -9, child.stderr

    # durable state, inspected without the lock
    assert env.audit_head()[0] == row.audit_events
    tx_scan = store.txlog.scan()
    assert tx_scan.corruption is None and tx_scan.tail == "clean"
    assert tx_scan.end.seq == BASE_SEQ + row.committed
    files = revision_files(store, tx)
    assert files == {"none": 0, "partial": 1, "all": 3}[row.revisions]
    assert head_seq(store) == row.head_seq
    if point is CrashPoint.HEAD_BEFORE_RENAME:
        assert store.layout.head.with_name("head.json.tmp").exists()
    if point is CrashPoint.INDEX_BEFORE_RENAME:
        assert store.layout.index.with_name("index.sqlite.tmp").exists()
    assert audit_verify(env, store) == row.verify

    # a lock-free reader before recovery
    snapshot = store.snapshot()
    assert snapshot.seq == BASE_SEQ + row.committed
    for revision in tx.revisions:
        assert (snapshot.get_revision(revision.id, revision.rev) == revision) is row.committed
    replay = FileStore(store.layout.root, env.graph_id, use_index=False).snapshot()
    assert snapshot.warnings == replay.warnings
    assert snapshot.query(EVERYTHING).items == replay.query(EVERYTHING).items

    # recovery at session start, then the retry
    with store.session(timeout_s=5) as session:
        assert session.recovery.recovered is row.recovers
    if row.committed:
        assert revision_files(store, tx) == 3
        assert head_seq(store) == BASE_SEQ + 1
    retry(store, tx, audit, row.committed)
    records = store.changes_since(0)
    assert [r.seq for r in records] == [1, 2, 3]
    assert [r.tx_id for r in records if r.idempotency_key == KEY] == [tx.tx_id]
    assert env.audit_head()[0] == row.audit_events + (not row.committed)
    assert head_seq(store) == 3
    assert audit_verify(env, store) == row.verify


def interrupted_recovery(tmp_path: Path) -> tuple[StoreEnv, FileStore, AuthorizedTx]:
    """A committed tx whose files were deferred, plus torn tails on both logs."""
    env, tx, audit = prepare(tmp_path)
    env.faults.armed = {CrashPoint.TX_AFTER_SYNC}
    with env.store.session(timeout_s=5) as session:
        result = session.commit(tx, session.append_audit(audit))
    env.faults.armed = set()
    assert [w.code for w in result.warnings] == ["storage.materialization_deferred"]
    store = store_of(env)
    for log in (store.txlog, store.auditlog):
        (last,) = log.segments()[-1:]
        with open(log.path(last[1]), "ab") as handle:
            handle.write(b'{"torn": tr')
    return env, store, tx


def test_crash_after_recovery_truncate(tmp_path: Path) -> None:
    env, store, tx = interrupted_recovery(tmp_path)
    child = run_child(store.layout.root, str(env.graph_id), "recovery.after_truncate")
    assert child.returncode == -9, child.stderr
    assert store.txlog.scan().tail == "clean"
    assert store.auditlog.scan().tail == "clean"
    assert revision_files(store, tx) == 0
    assert head_seq(store) == BASE_SEQ
    with store.session(timeout_s=5) as session:
        steps = [s["event"] for s in session.recovery.details["steps"]]  # type: ignore[union-attr]
    assert steps == ["revisions_materialized", "head_rewritten"]
    assert revision_files(store, tx) == 3 and head_seq(store) == 3


def test_crash_after_recovery_materialize(tmp_path: Path) -> None:
    env, store, tx = interrupted_recovery(tmp_path)
    child = run_child(store.layout.root, str(env.graph_id), "recovery.after_materialize")
    assert child.returncode == -9, child.stderr
    assert revision_files(store, tx) == 3
    assert head_seq(store) == BASE_SEQ
    before = {p: p.read_bytes() for p in store.layout.entities.rglob("r*.json")}
    with store.session(timeout_s=5) as session:
        steps = [s["event"] for s in session.recovery.details["steps"]]  # type: ignore[union-attr]
    assert steps == ["head_rewritten"]
    assert {p: p.read_bytes() for p in store.layout.entities.rglob("r*.json")} == before
    assert head_seq(store) == 3


def test_kill_leaves_no_stale_lock(tmp_path: Path) -> None:
    """W.c: the OS releases the lock of a killed writer."""
    env, tx, audit = prepare(tmp_path)
    store = store_of(env)
    child = run_child(
        store.layout.root,
        str(env.graph_id),
        CrashPoint.TX_AFTER_SYNC.value,
        tx=tx,
        audit_body=audit.body,
    )
    assert child.returncode == -9
    assert store.layout.lock.exists()
    with store.session(timeout_s=0.5):
        pass
    assert os.path.exists(store.layout.lock)


def test_every_write_path_point_is_covered() -> None:
    """Every named point outside the maintenance effects is killed at by a crash test: the
    commit points here, the blob points (local and object store) in ``test_blob_crash``
    and ``test_file_add_crash``, the recovery points above."""
    from crash import test_blob_crash, test_file_add_crash

    covered = (
        set(ROWS)
        | set(test_blob_crash.ROWS)
        | {point for point, _ in test_blob_crash.S3_ROWS}
        | set(test_file_add_crash.ROWS)
        | set(test_file_add_crash.S3_ROWS)
        | {CrashPoint.RECOVERY_AFTER_TRUNCATE, CrashPoint.RECOVERY_AFTER_MATERIALIZE}
    )
    assert set(CrashPoint) - MAINT_POINTS - covered == set()
