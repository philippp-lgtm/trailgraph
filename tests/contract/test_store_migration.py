"""A migration tx and its marker on both backends (plan M3, M4; contracts B1.6, B8.3, B13.8)."""

from uuid import UUID

import pytest
from support.migrations import MigrationDraft, begin_migration
from support.stores import ALL_STATUSES, StoreEnv, snapshot_state
from support.txbuilder import TxDraft

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef, TxId
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.store import FileStore

EVERYTHING = Query(record_status=ALL_STATUSES, limit=10**9)
RENAMED = TermRef("area_a:renamed_link")


def _seed(env: StoreEnv) -> tuple[EntityId, EntityId, EntityId]:
    """A linked pair and a retracted entity."""
    ids: list[EntityId] = []

    def create(draft: TxDraft) -> None:
        ids.extend([draft.create(), draft.create(), draft.create()])

    env.write(create)
    env.write(lambda d: d.link(ids[0], "core:source", ids[1]))  # the commit checks no ontology
    env.write(lambda d: d.retract(ids[2]))
    return ids[0], ids[1], ids[2]


def _current(env: StoreEnv, entity: EntityId) -> Revision:
    revision = env.store.snapshot().get(entity)
    assert revision is not None
    return revision


def _marker(tx_id: UUID, seq: int) -> MigrationMarker:
    return MigrationMarker(
        seq=seq,
        tx_id=TxId(tx_id),
        from_version="1.0.0",
        to_version="2.0.0",
        artifact_hash="sha256:" + "a" * 64,
        migration_hash="sha256:" + "b" * 64,
    )


def test_a_migration_tx_commits_and_reads_back(env: StoreEnv) -> None:
    source, other, retracted = _seed(env)
    before = {e: _current(env, e) for e in (source, other, retracted)}
    target_type = TermRef(next(c for c in env.builder.concrete_classes if c != before[other].type))
    edges: tuple[EdgeRecord, ...] = tuple(
        EdgeRecord(e.edge_id, RENAMED, e.target, e.qualifiers) for e in before[source].edges
    )

    def edit(draft: MigrationDraft) -> None:
        draft.revise(source, edges=edges)
        draft.revise(other, type=target_type)
        draft.revise(retracted, attrs={**before[retracted].attrs, "extra": "kept"})

    tx, result = env.migrate(edit)
    assert result.seq == tx.seq and result.warnings == ()
    snapshot = env.store.snapshot()
    assert snapshot.seq == tx.seq
    assert snapshot.tx(tx.seq) is not None
    for revision in tx.revisions:
        assert snapshot.get(revision.id) == revision
        assert snapshot.get_revision(revision.id, revision.rev) == revision
        history = snapshot.history(revision.id)
        assert [m.rev for m in history] == list(range(1, revision.rev + 1))  # old revisions stay
    after = {e: _current(env, e) for e in before}
    assert after[other].type == target_type and before[other].type != target_type
    assert after[retracted].record_status == "retracted"  # a non-active entity is migrated too
    assert {e.rel for e in after[source].edges} == {RENAMED}
    target = after[source].edges[0].target
    assert [(s, e.rel) for s, e in snapshot.incoming(target, RENAMED)] == [(source, RENAMED)]
    assert snapshot.incoming(target, before[source].edges[0].rel) == []
    assert [r.id for r in snapshot.query(Query(types=frozenset({target_type}))).items] == [other]


def test_a_migrated_graph_agrees_across_index_replay_and_files(env: StoreEnv) -> None:
    source, other, _ = _seed(env)
    target_type = TermRef(
        next(c for c in env.builder.concrete_classes if c != _current(env, other).type)
    )
    tx, _ = env.migrate(lambda d: (d.revise(other, type=target_type), d.revise(source)))
    if not isinstance(env.store, FileStore):
        return
    root = env.store.layout.root
    indexed = FileStore(root, env.graph_id).snapshot()
    replay = FileStore(root, env.graph_id, use_index=False).snapshot()
    assert indexed.warnings == replay.warnings == ()
    assert indexed.query(EVERYTHING).items == replay.query(EVERYTHING).items
    for revision in tx.revisions:  # materialized, one file per revision
        assert env.store.read_revision(revision.id, revision.rev) == revision
    assert replay.get(other) in tx.revisions
    # a later ordinary write on the migrated state works and keeps everything consistent
    env.write(lambda d: d.update(source))
    fresh = FileStore(root, env.graph_id, use_index=False).snapshot()
    assert fresh.get(other) == FileStore(root, env.graph_id).snapshot().get(other)


