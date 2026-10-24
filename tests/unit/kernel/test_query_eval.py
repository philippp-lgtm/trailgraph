"""Reference query semantics (contracts B13.5; plan D8)."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EdgeId, EntityId, TermRef, TxId
from graph_cli.kernel.query_ast import (
    And,
    AsOf,
    Cmp,
    NeighborQuery,
    Not,
    Or,
    OrderKey,
    Query,
    RelConstraint,
)
from graph_cli.kernel.query_eval import (
    field_value,
    run_neighbors,
    run_query,
    strict_equal,
)
from graph_cli.kernel.records import (
    EdgeRecord,
    RecordStatus,
    Revision,
    content_hash,
    content_projection,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TYPE_A, TYPE_B = TermRef("t:A"), TermRef("t:B")
REL, OTHER = TermRef("t:rel"), TermRef("t:other")


def eid(n: int) -> EntityId:
    return EntityId(UUID(int=(0x0192 << 112) | (0x7 << 76) | (0b10 << 62) | n))


def edge(n: int, target: int, rel: TermRef = REL) -> EdgeRecord:
    return EdgeRecord(
        EdgeId(UUID(int=(0x0193 << 112) | (0x7 << 76) | (0b10 << 62) | n)), rel, eid(target)
    )


def revision(
    n: int,
    attrs: dict[str, JsonValue] | None = None,
    *,
    type: TermRef = TYPE_A,
    edges: Sequence[EdgeRecord] = (),
    status: RecordStatus = "active",
    tx_seq: int = 1,
    alias: str | None = None,
) -> Revision:
    attrs = attrs or {}
    projection = content_projection(
        type=type,
        record_status=status,
        alias=alias,
        attrs=attrs,
        edges=tuple(edges),
        valid_from=None,
        valid_to=None,
    )
    return Revision(
        id=eid(n),
        rev=1,
        prev_rev=None,
        type=type,
        record_status=status,
        attrs=attrs,
        edges=tuple(edges),
        alias=alias,
        valid_from=None,
        valid_to=None,
        created_at=T0,
        recorded_at=T0,
        tx_id=TxId(eid(900 + tx_seq)),
        tx_seq=tx_seq,
        ontology_version="1.0.0",
        content_hash=content_hash(projection),
    )


class Universe:
    def __init__(self, revisions: Iterable[Revision]) -> None:
        self.revisions = {r.id: r for r in revisions}

    def current(self, entity: EntityId) -> Revision | None:
        return self.revisions.get(entity)

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterable[Revision]:
        return [self.revisions[k] for k in sorted(self.revisions)]

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        found = [
            (r.id, e)
            for r in self.current_revisions()
            for e in r.edges
            if e.target == entity and rel in (None, e.rel)
        ]
        return sorted(found, key=lambda p: (p[0], p[1].edge_id))


def ids(universe: Universe, query: Query) -> list[int]:
    page = run_query(universe, query)
    return [r.id.int & 0xFFFF for r in page.items]


def where(pred: Cmp | And | Or | Not, *revisions: Revision) -> list[int]:
    return ids(Universe(revisions), Query(where=pred, limit=100))


VALUES: dict[int, JsonValue] = {
    1: 5,
    2: 10,
    3: "abc",
    4: True,
    5: ["x", "y"],
    6: {"k": 1},
    7: "10.50",
    8: "9.25",
    9: 1,
}


def universe_of_values() -> list[Revision]:
    return [revision(n, {"v": value}) for n, value in VALUES.items()] + [revision(10)]


@pytest.mark.parametrize(
    ("pred", "expected"),
    [
        (Cmp("v", "eq", 5), [1]),
        (Cmp("v", "eq", True), [4]),  # true is not 1
        (Cmp("v", "eq", 1), [9]),
        (Cmp("v", "eq", ["x", "y"]), [5]),
        (Cmp("v", "eq", {"k": 1}), [6]),
        (Cmp("v", "ne", 5), [2, 3, 4, 5, 6, 7, 8, 9]),  # missing (10) never matches ne
        (Cmp("v", "lt", 10), [1, 9]),  # integers only
        (Cmp("v", "le", 10), [1, 2, 9]),
        (Cmp("v", "gt", 5), [2]),
        (Cmp("v", "ge", "abc"), [3]),  # strings only, code point order
        (Cmp("v", "gt", "10.00"), [3, 7, 8]),  # text comparison without numeric
        (Cmp("v", "gt", "10", numeric=True), [7]),
        (Cmp("v", "lt", 10, numeric=True), [1, 8, 9]),
        (Cmp("v", "eq", "10.5", numeric=True), [7]),
        (Cmp("v", "in", [5, "abc", False]), [1, 3]),
        (Cmp("v", "in", ["9.250"], numeric=True), [8]),
        (Cmp("v", "contains", "b"), [3]),
        (Cmp("v", "contains", "x"), [5]),
        (Cmp("v", "prefix", "10"), [7]),
        (Cmp("v", "exists"), [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        (Cmp("v", "exists", True), [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        (Cmp("v", "exists", False), [10]),
        (Not(Cmp("v", "eq", 5)), [2, 3, 4, 5, 6, 7, 8, 9, 10]),
        (And(()), list(range(1, 11))),
        (Or(()), []),
        (And((Cmp("v", "ge", 5), Cmp("v", "le", 5))), [1]),
        (Or((Cmp("v", "eq", 5), Cmp("v", "eq", "abc"))), [1, 3]),
    ],
)
def test_operator_table(pred: Cmp | And | Or | Not, expected: list[int]) -> None:
    assert where(pred, *universe_of_values()) == expected


def test_strings_are_case_sensitive() -> None:
    revisions = [revision(1, {"v": "Abc"}), revision(2, {"v": "abc"})]
    assert where(Cmp("v", "eq", "abc"), *revisions) == [2]
    assert where(Cmp("v", "prefix", "A"), *revisions) == [1]
    assert where(Cmp("v", "contains", "B"), *revisions) == []


def test_null_counts_as_missing() -> None:
    assert where(Cmp("v", "exists", False), revision(1, {"v": None})) == [1]


def test_strict_equal() -> None:
    assert not strict_equal(True, 1)
    assert not strict_equal([1], [True])
    assert not strict_equal({"a": 1}, {"a": 1, "b": 2})
    assert strict_equal({"a": [1, "x"]}, {"a": [1, "x"]})
    assert not strict_equal("1", 1)


def test_system_fields() -> None:
    r = revision(3, {"type": "shadowed?"}, alias="AB-0001", tx_seq=4)
    assert field_value(r, "id") == str(eid(3))
    assert field_value(r, "type") == TYPE_A  # system fields win (attribute names are reserved)
    assert field_value(r, "rev") == 1
    assert field_value(r, "tx_seq") == 4
    assert field_value(r, "alias") == "AB-0001"
    assert field_value(r, "created_at") == "2026-01-01T00:00:00.000Z"
    assert field_value(r, "record_status") == "active"
    assert field_value(r, "valid_from") is None
    assert where(Cmp("id", "eq", str(eid(3))), r, revision(4)) == [3]


def test_type_status_and_changed_since_filters() -> None:
    universe = Universe(
        [
            revision(1, type=TYPE_A, tx_seq=1),
            revision(2, type=TYPE_B, tx_seq=2),
            revision(3, type=TYPE_A, status="superseded", tx_seq=3),
            revision(4, type=TYPE_A, status="retracted", tx_seq=4),
        ]
    )
    assert ids(universe, Query()) == [1, 2]
    assert ids(universe, Query(types=(TYPE_A,))) == [1]
    all_status = frozenset({"active", "superseded", "retracted"})
    assert ids(universe, Query(record_status=all_status)) == [1, 2, 3, 4]
    assert ids(universe, Query(record_status=all_status, changed_since_seq=2)) == [3, 4]


def test_relation_constraints() -> None:
    universe = Universe(
        [
            revision(1, edges=[edge(1, 2), edge(2, 3, OTHER)]),
            revision(2, edges=[edge(3, 3)]),
            revision(3),
        ]
    )
    assert ids(universe, Query(rels=(RelConstraint(REL, "out"),))) == [1, 2]
    assert ids(universe, Query(rels=(RelConstraint(REL, "out", targets=(eid(3),)),))) == [2]
    either = RelConstraint(REL, "out", targets=(eid(2), eid(3)))
    assert ids(universe, Query(rels=(either,))) == [1, 2]
    neither = RelConstraint(REL, "out", targets=(eid(2), eid(3)), exists=False)
    assert ids(universe, Query(rels=(neither,))) == [3]
    assert ids(universe, Query(rels=(RelConstraint(REL, "out", exists=False),))) == [3]
    assert ids(universe, Query(rels=(RelConstraint(REL, "in"),))) == [2, 3]
    assert ids(universe, Query(rels=(RelConstraint(OTHER, "in", targets=(eid(1),)),))) == [3]
    from_either = RelConstraint(REL, "in", targets=(eid(1), eid(2)))
    assert ids(universe, Query(rels=(from_either,))) == [2, 3]
    both = (RelConstraint(REL, "in"), RelConstraint(REL, "out"))
    assert ids(universe, Query(rels=both)) == [2]


def test_order_missing_last_in_both_directions() -> None:
    revisions = [
        revision(1, {"n": 3}),
        revision(2),
        revision(3, {"n": 1}),
        revision(4, {"n": 3}),
        revision(5),
    ]
    universe = Universe(revisions)
    assert ids(universe, Query(order=(OrderKey("n"),))) == [3, 1, 4, 2, 5]
    assert ids(universe, Query(order=(OrderKey("n", descending=True),))) == [1, 4, 3, 2, 5]


def test_order_mixed_types_and_numeric() -> None:
    universe = Universe(
        [
            revision(1, {"n": "b"}),
            revision(2, {"n": 7}),
            revision(3, {"n": False}),
            revision(4, {"n": ["x"]}),
            revision(5, {"n": {"k": 1}}),
            revision(6, {"n": "a"}),
        ]
    )
    assert ids(universe, Query(order=(OrderKey("n"),))) == [3, 2, 6, 1, 4, 5]
    decimals = Universe(
        [revision(1, {"p": "10.5"}), revision(2, {"p": "9.25"}), revision(3, {"p": "oops"})]
    )
    assert ids(decimals, Query(order=(OrderKey("p"),))) == [1, 2, 3]
    assert ids(decimals, Query(order=(OrderKey("p", numeric=True),))) == [2, 1, 3]
    descending = Query(order=(OrderKey("p", descending=True, numeric=True),))
    assert ids(decimals, descending) == [1, 2, 3]


def test_order_uses_several_keys_then_id() -> None:
    universe = Universe(
        [
            revision(4, {"a": 1, "b": "x"}),
            revision(2, {"a": 1, "b": "y"}),
            revision(3, {"a": 1, "b": "x"}),
            revision(1, {"a": 2, "b": "x"}),
        ]
    )
    query = Query(order=(OrderKey("a"), OrderKey("b", descending=True)))
    assert ids(universe, query) == [2, 3, 4, 1]
    assert ids(universe, Query()) == [1, 2, 3, 4]


def test_paging_is_stable_and_total_exact() -> None:
    universe = Universe([revision(n, {"g": n % 3}) for n in range(1, 24)])
    order = (OrderKey("g"),)
    full = ids(universe, Query(order=order, limit=100))
    pages: list[int] = []
    for offset in range(0, 30, 5):
        page = run_query(universe, Query(order=order, limit=5, offset=offset))
        assert page.total == 23
        assert (page.limit, page.offset) == (5, offset)
        pages += [r.id.int & 0xFFFF for r in page.items]
    assert pages == full
    assert run_query(universe, Query(limit=0)).items == ()


@pytest.mark.parametrize(
    "query",
    [
        Query(where=Cmp("v", "like", 1)),  # type: ignore[arg-type]
        Query(where=Cmp("v", "in", 1)),
        Query(where=Cmp("v", "prefix", 1)),
        Query(where=Cmp("v", "contains", "x", numeric=True)),
        Query(where=Cmp("v", "exists", "yes")),
        Query(as_of=AsOf(tx_seq=1)),
    ],
)
def test_invalid_queries_are_internal_errors(query: Query) -> None:
    with pytest.raises(GraphCliError) as info:
        run_query(Universe(universe_of_values()), query)
    assert info.value.code == "internal.error"


def graph() -> Universe:
    """1 -> 2 -> 3 -> 1 (cycle), 1 -> 4 (other), 5 -> 1, 3 -> 6, 6 -> 7."""
    return Universe(
        [
            revision(1, edges=[edge(11, 2), edge(12, 4, OTHER)]),
            revision(2, edges=[edge(21, 3)]),
            revision(3, edges=[edge(31, 1), edge(32, 6)]),
            revision(4),
            revision(5, edges=[edge(51, 1)]),
            revision(6, edges=[edge(61, 7)]),
            revision(7),
        ]
    )


def hops(query: NeighborQuery) -> list[tuple[int, int]]:
    return [
        (neighbor.int & 0xFFFF, e.edge_id.int & 0xFFFF)
        for neighbor, e, r in run_neighbors(graph(), query)
        if r.id == neighbor
    ]


def test_neighbors_out_depth_one() -> None:
    assert hops(NeighborQuery(eid(1), None, "out")) == [(2, 11), (4, 12)]
    assert hops(NeighborQuery(eid(1), (REL,), "out")) == [(2, 11)]


def test_neighbors_in_and_both() -> None:
    assert hops(NeighborQuery(eid(1), None, "in")) == [(3, 31), (5, 51)]
    assert hops(NeighborQuery(eid(1), (REL,), "both")) == [(2, 11), (3, 31), (5, 51)]


def test_neighbors_depth_and_cycles() -> None:
    assert hops(NeighborQuery(eid(1), (REL,), "out", depth=2)) == [(2, 11), (3, 21)]
    assert hops(NeighborQuery(eid(1), (REL,), "out", depth=3)) == [(2, 11), (3, 21), (6, 32)]
    # Depth 2 over "both": 3 is a direct neighbour (in) and is reported once, at depth 1.
    both = hops(NeighborQuery(eid(1), (REL,), "both", depth=2))
    assert both == [(2, 11), (3, 31), (5, 51), (6, 32)]
    assert all(n != 1 for n, _ in hops(NeighborQuery(eid(1), None, "both", depth=3)))


def test_neighbors_of_a_missing_start() -> None:
    assert run_neighbors(graph(), NeighborQuery(eid(99), None, "both")) == []


def test_neighbor_depth_is_bounded() -> None:
    with pytest.raises(GraphCliError):
        NeighborQuery(eid(1), None, "out", depth=4)
