"""``WorkingState`` honours ``visible`` like every other store (contracts B8.14, B13.5; stage 3 V5).

The overlay of a change set is part of what a limited reader sees: new revisions of visible
types count, edges into hidden new entities do not.
"""

from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.query_ast import NeighborQuery, Query, RelConstraint
from graph_cli.kernel.query_eval import run_neighbors
from graph_cli.kernel.records import Revision, revision_content_hash
from graph_cli.temporal_knowledge.working import WorkingState

ITEM, PLACE = TermRef("inv:Item"), TermRef("inv:Location")
REL = TermRef("inv:located_in")
ONLY_ITEMS = frozenset({ITEM})

# Base: P1 -> Q1 -> P2 (P: Item, Q: Location).
BASE = (
    revision("p1", ITEM, edges=[edge("inv:located_in", "q1", name="p1-q1")]),
    revision("q1", PLACE, edges=[edge("inv:located_in", "p2", name="q1-p2")]),
    revision("p2", ITEM),
)
# Change set: P1 gets an edge to the new item P3; the new location Q2 points at P1.
CHANGED = (
    revision(
        "p1",
        ITEM,
        edges=[
            edge("inv:located_in", "q1", name="p1-q1"),
            edge("inv:located_in", "p3", name="p1-p3"),
        ],
        rev=2,
    ),
    revision("p3", ITEM),
    revision("q2", PLACE, edges=[edge("inv:located_in", "p1", name="q2-p1")]),
)


def working(base: tuple[Revision, ...], *changed: Revision) -> WorkingState:
    return WorkingState(
        base=DictSnapshot(*base),
        revisions={r.id: r for r in changed},
        unchanged={},
        created=frozenset(r.id for r in changed if r.rev == 1),
        locals={},
        alias_allocations=(),
        resolution={},
        findings=(),
        effects=(),
    )


def merged() -> DictSnapshot:
    """The same state as a plain snapshot, the reference for the overlay."""
    changed = {r.id: r for r in CHANGED}
    return DictSnapshot(*[changed.pop(r.id, r) for r in BASE], *changed.values())


def names(entities: list[EntityId]) -> list[str]:
    known = {eid(n): n for n in ("p1", "p2", "p3", "q1", "q2")}
    return sorted(known[e] for e in entities)


def test_queries_see_the_overlay_through_the_visible_set() -> None:
    state, reference = working(BASE, *CHANGED), merged()
    out = RelConstraint(REL, "out")
    inc = RelConstraint(REL, "in")
    cases = {
        "subjects": (Query(visible=ONLY_ITEMS), ["p1", "p2", "p3"]),
        "other type": (Query(visible=frozenset({PLACE})), ["q1", "q2"]),
        "types outside": (Query(types=(PLACE,), visible=ONLY_ITEMS), []),
        "nothing visible": (Query(visible=frozenset()), []),
        "out exists": (Query(rels=(out,), visible=ONLY_ITEMS), ["p1"]),  # only the new edge counts
        "out absent": (
            Query(rels=(RelConstraint(REL, "out", exists=False),), visible=ONLY_ITEMS),
            ["p2", "p3"],
        ),
        "in exists": (
            Query(rels=(inc,), visible=ONLY_ITEMS),
            ["p3"],
        ),  # p1 and p2 have hidden sources
        "in absent": (
            Query(rels=(RelConstraint(REL, "in", exists=False),), visible=ONLY_ITEMS),
            ["p1", "p2"],
        ),
        "unlimited": (Query(), ["p1", "p2", "p3", "q1", "q2"]),
    }
    for case, (query, expected) in cases.items():
        page = state.query(query)
        assert names([r.id for r in page.items]) == expected, case
        assert page.total == len(expected), case
        assert page == reference.query(query), case


def test_overlay_revisions_are_stripped() -> None:
    state = working(BASE, *CHANGED)
    items = {r.id: r for r in state.query(Query(visible=ONLY_ITEMS)).items}
    p1 = items[eid("p1")]
    assert [e.target for e in p1.edges] == [eid("p3")]
    assert (p1.rev, p1.tx_seq) == (2, 2)
    assert p1.content_hash == revision_content_hash(p1)
    assert items[eid("p3")] is state.get(eid("p3"))  # nothing to strip: the same object


def test_neighbors_do_not_walk_through_hidden_entities() -> None:
    state, reference = working(BASE, *CHANGED), merged()
    for direction, depth, expected in (
        ("out", 3, ["p3"]),  # q1 is hidden, so p2 behind it is not reached
        ("in", 3, []),
        ("both", 3, ["p3"]),
    ):
        q = NeighborQuery(eid("p1"), None, direction, depth, ONLY_ITEMS)  # type: ignore[arg-type]
        found = list(state.neighbors(q))
        assert names([entity for entity, _, _ in found]) == expected, direction
        assert found == run_neighbors(reference, q), direction
    unlimited = NeighborQuery(eid("p1"), None, "out", 3)
    assert names([e for e, _, _ in state.neighbors(unlimited)]) == ["p2", "p3", "q1"]
    hidden_start = NeighborQuery(eid("q2"), None, "both", 3, ONLY_ITEMS)
    assert state.neighbors(hidden_start) == []
