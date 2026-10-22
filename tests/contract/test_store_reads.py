"""Read port contract (contracts B4.6, B4.9, B4.10, B8.8 seam, B13.5, B13.6)."""

import itertools

import pytest
from support.stores import ALL_STATUSES, StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.storage import EMPTY_HEAD
from graph_cli.kernel.query_ast import Cmp, NeighborQuery, Not, OrderKey, Query, RelConstraint
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.records import revision_meta
from graph_cli.storage.common.view import GraphView


def test_empty_store(env: StoreEnv) -> None:
    store = env.store
    expected = {"snapshot_at_seq"} | ({"blob_local_path"} if env.backend == "files" else set())
    assert store.capabilities() == expected
    assert store.head() == EMPTY_HEAD
    snapshot = store.snapshot()
    assert snapshot.seq == 0
    assert snapshot.warnings == ()
    assert snapshot.query(Query()).total == 0
    assert store.changes_since(0) == ()


def test_commit_is_readable(env: StoreEnv) -> None:
    created: list[EntityId] = []
    tx, result = env.write(lambda d: created.append(d.create()))
    head = env.store.head()
    assert (head.seq, head.tx_id, head.hash, head.recorded_at) == (
        1,
        tx.tx_id,
        result.hash,
        tx.recorded_at,
    )
    assert (result.seq, result.tx_id, result.warnings) == (1, tx.tx_id, ())
    (revision,) = tx.revisions
    snapshot = env.store.snapshot()
    assert snapshot.get(created[0]) == revision
    assert snapshot.get_many([created[0]]) == {created[0]: revision}
    assert snapshot.get_revision(created[0], 1) == revision
    assert snapshot.get_revision(created[0], 2) is None
    assert list(snapshot.history(created[0])) == [revision_meta(revision)]
    (record,) = env.store.changes_since(0)
    assert (record.seq, record.tx_id, record.hash, record.revisions) == (
        1,
        tx.tx_id,
        result.hash,
        tx.revisions,
    )


def test_updates_build_history(env: StoreEnv) -> None:
    created: list[EntityId] = []
    env.write(lambda d: created.append(d.create()))
    entity = created[0]
    for _ in range(3):
        env.write(lambda d: d.update(entity))
    snapshot = env.store.snapshot()
    assert [m.rev for m in snapshot.history(entity)] == [1, 2, 3, 4]
    assert [m.tx_seq for m in snapshot.history(entity)] == [1, 2, 3, 4]
    current = snapshot.get(entity)
    assert current is not None and current.rev == 4 and current.prev_rev == 3
    assert snapshot.get_revision(entity, 2).rev == 2  # type: ignore[union-attr]


def test_snapshot_tx_is_bounded_by_its_seq(env: StoreEnv) -> None:
    """B8.15: a snapshot answers for its own txs only, also with a tail after the checkpoint."""
    first, _ = env.write(lambda d: d.create())
    second, _ = env.write(lambda d: d.create())
    snapshot = env.store.snapshot(at_seq=1)
    found = snapshot.tx(1)
    assert found is not None and (found.seq, found.tx_id) == (1, first.tx_id)
    assert [snapshot.tx(seq) for seq in (0, 2, -1)] == [None, None, None]
    later = env.store.snapshot().tx(2)
    assert later is not None and later.tx_id == second.tx_id


def test_snapshot_isolation(env: StoreEnv) -> None:
    created: list[EntityId] = []
    env.write(lambda d: created.append(d.create()))
    before = env.store.snapshot()
    env.write(lambda d: d.update(created[0]))
    env.write(lambda d: created.append(d.create()))
    assert before.seq == 1
    assert before.get(created[0]).rev == 1  # type: ignore[union-attr]
    assert before.get(created[1]) is None
    assert [m.rev for m in before.history(created[0])] == [1]
    assert before.query(Query()).total == 1
    assert env.store.snapshot().query(Query()).total == 2


def test_snapshot_at_seq_equals_state_after_n_txs(env: StoreEnv) -> None:
    states = []
    for _ in range(12):
        env.random(1)
        snapshot = env.store.snapshot()
        states.append(snapshot.query(Query(record_status=ALL_STATUSES, limit=10**6)).items)
    for seq, items in enumerate(states, start=1):
        at = env.store.snapshot(at_seq=seq)
        assert at.seq == seq
        assert at.query(Query(record_status=ALL_STATUSES, limit=10**6)).items == items
    assert env.store.snapshot(at_seq=0).query(Query()).total == 0


