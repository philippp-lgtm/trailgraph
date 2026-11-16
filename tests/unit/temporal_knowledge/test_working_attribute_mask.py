"""``WorkingState`` honours ``attributes`` like every other store (contracts B8.14, B13.5;
field visibility plan F5): dry-run batches and the write pipeline's queries over the overlay
never match, order or show an attribute the mask hides.
"""

from dataclasses import replace

from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.query_ast import Cmp, NeighborQuery, Not, OrderKey, Query
from graph_cli.kernel.read_view import AttributeMask
from graph_cli.kernel.records import Revision, revision_content_hash, strip_revision
from graph_cli.temporal_knowledge.working import WorkingState

PERSON, TEAM = TermRef("ex:Person"), TermRef("ex:Team")
MASK: AttributeMask = ((PERSON, frozenset({"name", "code"})), (TEAM, frozenset({"name"})))

BASE = (
    revision("p1", PERSON, {"name": "Ada", "email": "a@x", "code": "c1"}),
    revision("p2", PERSON, {"name": "Bo", "code": "c2"}),
    revision("t1", TEAM, {"name": "Core", "code": "c1"}),  # ``code`` is hidden on teams
)
# The change set gives p2 an email and creates a team and a person.
CHANGED = (
    revision("p2", PERSON, {"name": "Bo", "code": "c2", "email": "b@x"}, rev=2),
    revision("p3", PERSON, {"name": "Cy", "email": "c@x"}),
    revision("t2", TEAM, {"name": "Ops", "code": "c2"}),
)


def working() -> WorkingState:
    return WorkingState(
        base=DictSnapshot(*BASE),
        revisions={r.id: r for r in CHANGED},
        unchanged={},
        created=frozenset(r.id for r in CHANGED if r.rev == 1),
        locals={},
        alias_allocations=(),
        resolution={},
        findings=(),
        effects=(),
    )


def stripped() -> DictSnapshot:
    """The same state with the hidden attributes never stored, the oracle."""
    keep = dict(MASK)
    changed = {r.id: r for r in CHANGED}
    merged: list[Revision] = [changed.pop(r.id, r) for r in BASE]
    merged += changed.values()
    return DictSnapshot(*(strip_revision(r, None, keep[r.type], rehash=True) for r in merged))


def names(entities: list[EntityId]) -> list[str]:
    known = {eid(n): n for n in ("p1", "p2", "p3", "t1", "t2")}
    return [known[e] for e in entities]


def test_queries_over_the_overlay_match_only_what_the_mask_shows() -> None:
    state, oracle = working(), stripped()
    cases = {
        "a hidden attribute never matches": (Query(where=Cmp("email", "exists")), []),
        "a sibling's hidden value": (Query(where=Cmp("code", "eq", "c1")), ["p1"]),
        "not": (
            Query(where=Not(Cmp("code", "eq", "c2")), types=(TEAM,)),
            sorted(["t1", "t2"], key=eid),
        ),
        "ne": (Query(where=Cmp("code", "ne", "c1")), ["p2"]),
        "order": (
            Query(order=(OrderKey("email"),), types=(PERSON,)),
            sorted(["p1", "p2", "p3"], key=eid),  # every email is missing: ids decide
        ),
    }
    for case, (query, expected) in cases.items():
        masked = replace(query, attributes=MASK)
        page = state.query(masked)
        assert names([r.id for r in page.items]) == expected, case
        assert page.total == len(expected), case
        assert page == oracle.query(query), case


def test_overlay_revisions_are_stripped_and_rehashed() -> None:
    items = {r.id: r for r in working().query(Query(attributes=MASK)).items}
    p2 = items[eid("p2")]
    assert p2.attrs == {"name": "Bo", "code": "c2"} and p2.rev == 2
    assert p2.content_hash == revision_content_hash(p2)
    assert items[eid("t2")].attrs == {"name": "Ops"}


def test_neighbors_over_the_overlay_are_stripped() -> None:
    base = (
        revision("p1", PERSON, {"name": "Ada", "email": "a@x"}),
        revision("t1", TEAM, {"name": "Core", "code": "c1"}),
    )
    linked = revision(
        "p1", PERSON, {"name": "Ada", "email": "a@x"}, [edge("ex:member_of", "t1")], rev=2
    )
    state = WorkingState(
        base=DictSnapshot(*base),
        revisions={linked.id: linked},
        unchanged={},
        created=frozenset(),
        locals={},
        alias_allocations=(),
        resolution={},
        findings=(),
        effects=(),
    )
    ((entity, _, team),) = state.neighbors(NeighborQuery(eid("p1"), None, "out", 1, None, MASK))
    assert entity == eid("t1") and team.attrs == {"name": "Core"}
    ((_, _, person),) = state.neighbors(NeighborQuery(eid("t1"), None, "in", 1, None, MASK))
    assert person.attrs == {"name": "Ada"}
    assert person.content_hash == revision_content_hash(person)
