"""Reference query semantics (contracts B13.5; plan D8)."""

from collections.abc import Iterable, Sequence
from dataclasses import replace
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
    revision_content_hash,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TYPE_A, TYPE_B, TYPE_C = TermRef("t:A"), TermRef("t:B"), TermRef("t:C")
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
        Query(where=Cmp("v", "lang", 1)),
        Query(where=Cmp("v", "eq", "x", numeric=True, tagged=True)),
        Query(as_of=AsOf(tx_seq=1)),
    ],
)
def test_invalid_queries_are_internal_errors(query: Query) -> None:
    with pytest.raises(GraphCliError) as info:
        run_query(Universe(universe_of_values()), query)
    assert info.value.code == "internal.error"


def tagged(text: str, lang: str) -> JsonValue:
    return {"lang": lang, "value": text}


def tagged_universe() -> list[Revision]:
    return [
        revision(1, {"n": tagged("Steuerung und Kontrolle", "de")}),
        revision(2, {"n": tagged("Control and Monitoring", "en")}),
        revision(3, {"n": tagged("SuK", "de-CH")}),
        revision(4, {"m": [tagged("SuK", "de"), tagged("CaM", "en-GB")]}),
        revision(5, {"n": "SuK"}),  # a plain string compares as before
    ]


@pytest.mark.parametrize(
    ("pred", "expected"),
    [
        (Cmp("n", "eq", "SuK", tagged=True), [3, 5]),
        (Cmp("n", "eq", tagged("SuK", "de-CH"), tagged=True), [3]),
        (Cmp("n", "eq", tagged("SuK", "de"), tagged=True), []),
        (Cmp("n", "ne", "SuK", tagged=True), [1, 2]),
        (Cmp("n", "in", ["SuK", tagged("Control and Monitoring", "en")], tagged=True), [2, 3, 5]),
        (Cmp("n", "contains", "und", tagged=True), [1]),
        (Cmp("n", "prefix", "Contr", tagged=True), [2]),
        (Cmp("n", "lt", "D", tagged=True), [2]),
        (Cmp("m", "contains", "CaM", tagged=True), [4]),
        (Cmp("m", "contains", tagged("SuK", "de"), tagged=True), [4]),
        (Cmp("m", "contains", tagged("SuK", "en"), tagged=True), []),
        (Cmp("n", "lang", "de"), [1, 3]),
        (Cmp("n", "lang", "DE-ch"), [3]),
        (Cmp("n", "lang", "d"), []),
        (Cmp("n", "lang", "*"), [1, 2, 3]),
        (Cmp("m", "lang", "en"), [4]),
        (Cmp("n", "eq", "SuK"), [5]),  # untagged comparison: structural
    ],
)
def test_tagged_values(pred: Cmp, expected: list[int]) -> None:
    assert where(pred, *tagged_universe()) == expected


def test_tagged_order_by_text_then_tag() -> None:
    universe = Universe(
        [
            revision(1, {"n": tagged("b", "de")}),
            revision(2, {"n": tagged("a", "fr")}),
            revision(3, {"n": tagged("a", "de")}),
            revision(4),
        ]
    )
    assert ids(universe, Query(order=(OrderKey("n", tagged=True),))) == [3, 2, 1, 4]
    descending = Query(order=(OrderKey("n", descending=True, tagged=True),))
    assert ids(universe, descending) == [1, 2, 3, 4]


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


# ---- visibility (``Query.visible``, ``NeighborQuery.visible``) ----------------------------


def sorted_ids(universe: Universe, query: Query) -> list[int]:
    return sorted(ids(universe, query))


def mixed_universe() -> Universe:
    """A1..A3, B4, B5 and a retracted C6, with edges between them."""
    return Universe(
        [
            revision(1, edges=[edge(11, 4)]),  # A -> hidden B only
            revision(2, edges=[edge(21, 3), edge(22, 4)]),  # A -> visible A and hidden B
            revision(3),
            revision(4, type=TYPE_B, edges=[edge(41, 1)]),
            revision(5, type=TYPE_B, edges=[edge(51, 2)]),
            revision(6, type=TYPE_C, status="retracted"),
        ]
    )


