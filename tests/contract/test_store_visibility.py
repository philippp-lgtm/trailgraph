"""Type visibility in query and neighbor reads on every store (contracts B13.5; stage 3 V5).

The memory store, the file store with and without the SQLite index, the replay view at an
earlier seq (``--as-of``) and the reference evaluator must agree with each other and with
explicit expectations on a small graph of two types.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import pytest
from support.stores import StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import NeighborQuery, OrderKey, Query, RelConstraint
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.records import revision_content_hash
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files.index import IndexedView
from graph_cli.storage.files.store import FileSnapshot, FileStore

REL = TermRef("core:source")
ALL_STATUSES = frozenset({"active", "superseded", "retracted"})
HEAD = 3


@dataclass
class World:
    """``A*`` are of the visible type, ``B*`` of the hidden one."""

    env: StoreEnv
    ids: dict[str, EntityId]
    a: TermRef
    b: TermRef

    def names(self, entities: list[EntityId]) -> list[str]:
        by_id = {entity: name for name, entity in self.ids.items()}
        return sorted(by_id[entity] for entity in entities)

    def ref(self, *names: str) -> tuple[EntityId, ...]:
        return tuple(self.ids[name] for name in names)


@pytest.fixture
def world(env: StoreEnv) -> World:
    """A1 -> B1; A2 -> A3, B1; A3 -> B2; A4 -> A6; A5 -> B1; B1 -> A1; B2 -> A2.

    Three txs: everything; A6 retracted; A1 updated (a second revision).
    """
    classes = env.builder.concrete_classes
    assert len(classes) >= 2
    a, b = TermRef(classes[0]), TermRef(classes[1])
    ids: dict[str, EntityId] = {}

    def build(d: TxDraft) -> None:
        for name in ("A1", "A2", "A3", "A4", "A5", "A6"):
            ids[name] = d.create(a)
        for name in ("B1", "B2"):
            ids[name] = d.create(b)
        for source, target in (
            ("A1", "B1"),
            ("A2", "A3"),
            ("A2", "B1"),
            ("A3", "B2"),
            ("A4", "A6"),
            ("A5", "B1"),
            ("B1", "A1"),
            ("B2", "A2"),
        ):
            d.link(ids[source], REL, ids[target])

    env.write(build)
    env.write(lambda d: d.retract(ids["A6"]))
    env.write(lambda d: d.update(ids["A1"]))
    return World(env, ids, a, b)


SnapshotAt = Callable[[int], Snapshot]


def readers(world: World) -> list[tuple[str, SnapshotAt]]:
    """Every way to read the graph at a seq."""
    env = world.env
    found: list[tuple[str, SnapshotAt]] = [
        (env.backend, lambda seq: env.store.snapshot(at_seq=seq))
    ]
    if env.backend == "files":
        assert isinstance(env.store, FileStore)
        root = env.store.layout.root
        found.append(
            (
                "files without the index",
                lambda seq: FileStore(root, env.graph_id, use_index=False).snapshot(at_seq=seq),
            )
        )
    return found


def reference_of(world: World) -> GraphView:
    reference = GraphView()
    for record in world.env.store.changes_since(0, limit=10**6):
        reference.apply(record)
    return reference


def test_files_are_read_through_the_index_and_without_it(world: World) -> None:
    if world.env.backend != "files":
        pytest.skip("files only")
    snapshot = world.env.store.snapshot()
    assert isinstance(snapshot, FileSnapshot)
    assert isinstance(snapshot._index, IndexedView)  # the SQLite pushdown is on this path
    assert isinstance(world.env.store, FileStore)
    plain = FileStore(world.env.store.layout.root, world.env.graph_id, use_index=False)
    assert plain.snapshot()._index is None  # type: ignore[attr-defined]
    assert [name for name, _ in readers(world)] == ["files", "files without the index"]


def query_cases(world: World) -> dict[str, tuple[Query, list[str]]]:
    """Name -> (query, the names it returns at the head)."""
    only_a, only_b = frozenset({world.a}), frozenset({world.b})
    ref = world.ref

    def rel(direction: str, *targets: str, exists: bool = True) -> tuple[RelConstraint, ...]:
        return (RelConstraint(REL, direction, ref(*targets), exists),)  # type: ignore[arg-type]

    a_active = ["A1", "A2", "A3", "A4", "A5"]  # A6 is retracted, not in the default status set
    return {
        "subjects": (Query(visible=only_a), a_active),
        "subjects of the other type": (Query(visible=only_b), ["B1", "B2"]),
        "all statuses": (Query(record_status=ALL_STATUSES, visible=only_a), [*a_active, "A6"]),
        "types inside": (Query(types=(world.a, world.b), visible=only_a), a_active),
        "types outside": (Query(types=(world.b,), visible=only_a), []),
        "nothing visible": (Query(visible=frozenset()), []),
        "unknown type": (Query(visible=frozenset({TermRef("x:Unknown")})), []),
        "out exists": (Query(rels=rel("out"), visible=only_a), ["A2", "A4"]),
        "out absent": (Query(rels=rel("out", exists=False), visible=only_a), ["A1", "A3", "A5"]),
        "out hidden target": (Query(rels=rel("out", "B1"), visible=only_a), []),
        "out hidden target absent": (
            Query(rels=rel("out", "B1", exists=False), visible=only_a),
            a_active,
        ),
        "out visible target": (Query(rels=rel("out", "A3"), visible=only_a), ["A2"]),
        "out retracted target": (Query(rels=rel("out", "A6"), visible=only_a), ["A4"]),
        "in exists": (Query(rels=rel("in"), visible=only_a), ["A3"]),
        "in absent": (
            Query(rels=rel("in", exists=False), visible=only_a),
            ["A1", "A2", "A4", "A5"],
        ),
        "in hidden source": (Query(rels=rel("in", "B1"), visible=only_a), []),
        "in visible source": (Query(rels=rel("in", "A2"), visible=only_a), ["A3"]),
        "other type out": (Query(rels=rel("out"), visible=only_b), []),
        "other type absent": (Query(rels=rel("out", exists=False), visible=only_b), ["B1", "B2"]),
    }


def test_visible_queries_on_every_store(world: World) -> None:
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        for case, (query, expected) in query_cases(world).items():
            page = snapshot.query(query)
            assert world.names([r.id for r in page.items]) == expected, (label, case)
            assert page.total == len(expected), (label, case)


def test_visible_queries_equal_the_reference_at_every_seq(world: World) -> None:
    reference = reference_of(world)
    cases = [query for query, _ in query_cases(world).values()]
    cases += [
        Query(visible=frozenset({world.a}), limit=2, offset=1, order=(OrderKey("id"),)),
        Query(visible=frozenset({world.a}), limit=2, offset=4, order=(OrderKey("id", True),)),
        Query(visible=None, rels=(RelConstraint(REL, "in"),), record_status=ALL_STATUSES),
    ]
    for label, snapshot_at in readers(world):
        for seq in range(1, HEAD + 1):
            snapshot, view = snapshot_at(seq), reference.at(seq)
            for query in cases:
                assert snapshot.query(query) == run_query(view, query), (label, seq, query)


def test_visible_total_follows_paging_on_every_store(world: World) -> None:
    only_a = frozenset({world.a})
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        ordered = snapshot.query(Query(visible=only_a)).items
        page = snapshot.query(Query(visible=only_a, limit=2, offset=1))
        assert page.items == ordered[1:3], label
        assert (page.total, page.limit, page.offset) == (5, 2, 1)
        assert snapshot.query(Query(visible=only_a, offset=9)).items == ()
        assert snapshot.query(Query(visible=only_a, offset=9)).total == 5
        assert snapshot.query(Query(limit=2, offset=1)).total == 7


def test_returned_revisions_lose_their_hidden_edges(world: World) -> None:
    only_a = frozenset({world.a})
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        raw = {r.id: r for r in snapshot.query(Query()).items}
        page = snapshot.query(Query(visible=only_a))
        stripped = {world.names([r.id])[0]: r for r in page.items}
        assert stripped["A1"].edges == ()  # its only edge goes to B1
        assert [e.target for e in stripped["A2"].edges] == [world.ids["A3"]]
        assert stripped["A2"].edges == tuple(
            e for e in raw[world.ids["A2"]].edges if e.target == world.ids["A3"]
        )
        assert stripped["A4"] == raw[world.ids["A4"]]  # an edge to a retracted visible entity stays
        for name, revision in stripped.items():
            before = raw[world.ids[name]]
            assert (revision.rev, revision.tx_seq, revision.attrs) == (
                before.rev,
                before.tx_seq,
                before.attrs,
            ), (label, name)
            assert revision.content_hash == revision_content_hash(revision), (label, name)
        assert stripped["A1"].rev == 2 and stripped["A1"].tx_seq == HEAD
        # The unlimited query hands the stored revisions out unchanged.
        assert snapshot.query(Query()).items == tuple(raw.values())
        # Stripping follows paging: the page holds the same entities as without it.
        assert snapshot.query(Query(visible=only_a, limit=2)).items == page.items[:2]


def neighbor_cases(world: World) -> list[tuple[str, str, int, str, list[str]]]:
    """(start, direction, depth, visible type: a, b or none, names reached)."""
    return [
        ("A5", "out", 2, "none", ["A1", "B1"]),
        ("A5", "out", 2, "a", []),  # A1 lies behind hidden B1
        ("A2", "out", 3, "a", ["A3"]),  # B1 and (behind A3) B2 are hidden
        ("A2", "out", 3, "none", ["A1", "A3", "B1", "B2"]),
        ("A4", "out", 1, "a", ["A6"]),  # a retracted entity of a visible type is visible
        ("A3", "in", 3, "a", ["A2"]),  # the only source of A3 is A2; B2 -> A2 is not walked
        ("A1", "in", 1, "a", []),
        ("A1", "in", 2, "none", ["A2", "A5", "B1"]),
        ("A1", "in", 2, "a", []),  # A2 and A5 lie behind hidden B1
        ("A2", "both", 1, "a", ["A3"]),
        ("A2", "both", 1, "none", ["A3", "B1", "B2"]),
        ("A3", "both", 3, "a", ["A2"]),
        ("B1", "both", 3, "a", []),  # a hidden start
        ("B1", "out", 2, "b", []),  # B1 -> A1 and A1 is hidden for this reader
        ("B1", "both", 3, "b", []),
    ]


def test_visible_neighbors_on_every_store(world: World) -> None:
    visible_of = {"a": frozenset({world.a}), "b": frozenset({world.b}), "none": None}
    reference = reference_of(world)
    for label, snapshot_at in readers(world):
        for seq in (HEAD, 2, 1):
            snapshot, view = snapshot_at(seq), reference.at(seq)
            for start, direction, depth, kind, expected in neighbor_cases(world):
                q = NeighborQuery(
                    world.ids[start],
                    None,
                    direction,  # type: ignore[arg-type]
                    depth,
                    visible_of[kind],
                )
                found = list(snapshot.neighbors(q))
                assert found == run_neighbors(view, q), (label, seq, start, direction, depth)
                if seq == HEAD:
                    names = world.names([entity for entity, _, _ in found])
                    assert names == expected, (label, start, direction, depth, kind)


def test_neighbors_of_a_missing_start_and_stripped_revisions(world: World) -> None:
    only_a = frozenset({world.a})
    missing = EntityId(UUID(int=7))
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        assert snapshot.neighbors(NeighborQuery(missing, None, "both", 3, only_a)) == []
        found = snapshot.neighbors(NeighborQuery(world.ids["A5"], None, "both", 1, only_a))
        assert found == []
        ((entity, _, revision),) = snapshot.neighbors(
            NeighborQuery(world.ids["A2"], None, "both", 1, only_a)
        )
        assert entity == world.ids["A3"]
        assert revision.edges == ()  # A3 -> B2 is hidden
        assert revision.content_hash == revision_content_hash(revision)
        raw = snapshot.get(world.ids["A3"])
        assert raw is not None and raw.edges != ()
        assert (revision.rev, revision.tx_seq) == (raw.rev, raw.tx_seq), label
