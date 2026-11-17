"""A limited reader does not pay for its limit with the size of the graph (type read limits plan
leak item 12): a ``find`` with a relation filter and a ``neighbors`` walk over a graph with many
edges stay within a generous factor of what the reader who sees everything pays, on each store;
so does a page of the change feed over a thousand txs that write entities of a hidden type
(change feed plan leak item 12) and one over entities that were retyped out of a hidden class
(each revision is judged by its own type, F7).

The factor catches an evaluation that reads every edge target of every candidate one by one
(a read per edge instead of a memoised, batched type lookup); it is no benchmark. A small
absolute allowance absorbs the noise of a timer on a loaded machine. The memory store answers
the unlimited reader in about a millisecond, while the limited reader also pays for every
revision it hands out without its hidden edges (a recomputed content hash per returned
revision, proportional to the answer, not to the graph); so there a larger allowance does most
of the work, and on a loaded CI runner (``-n 8``) the walks measured 118 ms against 19 ms. The
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
from graph_cli.kernel.read_view import ReadView
from graph_cli.services.read import change_page
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.temporal_knowledge.changes import FeedPosition

pytestmark = pytest.mark.slow

REL = TermRef("core:source")
VISIBLE_ENTITIES, HIDDEN_ENTITIES, EDGES_EACH = 2000, 1000, 5
FACTOR = 3.0
ALLOWANCE_S = {"memory": 0.25}  # per measured operation; others below
DEFAULT_ALLOWANCE_S = 0.05
REPEATS = 5


def best_of(operation: Callable[[], object], repeats: int = REPEATS) -> float:
    times = []
    for _ in range(repeats):
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
    graph: tuple[StoreEnv, list[EntityId]], backend: str
) -> None:
    env, seen = graph
    allowance = ALLOWANCE_S.get(backend, DEFAULT_ALLOWANCE_S)
    raw: Snapshot = env.store.snapshot()
    limited: Snapshot = VisibleSnapshot(
        env.store.snapshot(), ReadView(frozenset({env.builder.concrete_classes[0]}))
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
        assert paid <= FACTOR * unlimited + allowance, (
            f"{name}: {paid * 1000:.1f} ms for the limited reader, "
            f"{unlimited * 1000:.1f} ms for the unlimited one"
        )
    # the limit filters something: the answers differ, so the comparison is not of equals
    assert limited.query(Query(limit=1)).total == VISIBLE_ENTITIES
    assert raw.query(Query(limit=1)).total == VISIBLE_ENTITIES + HIDDEN_ENTITIES


# ---- markings (field visibility plan leak item 12) -----------------------------------------

MARKED_ENTITIES = 5000


@pytest.fixture
def marked(backend: str, tmp_path: Path) -> StoreEnv:
    env = StoreEnv(backend, "staff", tmp_path)

    def build(draft: TxDraft) -> None:
        for n in range(MARKED_ENTITIES):
            draft.create(
                "stf:Employee",
                attrs={
                    "name": f"Person {n}",
                    "phone": f"+1 555 {n:04d}",
                    "email": f"p{n}@private.example",
                    "birth_date": "1990-01-01",
                    "weekly_hours": n % 40,
                    "badge_code": f"B-{n}",
                },
                alias=None,
            )

    env.write(build)
    return env


def test_a_reader_without_markings_stays_within_a_factor_of_a_holder(
    marked: StoreEnv, backend: str
) -> None:
    """``find``, ``lookup`` and ``search`` over 5,000 entities of a marked type: the reader
    without the marking strips every candidate ``lookup`` and ``search`` read (all of them),
    which must not cost more than a factor of what the holder pays. Their queries skip the
    content hash of the stripped candidates (``Query.rehash``): with it the memory store paid
    about 85 ms against 7 ms, without it about 30 ms; the file store about 75 ms against 50."""
    from graph_cli.query.lookup import LookupSpec, plan_lookup
    from graph_cli.query.plan import FindSpec, plan_find
    from graph_cli.query.search import SearchSpec, plan_search
    from graph_cli.services.visibility import attribute_mask, filter_model

    model = marked.builder.model
    view = ReadView(None, attribute_mask(model, frozenset()))
    seen = filter_model(model, None, view.attributes)
    allowance = ALLOWANCE_S.get(backend, DEFAULT_ALLOWANCE_S)
    raw: Snapshot = marked.store.snapshot()
    limited: Snapshot = VisibleSnapshot(marked.store.snapshot(), view)
    plans = {
        "find by a visible field, sorted": lambda m: plan_find(
            m, FindSpec(types=("stf:Employee",), where=("phone:prefix=+1",), sort=("name",))
        ),
        "lookup": lambda m: plan_lookup(m, LookupSpec("Person 4711")).query,
        "search": lambda m: plan_search(m, SearchSpec("person")).query,
    }
    for name, plan in plans.items():
        unlimited = best_of(partial(raw.query, plan(model)))
        paid = best_of(partial(limited.query, plan(seen)))
        assert paid <= FACTOR * unlimited + allowance, (
            f"{name}: {paid * 1000:.1f} ms for the reader without markings, "
            f"{unlimited * 1000:.1f} ms for the holder"
        )
    # the view strips something: the answers differ, so the comparison is not of equals
    first = limited.query(Query(limit=1)).items[0]
    assert "email" not in first.attrs and "email" in raw.query(Query(limit=1)).items[0].attrs


# ---- the change feed (change feed plan leak item 12) ---------------------------------------

FEED_TXS = 1000
FEED_REPEATS = 3


def test_a_feed_page_over_txs_of_a_hidden_type_stays_within_a_factor_of_the_unlimited_page(
    backend: str, tmp_path: Path
) -> None:
    """One page reads 1,000 txs, each creating an entity of the hidden type and linking the
    one before to it (two hidden revisions per tx), every tenth also a visible entity. The
    limited reader reads each tx once and decides the type of every hidden revision once;
    the factor catches a page that replays the log or looks a type up per revision."""
    env = StoreEnv(backend, "inventory", tmp_path)
    visible_type, hidden_type = env.builder.concrete_classes[:2]
    previous: list[EntityId] = []

    def tx(n: int) -> Callable[[TxDraft], None]:
        def build(draft: TxDraft) -> None:
            created = draft.create(hidden_type, alias=None)
            if previous:
                draft.link(previous[-1], REL, created)
            previous.append(created)
            if n % 10 == 0:
                draft.create(visible_type, alias=None)

        return build

    for n in range(FEED_TXS):
        env.write(tx(n))
    allowance = ALLOWANCE_S.get(backend, DEFAULT_ALLOWANCE_S)
    raw: Snapshot = env.store.snapshot()
    limited: Snapshot = VisibleSnapshot(env.store.snapshot(), ReadView(frozenset({visible_type})))
    assert raw.seq == FEED_TXS

    def page(view: Snapshot) -> dict[str, object]:
        return change_page(
            view, raw, FeedPosition(0), 3 * FEED_TXS, session=None, migration=lambda _: None
        )

    unlimited = best_of(partial(page, raw), FEED_REPEATS)
    paid = best_of(partial(page, limited), FEED_REPEATS)
    assert paid <= FACTOR * unlimited + allowance, (
        f"{paid * 1000:.1f} ms for the limited reader, {unlimited * 1000:.1f} ms for the "
        "unlimited one"
    )
    # the limit filters something: the pages differ, so the comparison is not of equals
    everything, visible = page(raw), page(limited)
    assert (everything["next"], visible["next"]) == (f"tx:{FEED_TXS}",) * 2
    assert everything["more"] is False and visible["more"] is False
    assert len(everything["items"]) == FEED_TXS  # type: ignore[arg-type]
    assert len(visible["items"]) == FEED_TXS // 10  # type: ignore[arg-type]


MOVED_ENTITIES, HIDDEN_REVISIONS = 500, 4


def test_a_feed_page_over_entities_retyped_out_of_a_hidden_class_stays_within_a_factor(
    backend: str, tmp_path: Path
) -> None:
    """500 entities of the hidden class get four revisions of it, then a migration moves them
    to the visible one: the limited reader shows the move only, as a creation, after looking
    back over the four revisions it omits. The factor catches a walk that is not bounded by
    the revisions it omits, or one that reads every revision of the log per item."""
    env = StoreEnv(backend, "inventory", tmp_path)
    visible_type, hidden_type = env.builder.concrete_classes[:2]
    moved: list[EntityId] = []
    env.write(
        lambda d: moved.extend(d.create(hidden_type, alias=None) for _ in range(MOVED_ENTITIES))
    )
    for _ in range(HIDDEN_REVISIONS - 1):
        env.write(lambda d: [d.update(e) for e in moved])
    env.migrate(lambda d: [d.revise(e, type=TermRef(visible_type)) for e in moved])
    allowance = ALLOWANCE_S.get(backend, DEFAULT_ALLOWANCE_S)
    raw: Snapshot = env.store.snapshot()
    limited: Snapshot = VisibleSnapshot(env.store.snapshot(), ReadView(frozenset({visible_type})))

    def page(view: Snapshot) -> dict[str, object]:
        return change_page(
            view,
            raw,
            FeedPosition(0),
            MOVED_ENTITIES * (HIDDEN_REVISIONS + 1),
            session=None,
            migration=lambda _: None,
        )

    unlimited = best_of(partial(page, raw), FEED_REPEATS)
    paid = best_of(partial(page, limited), FEED_REPEATS)
    assert paid <= FACTOR * unlimited + allowance, (
        f"{paid * 1000:.1f} ms for the limited reader, {unlimited * 1000:.1f} ms for the "
        "unlimited one"
    )
    # the limit filters something: the limited page holds the moves only, as creations
    everything, shown = page(raw), page(limited)
    assert len(everything["items"]) == HIDDEN_REVISIONS + 1  # type: ignore[arg-type]
    (entry,) = shown["items"]  # type: ignore[misc]
    assert len(entry["entities"]) == MOVED_ENTITIES
    assert all(
        item["changed"] is None and item["rev"] == HIDDEN_REVISIONS + 1
        for item in entry["entities"]
    )