def test_no_visible_limit_changes_nothing() -> None:
    universe = mixed_universe()
    everything = frozenset({TYPE_A, TYPE_B, TYPE_C})
    for query in (Query(), Query(types=(TYPE_B,)), Query(rels=(RelConstraint(REL, "in"),))):
        plain = run_query(universe, query)
        assert plain.items == run_query(universe, replace(query, visible=None)).items
        limited = run_query(universe, replace(query, visible=everything))
        assert [r.id for r in limited.items] == [r.id for r in plain.items]
        assert limited.total == plain.total
    assert run_query(universe, Query()).items[0].edges == (edge(11, 4),)


def test_visible_skips_subjects_of_other_types() -> None:
    universe = mixed_universe()
    assert ids(universe, Query(visible=frozenset({TYPE_A}))) == [1, 2, 3]
    assert ids(universe, Query(visible=frozenset({TYPE_B}))) == [4, 5]
    both = frozenset({TYPE_A, TYPE_B})
    assert ids(universe, Query(visible=both, order=(OrderKey("id", True),))) == [5, 4, 3, 2, 1]
    # Record status does not matter for visibility, only for the status filter.
    some = Query(visible=frozenset({TYPE_C}), record_status=frozenset({"retracted"}))
    assert ids(universe, some) == [6]


def test_visible_narrows_types_and_never_falls_back_to_any_type() -> None:
    universe = mixed_universe()
    visible = frozenset({TYPE_A, TYPE_C})
    assert ids(universe, Query(types=(TYPE_A, TYPE_B), visible=visible)) == [1, 2, 3]
    for query in (
        Query(types=(TYPE_B,), visible=frozenset({TYPE_A})),  # empty intersection
        Query(visible=frozenset()),
        Query(types=(TYPE_A,), visible=frozenset()),
        Query(visible=frozenset({TermRef("t:Unknown")})),
    ):
        page = run_query(universe, query)
        assert (page.items, page.total, page.limit, page.offset) == ((), 0, 50, 0)
    page = run_query(universe, Query(visible=frozenset(), limit=7, offset=3))
    assert (page.limit, page.offset) == (7, 3)


class HintRecorder(Universe):
    """A universe that records the type hint and honours it, as a backend may."""

    def __init__(self, revisions: Iterable[Revision]) -> None:
        super().__init__(revisions)
        self.hints: list[frozenset[TermRef]] = []

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterable[Revision]:
        self.hints.append(types)
        return [r for r in super().current_revisions() if not types or r.type in types]


def test_visible_is_pushed_down_as_the_type_hint() -> None:
    universe = HintRecorder(mixed_universe().revisions.values())
    run_query(universe, Query(visible=frozenset({TYPE_A})))
    run_query(universe, Query(types=(TYPE_A, TYPE_B), visible=frozenset({TYPE_B, TYPE_C})))
    run_query(universe, Query(types=(TYPE_B,), visible=frozenset({TYPE_A})))
    run_query(universe, Query())
    assert universe.hints == [frozenset({TYPE_A}), frozenset({TYPE_B}), frozenset()]


def test_visible_total_counts_what_is_left_and_paging_follows() -> None:
    universe = mixed_universe()
    visible = frozenset({TYPE_A})
    page = run_query(universe, Query(visible=visible, limit=2, offset=1))
    assert [r.id.int & 0xFFFF for r in page.items] == [2, 3]
    assert (page.total, page.limit, page.offset) == (3, 2, 1)
    assert run_query(universe, Query(visible=visible, limit=2, offset=3)).items == ()
    assert run_query(universe, Query(visible=visible, limit=0)).total == 3
    unlimited = run_query(universe, Query(limit=2, offset=1))
    assert unlimited.total == 5


