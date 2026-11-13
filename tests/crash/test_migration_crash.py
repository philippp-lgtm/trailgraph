"""K: kill -9 at every point of a migration (plan M3, M4; contracts section 4.1).

The child writes the marker, appends the intent and commits a three-revision migration tx
(a type change, a renamed edge, a retracted entity), then removes the marker, with the real
syncer and SIGKILL at the named point. After the kill the state is either the old one or the
committed tx; the marker is present or absent as the point says; ``marker_committed`` follows
the log, never the head. Recovery then rolls a committed tx forward and the marker can be
removed.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from support.crash_runner import run_migration_child
from support.migrations import begin_migration
from support.stores import ALL_STATUSES, StoreEnv, snapshot_state

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import parse_json
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import EdgeRecord
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow

EVERYTHING = Query(record_status=ALL_STATUSES, limit=1000)
BASE_SEQ = 3
RENAMED = TermRef("area_a:renamed_link")


@dataclass(frozen=True)
class Row:
    """Expected durable state right after the kill."""

    marker: bool  # data/migration.json exists
    audit_events: int
    committed: bool
    revisions: str  # none | partial | all
    head_seq: int  # head.json
    recovers: bool  # recovery has something to do


ROWS = {
    CrashPoint.MIGRATION_MARKER_BEFORE_RENAME: Row(False, 3, False, "none", 3, False),
    CrashPoint.AUDIT_BEFORE_WRITE: Row(True, 3, False, "none", 3, False),
    CrashPoint.TX_BEFORE_WRITE: Row(True, 4, False, "none", 3, False),
    CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC: Row(True, 4, True, "none", 3, True),
    CrashPoint.TX_AFTER_SYNC: Row(True, 4, True, "none", 3, True),
    CrashPoint.MATERIALIZE_AFTER_FIRST_FILE: Row(True, 4, True, "partial", 3, True),
    CrashPoint.MATERIALIZE_BEFORE_DIR_SYNC: Row(True, 4, True, "all", 3, True),
    CrashPoint.HEAD_BEFORE_RENAME: Row(True, 4, True, "all", 3, True),
    CrashPoint.HEAD_AFTER_RENAME: Row(True, 4, True, "all", 4, False),
    CrashPoint.INDEX_BEFORE_RENAME: Row(True, 4, True, "all", 4, False),
    CrashPoint.INDEX_AFTER_RENAME: Row(True, 4, True, "all", 4, False),
    CrashPoint.MIGRATION_MARKER_BEFORE_REMOVE: Row(True, 4, True, "all", 4, False),
}


def prepare(
    tmp_path: Path,
) -> tuple[StoreEnv, AuthorizedTx, AuditEventDraft, MigrationMarker, tuple[EntityId, ...]]:
    """Three committed txs (three entities, a link, a retraction), then a migration tx built
    but not run, with its intent and marker."""
    env = StoreEnv("files", "inventory", tmp_path, real_audit=True)
    ids: list[EntityId] = []
    env.write(lambda d: ids.extend([d.create(), d.create(), d.create()]))
    env.write(lambda d: d.link(ids[0], "core:source", ids[1]))
    env.write(lambda d: d.retract(ids[2]))
    with env.store.session(timeout_s=5) as session:
        draft = begin_migration(env.builder, snapshot_state(session.snapshot()), session.head())
        current = {e: session.snapshot().get(e) for e in ids}
        source, other, retracted = (current[e] for e in ids)
        assert source is not None and other is not None and retracted is not None
        other_type = TermRef(next(c for c in env.builder.concrete_classes if c != other.type))
        draft.revise(
            ids[0],
            edges=tuple(
                EdgeRecord(e.edge_id, RENAMED, e.target, e.qualifiers) for e in source.edges
            ),
        )
        draft.revise(ids[1], type=other_type)
        draft.revise(ids[2], attrs={**retracted.attrs, "extra": "kept"})
        tx = draft.build()
        audit = env.intent(tx, session.snapshot())
    assert len(tx.revisions) == 3 and tx.seq == BASE_SEQ + 1
    marker = MigrationMarker(
        seq=tx.seq,
        tx_id=tx.tx_id,
        from_version="1.0.0",
        to_version="2.0.0",
        artifact_hash="sha256:" + "a" * 64,
        migration_hash="sha256:" + "b" * 64,
    )
    return env, tx, audit, marker, tuple(ids)


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


def kill(
    env: StoreEnv,
    store: FileStore,
    point: CrashPoint,
    tx: AuthorizedTx,
    audit: AuditEventDraft,
    marker: MigrationMarker,
) -> None:
    child = run_migration_child(
        store.layout.root,
        str(env.graph_id),
        point.value,
        tx=tx,
        audit_body=audit.body,
        marker=marker.to_json(),
    )
    assert child.returncode == -9, child.stderr


@pytest.mark.parametrize("point", list(ROWS), ids=str)
def test_kill_during_a_migration(tmp_path: Path, point: CrashPoint) -> None:
    env, tx, audit, marker, ids = prepare(tmp_path)
    row = ROWS[point]
    store = store_of(env)
    kill(env, store, point, tx, audit, marker)

    # durable state, inspected without the lock
    assert env.audit_head()[0] == row.audit_events
    scan = store.txlog.scan()
    assert scan.corruption is None and scan.tail == "clean"
    assert scan.end.seq == BASE_SEQ + row.committed
    assert {"none": 0, "partial": 1, "all": 3}[row.revisions] == revision_files(store, tx)
    assert head_seq(store) == row.head_seq
    assert store.layout.migration_marker.exists() is row.marker
    if point is CrashPoint.MIGRATION_MARKER_BEFORE_RENAME:
        assert store.layout.migration_marker.with_name("migration.json.tmp").exists()

    # a lock-free reader before recovery: old state or the committed tx, marker as the point says
    assert store.migration_marker() == (marker if row.marker else None)
    snapshot = store.snapshot()
    assert snapshot.seq == BASE_SEQ + row.committed
    for revision in tx.revisions:
        assert (snapshot.get(revision.id) == revision) is row.committed
    if row.marker:
        assert marker_committed(snapshot, marker) is row.committed
    replay = FileStore(store.layout.root, env.graph_id, use_index=False).snapshot()
    assert snapshot.warnings == replay.warnings
    assert snapshot.query(EVERYTHING).items == replay.query(EVERYTHING).items

    # recovery rolls a committed tx forward; the marker stays until it is removed
    with store.session(timeout_s=5) as session:
        assert session.recovery.recovered is row.recovers
    if row.committed:
        assert revision_files(store, tx) == 3
        assert head_seq(store) == BASE_SEQ + 1
        after = store.snapshot()
        assert [after.get(r.id) for r in tx.revisions] == list(tx.revisions)
    assert store.migration_marker() == (marker if row.marker else None)
    maintenance = FileStoreMaintenance(env.store.layout.root.parent.parent, syncer=env.syncer)
    with maintenance.maintenance_session(env.graph_id, timeout_s=5) as session:
        session.remove_migration_marker()
        session.remove_migration_marker()
    assert store.migration_marker() is None
    assert ids  # the entities of the scenario


def test_a_crash_before_the_commit_leaves_a_stale_marker_that_an_app_write_outlives(
    tmp_path: Path,
) -> None:
    """The marker is durable, the tx never committed; an application write takes the marker's
    seq. ``head.seq >= marker.seq`` would call the marker committed; identity does not."""
    env, tx, audit, marker, ids = prepare(tmp_path)
    store = store_of(env)
    kill(env, store, CrashPoint.TX_BEFORE_WRITE, tx, audit, marker)
    assert store.migration_marker() == marker
    assert not marker_committed(store.snapshot(), marker)
    written, _ = env.write(lambda d: d.update(ids[0]))
    assert written.seq == marker.seq and written.tx_id != marker.tx_id
    snapshot = store_of(env).snapshot()
    assert snapshot.seq >= marker.seq
    assert not marker_committed(snapshot, marker)