@pytest.mark.parametrize("at_seq", [-1, 2])
def test_snapshot_outside_the_log(env: StoreEnv, at_seq: int) -> None:
    env.write(lambda d: d.create())
    with pytest.raises(GraphCliError) as info:
        env.store.snapshot(at_seq=at_seq)
    assert info.value.code == "usage.invalid_argument"


def test_incoming_aliases_and_suffix(env: StoreEnv) -> None:
    ids: dict[str, EntityId] = {}

    def setup(d: TxDraft) -> None:
        cls = next(c for c in env.builder.concrete_classes if env.builder.prefix_of(c))
        ids["target"] = d.create(cls)
        ids["source"] = d.create()
        d.link(ids["source"], "core:source", ids["target"])

    env.write(setup)
    snapshot = env.store.snapshot()
    ((source, edge),) = snapshot.incoming(ids["target"])
    assert source == ids["source"] and edge.rel == "core:source"
    assert snapshot.incoming(ids["target"], "core:supersedes") == []  # type: ignore[arg-type]
    alias = snapshot.get(ids["target"]).alias  # type: ignore[union-attr]
    assert alias is not None
    assert snapshot.resolve_alias(alias) == (ids["target"], True)
    assert snapshot.match_suffix(ids["target"].hex[-10:]) == [ids["target"]]
    renamed: list[str] = []
    env.write(lambda d: renamed.append(d.set_alias(ids["target"])))
    after = env.store.snapshot()
    assert after.resolve_alias(alias) == (ids["target"], False)
    assert after.resolve_alias(renamed[0]) == (ids["target"], True)
    assert snapshot.resolve_alias(renamed[0]) is None


def _queries(env: StoreEnv) -> list[Query]:
    model = env.builder.model
    some_type = env.builder.concrete_classes[0]
    texts = [env.builder.text_attribute(c) for c in env.builder.concrete_classes]
    text = next((t for t in texts if t), "name")
    rel = next(r for r in sorted(model.relations) if not r.startswith("core:"))
    return [
        Query(limit=1000),
        Query(types=(some_type,), limit=1000),  # type: ignore[arg-type]
        Query(record_status=ALL_STATUSES, order=(OrderKey("type"), OrderKey("rev", True))),
        Query(where=Cmp(text, "prefix", "updated"), record_status=ALL_STATUSES, limit=1000),
        Query(where=Not(Cmp(text, "exists")), limit=7, offset=2),
        Query(rels=(RelConstraint(rel, "out"),), limit=1000),  # type: ignore[arg-type]
        Query(rels=(RelConstraint(rel, "in"),), order=(OrderKey(text, True),)),  # type: ignore[arg-type]
        Query(changed_since_seq=10, record_status=ALL_STATUSES, limit=1000),
        Query(order=(OrderKey("alias"),), limit=5, offset=5),
    ]


def test_query_results_match_the_reference_evaluator(env: StoreEnv) -> None:
    env.random(40)
    reference = GraphView()
    for record in env.store.changes_since(0, limit=10**6):
        reference.apply(record)
    for seq in (20, env.store.head().seq):
        snapshot = env.store.snapshot(at_seq=seq)
        expected_view = reference.at(seq)
        for query in _queries(env):
            assert snapshot.query(query) == run_query(expected_view, query), query
        for entity in expected_view.entity_ids()[:10]:
            for direction in ("out", "in", "both"):
                q = NeighborQuery(entity, None, direction, depth=2)  # type: ignore[arg-type]
                assert list(snapshot.neighbors(q)) == run_neighbors(expected_view, q)
            assert list(snapshot.incoming(entity)) == expected_view.incoming(entity)
            assert list(snapshot.history(entity)) == expected_view.history(entity)


def test_changes_since_pages(env: StoreEnv) -> None:
    env.random(9)
    store = env.store
    assert [tx.seq for tx in store.changes_since(0, limit=4)] == [1, 2, 3, 4]
    assert [tx.seq for tx in store.changes_since(4, limit=4)] == [5, 6, 7, 8]
    assert [tx.seq for tx in store.changes_since(8, limit=4)] == [9]
    assert store.changes_since(9) == ()
    assert [tx.seq for tx in store.changes_since(0)] == list(range(1, 10))
    records = store.changes_since(0)
    assert all(b.prev_hash == a.hash for a, b in itertools.pairwise(records))