def test_hidden_relation_ends_count_as_absent() -> None:
    universe = mixed_universe()
    visible = frozenset({TYPE_A, TYPE_C})
    out = RelConstraint(REL, "out")

    def found(*rels: RelConstraint) -> list[int]:
        return sorted_ids(universe, Query(rels=rels, visible=visible))

    assert found(out) == [2]  # 1 points at a hidden entity only
    assert found(RelConstraint(REL, "out", exists=False)) == [1, 3]
    assert found(RelConstraint(REL, "out", targets=(eid(4),))) == []
    assert found(RelConstraint(REL, "out", targets=(eid(4),), exists=False)) == [1, 2, 3]
    assert found(RelConstraint(REL, "out", targets=(eid(3),))) == [2]
    assert found(RelConstraint(REL, "out", targets=(eid(3), eid(4)))) == [2]
    # "in": sources 4 and 5 are hidden, so 1 and 2 have no incoming edge from a visible entity.
    assert found(RelConstraint(REL, "in")) == [3]
    assert found(RelConstraint(REL, "in", exists=False)) == [1, 2]
    assert found(RelConstraint(REL, "in", targets=(eid(4),))) == []
    assert found(RelConstraint(REL, "in", targets=(eid(2),))) == [3]
    # Without a limit the same constraints see everything.
    plain = Query(rels=(RelConstraint(REL, "in"),))
    assert sorted_ids(universe, plain) == [1, 2, 3, 4]


def test_a_missing_target_is_not_visible() -> None:
    universe = Universe([revision(1, edges=[edge(11, 99)]), revision(2)])
    out = RelConstraint(REL, "out")
    assert ids(universe, Query(rels=(out,))) == [1]
    assert ids(universe, Query(rels=(out,), visible=frozenset({TYPE_A}))) == []
    gone = RelConstraint(REL, "out", exists=False)
    assert ids(universe, Query(rels=(gone,), visible=frozenset({TYPE_A}))) == [1, 2]


def test_a_retracted_entity_of_a_visible_type_is_visible() -> None:
    universe = Universe([revision(1, edges=[edge(11, 2)]), revision(2, status="retracted")])
    page = run_query(universe, Query(visible=frozenset({TYPE_A})))
    assert [r.edges for r in page.items] == [(edge(11, 2),)]
    out = RelConstraint(REL, "out")
    assert ids(universe, Query(rels=(out,), visible=frozenset({TYPE_A}))) == [1]


def test_returned_revisions_are_stripped_after_paging() -> None:
    universe = mixed_universe()
    raw = {r.id: r for r in universe.revisions.values()}
    page = run_query(universe, Query(visible=frozenset({TYPE_A}), limit=2))
    one, two = page.items
    assert one.edges == ()
    assert two.edges == (edge(21, 3),)
    for stripped in (one, two):
        before = raw[stripped.id]
        assert (stripped.rev, stripped.tx_seq, stripped.attrs) == (
            before.rev,
            before.tx_seq,
            before.attrs,
        )
        assert stripped.content_hash == revision_content_hash(stripped)
        assert stripped.content_hash != before.content_hash
    # Nothing to strip: the very same object comes back.
    (third,) = run_query(universe, Query(visible=frozenset({TYPE_A}), limit=1, offset=2)).items
    assert third is raw[third.id]
    wide = run_query(universe, Query(visible=frozenset({TYPE_A, TYPE_B}), limit=10))
    assert all(r is raw[r.id] for r in wide.items)


def test_where_and_order_on_content_hash_read_the_stripped_revision() -> None:
    universe = mixed_universe()
    only_a = frozenset({TYPE_A})
    stripped = {r.id: r for r in run_query(universe, Query(visible=only_a, limit=10)).items}
    raw = {r.id: r for r in universe.revisions.values()}
    for entity, revision in stripped.items():
        if revision.content_hash == raw[entity].content_hash:
            continue
        # The raw hash would tell that hidden edges exist; only the stripped one matches.
        hidden = Query(visible=only_a, where=Cmp("content_hash", "eq", raw[entity].content_hash))
        shown = Query(visible=only_a, where=Cmp("content_hash", "eq", revision.content_hash))
        assert run_query(universe, hidden).total == 0
        assert entity in [r.id for r in run_query(universe, shown).items]
    ordered = run_query(universe, Query(visible=only_a, order=(OrderKey("content_hash"),)))
    assert [r.id for r in ordered.items] == sorted(
        stripped, key=lambda e: (stripped[e].content_hash, e)
    )


def typed_graph(*, shortcut: bool) -> Universe:
    """A1 -> A2 -> B3 -> A6 -> A7, A5 -> A1, B3 -> A1, and optionally A1 -> A8 -> A6."""
    return Universe(
        [
            revision(1, edges=[edge(11, 2), *([edge(13, 8)] if shortcut else [])]),
            revision(2, edges=[edge(21, 3)]),
            revision(3, type=TYPE_B, edges=[edge(31, 6), edge(33, 1)]),
            revision(5, edges=[edge(51, 1)]),
            revision(6, edges=[edge(61, 7)]),
            revision(7),
            revision(8, edges=[edge(81, 6)]),
        ]
    )


