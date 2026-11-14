"""``WorkingState.incoming`` answers from a lazily built reverse-edge index, with the same
result as a scan of the overlay (contracts B8.14)."""

import random
from dataclasses import replace

import pytest
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.temporal_knowledge.working import WorkingState

RELATIONS = ("ex:link", "ex:see", "ex:uses")


def working(base: DictSnapshot, changed: dict[EntityId, Revision]) -> WorkingState:
    return WorkingState(
        base=base,
        revisions=changed,
        unchanged={},
        created=frozenset(),
        locals={},
        alias_allocations=(),
        resolution={},
        findings=(),
        effects=(),
    )


def scan(
    state: WorkingState, entity: EntityId, rel: TermRef | None
) -> list[tuple[EntityId, EdgeRecord]]:
    """The straightforward answer: the base's incoming edges of unchanged sources, plus the
    overlay's edges found by walking every changed revision."""
    found = [
        (source, e)
        for source, e in state.base.incoming(entity, rel)
        if source not in state.revisions
    ]
    for source, changed in state.revisions.items():
        found += [(source, e) for e in changed.edges if e.target == entity and rel in (None, e.rel)]
    return sorted(found, key=lambda pair: (pair[0], pair[1].edge_id))


def random_edges(rng: random.Random, source: int, count: int, prefix: str) -> list[EdgeRecord]:
    return [
        edge(
            rng.choice(RELATIONS),
            f"e{rng.randrange(count)}",
            name=f"{prefix}{source}-{j}",
        )
        for j in range(rng.randint(0, 4))
    ]


@pytest.mark.parametrize("seed", range(20))
def test_the_index_answers_like_a_scan_on_random_overlays(seed: int) -> None:
    rng = random.Random(seed)
    count = rng.randint(5, 40)
    base = [
        revision(f"e{i}", "ex:Item", edges=random_edges(rng, i, count, "b")) for i in range(count)
    ]
    changed = {}
    for i in rng.sample(range(count + 5), rng.randint(0, count)):
        # some overlay revisions replace a base entity, some are new (ids past the base)
        r = revision(f"e{i}", "ex:Item", edges=random_edges(rng, i, count, "o"), rev=2)
        changed[r.id] = r
    state = working(DictSnapshot(*base), changed)
    for i in range(count + 5):
        for rel in (None, *map(TermRef, RELATIONS)):
            assert state.incoming(eid(f"e{i}"), rel) == scan(state, eid(f"e{i}"), rel)


def test_the_index_is_built_once_and_only_when_asked() -> None:
    state = working(DictSnapshot(revision("e1", "ex:Item")), {})
    assert "incoming" not in state._cache
    state.incoming(eid("e1"))
    first = state._cache["incoming"]
    state.incoming(eid("e1"), TermRef("ex:link"))
    assert state._cache["incoming"] is first


def test_a_copy_with_other_revisions_does_not_reuse_the_index() -> None:
    """``replace`` carries ``_cache`` over; the index must follow the revisions."""
    one = revision("e1", "ex:Item", edges=[edge("ex:link", "e2", name="a")], rev=2)
    state = working(
        DictSnapshot(revision("e1", "ex:Item"), revision("e2", "ex:Item")), {one.id: one}
    )
    assert [s for s, _ in state.incoming(eid("e2"))] == [eid("e1")]
    emptied = replace(state, revisions={})
    assert emptied.incoming(eid("e2")) == []
    other = revision("e3", "ex:Item", edges=[edge("ex:link", "e2", name="b")], rev=2)
    moved = replace(state, revisions={other.id: other})
    assert [s for s, _ in moved.incoming(eid("e2"))] == [eid("e3")]
    assert [s for s, _ in state.incoming(eid("e2"))] == [eid("e1")]
