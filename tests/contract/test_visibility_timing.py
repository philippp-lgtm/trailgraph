"""A limited reader does not pay for its limit with the size of the graph (type read limits plan
leak item 12): a ``find`` with a relation filter and a ``neighbors`` walk over a graph with many
edges stay within a generous factor of what the reader who sees everything pays, on each store.

The factor catches an evaluation that reads every edge target of every candidate one by one
(a read per edge instead of a memoised, batched type lookup); it is no benchmark. A small
absolute allowance absorbs the noise of a timer on a loaded machine. The memory store answers
the unlimited reader in about a millisecond, so there the allowance does most of the work; the
file store with its index is the one that matters.
"""

import random
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from support.stores import StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import NeighborQuery, Query, RelConstraint
from graph_cli.services.visibility import VisibleSnapshot

pytestmark = pytest.mark.slow

REL = TermRef("core:source")
VISIBLE_ENTITIES, HIDDEN_ENTITIES, EDGES_EACH = 2000, 1000, 5
FACTOR = 3.0
ALLOWANCE_S = 0.03  # per measured operation
REPEATS = 5


def best_of(operation: Callable[[], object]) -> float:
    times = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        operation()
        times.append(time.perf_counter() - started)
    return min(times)


@pytest.fixture
def graph(backend: str, tmp_path: Path) -> tuple[StoreEnv, list[EntityId]]:
    env = StoreEnv(backend, "inventory", tmp_path)
    visible_type, hidden_type = env.builder.concrete_classes[:2]
    rng = random.Random(7)
    seen: list[EntityId] = []

    def build(draft: TxDraft) -> None:
        seen.extend(draft.create(visible_type, alias=None) for _ in range(VISIBLE_ENTITIES))
        hidden = [draft.create(hidden_type, alias=None) for _ in range(HIDDEN_ENTITIES)]
        for source in seen:  # three edges to hidden entities, two to visible ones
            targets = [*rng.sample(hidden, EDGES_EACH - 2), *rng.sample(seen, 2)]
            for target in targets:
                if target != source:
                    draft.link(source, REL, target)

    env.write(build)
    return env, seen


def test_a_limited_reader_stays_within_a_factor_of_an_unlimited_one(
    graph: tuple[StoreEnv, list[EntityId]],
) -> None:
    env, seen = graph
    raw: Snapshot = env.store.snapshot()
    limited: Snapshot = VisibleSnapshot(
        env.store.snapshot(), frozenset({env.builder.concrete_classes[0]})
    )
    starts = random.Random(3).sample(seen, 20)
    operations: dict[str, Callable[[Snapshot], object]] = {
        "find with an outgoing relation": lambda s: s.query(
            Query(rels=(RelConstraint(REL, "out"),), limit=500)
        ),
        "find with an incoming relation": lambda s: s.query(
            Query(rels=(RelConstraint(REL, "in"),), limit=500)
        ),
        "find with a relation to a target": lambda s: s.query(
            Query(rels=(RelConstraint(REL, "out", (seen[0], seen[1])),), limit=500)
        ),
        "neighbors, depth 3": lambda s: [
            s.neighbors(NeighborQuery(start, None, "both", 3)) for start in starts
        ],
    }
    for name, operation in operations.items():
        unlimited = best_of(partial(operation, raw))
        paid = best_of(partial(operation, limited))
        assert paid <= FACTOR * unlimited + ALLOWANCE_S, (
            f"{name}: {paid * 1000:.1f} ms for the limited reader, "
            f"{unlimited * 1000:.1f} ms for the unlimited one"
        )
    # the limit filters something: the answers differ, so the comparison is not of equals
    assert limited.query(Query(limit=1)).total == VISIBLE_ENTITIES
    assert raw.query(Query(limit=1)).total == VISIBLE_ENTITIES + HIDDEN_ENTITIES