def walk(
    universe: Universe,
    start: int,
    direction: str,
    depth: int,
    visible: frozenset[TermRef] | None,
) -> list[tuple[int, int]]:
    query = NeighborQuery(eid(start), None, direction, depth, visible)  # type: ignore[arg-type]
    return [(n.int & 0xFFFF, e.edge_id.int & 0xFFFF) for n, e, _ in run_neighbors(universe, query)]


def test_neighbors_never_step_onto_or_through_a_hidden_node() -> None:
    universe = typed_graph(shortcut=False)
    only_a = frozenset({TYPE_A})
    for depth in (1, 2, 3):
        assert walk(universe, 1, "out", depth, only_a) == [(2, 11)]
    # Without a limit the walk goes on through the hidden node (depth 3 ends at 6).
    assert walk(universe, 1, "out", 3, None) == [(2, 11), (3, 21), (6, 31)]
    assert walk(universe, 1, "out", 3, frozenset({TYPE_A, TYPE_B})) == [(2, 11), (3, 21), (6, 31)]


def test_neighbors_reach_a_node_behind_a_hidden_one_by_another_visible_path() -> None:
    universe = typed_graph(shortcut=True)
    only_a = frozenset({TYPE_A})
    assert walk(universe, 1, "out", 1, only_a) == [(2, 11), (8, 13)]
    assert walk(universe, 1, "out", 2, only_a) == [(2, 11), (8, 13), (6, 81)]
    assert walk(universe, 1, "out", 3, only_a) == [(2, 11), (8, 13), (6, 81), (7, 61)]
    assert walk(universe, 1, "out", 2, None) == [(2, 11), (8, 13), (3, 21), (6, 81)]


def test_neighbors_in_and_both_skip_hidden_sources() -> None:
    universe = typed_graph(shortcut=False)
    only_a = frozenset({TYPE_A})
    assert walk(universe, 1, "in", 1, None) == [(3, 33), (5, 51)]
    assert walk(universe, 1, "in", 1, only_a) == [(5, 51)]
    assert walk(universe, 6, "in", 3, None) == [(3, 31), (8, 81), (2, 21), (1, 11)]
    assert walk(universe, 6, "in", 3, only_a) == [(8, 81)]  # 2 and 1 lie behind hidden 3
    assert walk(universe, 2, "both", 2, None) == [(1, 11), (3, 21), (5, 51), (6, 31)]
    assert walk(universe, 2, "both", 1, only_a) == [(1, 11)]
    assert walk(universe, 2, "both", 3, only_a) == [(1, 11), (5, 51)]
    assert walk(universe, 7, "both", 3, only_a) == [(6, 61), (8, 81)]


def test_neighbors_of_a_hidden_or_missing_start_are_empty() -> None:
    universe = typed_graph(shortcut=False)
    only_a = frozenset({TYPE_A})
    assert walk(universe, 3, "both", 3, only_a) == []
    assert walk(universe, 99, "both", 3, only_a) == []
    assert walk(universe, 1, "both", 3, frozenset()) == []
    assert walk(universe, 3, "both", 1, None) != []


def test_neighbor_revisions_are_stripped() -> None:
    universe = typed_graph(shortcut=False)
    raw = {r.id: r for r in universe.revisions.values()}
    only_a = frozenset({TYPE_A})
    ((node, _, stripped),) = run_neighbors(universe, NeighborQuery(eid(1), None, "out", 1, only_a))
    assert node == eid(2) and stripped.edges == ()  # its only edge goes to hidden 3
    assert (stripped.rev, stripped.tx_seq) == (raw[node].rev, raw[node].tx_seq)
    assert stripped.content_hash == revision_content_hash(stripped) != raw[node].content_hash
    ((node, _, same),) = run_neighbors(universe, NeighborQuery(eid(1), None, "in", 1, only_a))
    assert node == eid(5) and same is raw[node]
    ((_, _, plain),) = run_neighbors(universe, NeighborQuery(eid(1), None, "out", 1))
    assert plain is raw[eid(2)]