def test_commit_refuses_a_migration_tx_and_changes_nothing(env: StoreEnv) -> None:
    source, *_ = _seed(env)
    with env.store.session(timeout_s=5) as session:
        draft = begin_migration(env.builder, snapshot_state(session.snapshot()), session.head())
        draft.revise(source)
        tx = draft.build()
        receipt = session.append_audit(env.intent(tx, session.snapshot()))
        with pytest.raises(GraphCliError) as info:
            session.commit(tx, receipt)
    assert info.value.details["rule"] == "migration_via_commit"
    assert env.store.snapshot().seq == tx.seq - 1


# ---- the marker -----------------------------------------------------------------------


def test_the_marker_is_written_read_and_removed(env: StoreEnv) -> None:
    assert env.store.migration_marker() is None
    marker = _marker(env.ids.new_id(), 1)
    with env.maintenance() as session:
        session.write_migration_marker(marker)
    assert env.store.migration_marker() == marker
    newer = _marker(env.ids.new_id(), 2)
    with env.maintenance() as session:
        session.write_migration_marker(newer)  # replaces
    assert env.store.migration_marker() == newer
    with env.maintenance() as session:
        session.remove_migration_marker()
        session.remove_migration_marker()  # idempotent
    assert env.store.migration_marker() is None


def test_a_marker_is_committed_by_identity(env: StoreEnv) -> None:
    source, *_ = _seed(env)
    head = env.store.head()
    # the migration tx is built and committed: its marker is committed
    tx, _ = env.migrate(lambda d: d.revise(source))
    committed = _marker(tx.tx_id, tx.seq)
    assert marker_committed(env.store.snapshot(), committed)
    # a marker beyond the head, or for another tx at an existing seq, is not
    assert not marker_committed(env.store.snapshot(), _marker(tx.tx_id, tx.seq + 1))
    assert not marker_committed(env.store.snapshot(), _marker(env.ids.new_id(), head.seq))
    assert not marker_committed(env.store.snapshot(), _marker(tx.tx_id, head.seq))


def test_a_stale_marker_does_not_look_committed_after_an_app_write(env: StoreEnv) -> None:
    """The marker was written, the migration tx never committed, then an application write took
    the marker's seq: ``head.seq >= marker.seq`` would call it committed, identity does not."""
    source, *_ = _seed(env)
    head = env.store.head()
    stale = _marker(env.ids.new_id(), head.seq + 1)
    with env.maintenance() as session:
        session.write_migration_marker(stale)
    assert not marker_committed(env.store.snapshot(), stale)  # not yet in the log
    write_tx, _ = env.write(lambda d: d.update(source))
    assert write_tx.seq == stale.seq
    snapshot = env.store.snapshot()
    assert snapshot.seq >= stale.seq
    assert not marker_committed(snapshot, stale)


@pytest.mark.parametrize(
    ("point", "marker_after"),
    [
        (CrashPoint.MIGRATION_MARKER_BEFORE_RENAME, False),
        (CrashPoint.MIGRATION_MARKER_BEFORE_REMOVE, True),
    ],
)
def test_a_failing_marker_step_leaves_the_old_marker_state(
    env: StoreEnv, point: CrashPoint, marker_after: bool
) -> None:
    marker = _marker(env.ids.new_id(), 1)
    if marker_after:
        with env.maintenance() as session:
            session.write_migration_marker(marker)
    env.faults.armed = {point}
    with env.maintenance() as session, pytest.raises(GraphCliError) as info:
        if marker_after:
            session.remove_migration_marker()
        else:
            session.write_migration_marker(marker)
    assert info.value.code == "backend.unavailable"
    assert (env.store.migration_marker() == marker) is marker_after
    env.faults.armed = set()
    with env.maintenance() as session:  # the retry works
        if marker_after:
            session.remove_migration_marker()
        else:
            session.write_migration_marker(marker)
    assert (env.store.migration_marker() == marker) is not marker_after


def test_a_marker_leftover_temp_file_is_overwritten(env: StoreEnv) -> None:
    if not isinstance(env.store, FileStore):
        pytest.skip("files only")
    env.faults.armed = {CrashPoint.MIGRATION_MARKER_BEFORE_RENAME}
    marker = _marker(env.ids.new_id(), 1)
    with env.maintenance() as session, pytest.raises(GraphCliError):
        session.write_migration_marker(marker)
    leftover = env.store.layout.migration_marker.with_name("migration.json.tmp")
    assert leftover.exists() and env.store.migration_marker() is None
    env.faults.armed = set()
    with env.maintenance() as session:
        session.write_migration_marker(marker)
    assert not leftover.exists() and env.store.migration_marker() == marker


def test_an_unreadable_marker_is_backend_corrupt(env: StoreEnv) -> None:
    if not isinstance(env.store, FileStore):
        pytest.skip("files only")
    for garbage in (b"not json", b"[]", b'{"schema": "graph_cli.migration_marker/1"}', b""):
        env.store.layout.migration_marker.write_bytes(garbage)
        with pytest.raises(GraphCliError) as info:
            env.store.migration_marker()
        assert info.value.code == "backend.corrupt"
