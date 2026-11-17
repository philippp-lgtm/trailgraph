"""The definition of "as if absent" (type read limits plan V5, section 6 acceptance).

For random small graphs (seeded random transactions of the toy fixtures: creates, updates, links,
unlinks, aliases, supersessions, retractions) and random sets of visible types, every answer over
``VisibleSnapshot`` equals the answer over a store that holds only the visible entities: the
revisions of those entities copied verbatim (same ``rev``, ``tx_seq``, ``tx_id``, times), but for
the edges to entities that are not copied, which are dropped, and the content hash recomputed.
That reference is a plain in-memory ``GraphView`` replay, so the comparison tests the filter
against the evaluation the memory store has always run.

The same holds with a random attribute half (field visibility plan F4, F5, section 6): per
concrete type a random set of visible attributes, and the reference store holds the revisions
without the others (a type without an entry loses every attribute), the content hash recomputed.
The queries then also filter, test and sort by attributes.

Compared: ``get``, ``get_many``, ``get_revision``, ``history``, ``incoming``, ``resolve_alias``,
``match_suffix``, ``query`` (types, attribute and hash predicates, ``RelConstraint`` out and in
with and without targets, ``exists`` false, statuses, ordering, paging, ``total``) and
``neighbors`` (every direction, depth 1-3, relation filters), at the head and at earlier
sequences. Not compared: the snapshot's ``seq`` and the body of a transaction (a view hands out its
metadata only, by design).

The change feed (change feed plan CF5, section 6) is compared too: every page of the feed over the
view, followed from ``tx:0`` and from the middle of the log through every cursor at several limits
and scan caps, equals the page of the feed over the reference store (which keeps the original
``tx_seq`` of every tx, so a tx that held hidden entities only is an empty one there).
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest
from support.stores import StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Cmp, NeighborQuery, Not, OrderKey, Query, RelConstraint
from graph_cli.kernel.read_view import AttributeMask, ReadView
from graph_cli.kernel.records import Revision, TxRecord, revision_content_hash
from graph_cli.services.read import change_page
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.storage.common.snapshot import ViewSnapshot
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files.store import FileStore
from graph_cli.temporal_knowledge.changes import SCAN_CAP, FeedPosition

CASES = 40  # graphs per backend and toy ontology
MISSING = EntityId(UUID(int=7))
STATUSES = (
    frozenset({"active"}),
    frozenset({"active", "superseded", "retracted"}),
    frozenset({"retracted"}),
    frozenset({"superseded", "retracted"}),
)
LIMITS = (1, 2, 5, 50)
OFFSETS = (0, 0, 0, 1, 3, 40)
FEED_PAGES = ((1, SCAN_CAP), (2, 3), (5, 1), (500, SCAN_CAP))  # (limit, scan cap)


def stripped(
    revision: Revision, kept: set[EntityId], mask: AttributeMask | None = None
) -> Revision:
    """The revision as the store of the visible part holds it: the edges to entities outside it
    and the attributes outside ``mask`` are gone and the content hash is that of what is left."""
    edges = tuple(e for e in revision.edges if e.target in kept)
    attrs = revision.attrs
    if mask is not None:
        keep = dict(mask).get(revision.type, frozenset())
        attrs = {name: value for name, value in revision.attrs.items() if name in keep}
    if edges == revision.edges and attrs == revision.attrs:
        return revision
    cut = replace(revision, edges=edges, attrs=attrs)
    return replace(cut, content_hash=revision_content_hash(cut))


@dataclass
class Sides:
    """The raw store, the visible types, and the store that holds the visible part only."""

    raw: Sequence[TxRecord]
    visible: frozenset[str]
    kept: set[EntityId]
    reference: GraphView

    @property
    def head(self) -> int:
        return len(self.raw)


def split(env: StoreEnv, visible: frozenset[str], mask: AttributeMask | None = None) -> Sides:
    records = list(env.store.changes_since(0, limit=10**6))
    types = {r.id: r.type for tx in records for r in tx.revisions}
    kept = {entity for entity, type_ in types.items() if type_ in visible}
    reference = GraphView()
    for tx in records:
        reference.apply(
            replace(
                tx,
                revisions=tuple(stripped(r, kept, mask) for r in tx.revisions if r.id in kept),
                alias_allocations=tuple((e, a) for e, a in tx.alias_allocations if e in kept),
            )
        )
    return Sides(records, visible, kept, reference)


def more_links(draft: TxDraft, rng: random.Random) -> None:
    for _ in range(rng.randint(2, 5)):
        active = draft.active_ids()
        if active:
            draft.link(rng.choice(active))
    if not draft.work:  # every transaction needs a revision
        draft.create()


def random_graph(env: StoreEnv, rng: random.Random) -> frozenset[str]:
    """Write a random graph; return a random set of visible types (none and all included)."""
    env.random(rng.randint(8, 26))
    for _ in range(rng.randint(3, 8)):  # the random walk links rarely; add edges between types
        env.write(lambda d: more_links(d, rng))
    classes = env.builder.concrete_classes
    extreme = rng.random() < 0.1
    size = rng.choice((0, len(classes))) if extreme else rng.randint(1, len(classes) - 1)
    return frozenset(rng.sample(classes, size))


def pick[T](rng: random.Random, items: Sequence[T], count: int) -> list[T]:
    return rng.sample(list(items), min(count, len(items)))


def random_queries(
    rng: random.Random, ids: Sequence[EntityId], sides: Sides, concrete: Sequence[str]
) -> list[Query]:
    relations = sorted(
        {e.rel for tx in sides.raw for r in tx.revisions for e in r.edges}, key=str
    ) or [TermRef("core:supersedes")]
    hashes = [r.content_hash for tx in sides.raw for r in tx.revisions]
    queries: list[Query] = [Query(), Query(record_status=STATUSES[1], limit=500)]
    for _ in range(16):
        constraints = tuple(
            RelConstraint(
                rng.choice(relations),
                rng.choice(("out", "in")),
                tuple(pick(rng, [*ids, MISSING], rng.choice((0, 0, 1, 2)))),
                rng.random() < 0.7,
            )
            for _ in range(rng.choice((0, 1, 1, 2)))
        )
        where = rng.choice(
            (
                None,
                Cmp("alias", "exists"),
                Not(Cmp("alias", "exists")),
                Cmp("rev", "ge", 2),
                Cmp("content_hash", "eq", rng.choice(hashes)),
            )
        )
        queries.append(
            Query(
                types=tuple(TermRef(t) for t in pick(rng, concrete, rng.choice((0, 0, 1, 2, 3)))),
                where=where,
                rels=constraints,
                record_status=rng.choice(STATUSES),
                changed_since_seq=rng.choice((None, None, 1, sides.head // 2)),
                order=rng.choice(
                    (
                        (),
                        (OrderKey("rev", True),),
                        (OrderKey("alias"),),
                        (OrderKey("content_hash"),),
                    )
                ),
                limit=rng.choice(LIMITS),
                offset=rng.choice(OFFSETS),
            )
        )
    return queries


def compare(label: str, seed: int, got: object, want: object) -> None:
    assert got == want, f"{label} differs (seed {seed})\n  view:      {got}\n  reference: {want}"


def check_point_reads(
    seed: int,
    view: Snapshot,
    ref: Snapshot,
    sides: Sides,
    ids: Sequence[EntityId],
    rng: random.Random,
) -> None:
    everything = [*ids, MISSING]
    for entity in everything:
        compare(f"get {entity}", seed, view.get(entity), ref.get(entity))
        compare(f"history {entity}", seed, list(view.history(entity)), list(ref.history(entity)))
        for rev in (1, 2, 3, 9):
            compare(
                f"get_revision {entity} {rev}",
                seed,
                view.get_revision(entity, rev),
                ref.get_revision(entity, rev),
            )
        compare(f"incoming {entity}", seed, list(view.incoming(entity)), list(ref.incoming(entity)))
    relations = sorted({e.rel for tx in sides.raw for r in tx.revisions for e in r.edges}, key=str)
    for entity in pick(rng, everything, 6):
        for rel in relations:
            compare(
                f"incoming {entity} {rel}",
                seed,
                list(view.incoming(entity, rel)),
                list(ref.incoming(entity, rel)),
            )
    for _ in range(6):
        asked = pick(rng, [*everything, *everything[:3]], rng.randint(0, 8))
        rng.shuffle(asked)
        compare("get_many", seed, dict(view.get_many(asked)), dict(ref.get_many(asked)))
    aliases = {a for tx in sides.raw for _, a in tx.alias_allocations}
    for alias in [*aliases, "ZZ-0001", ""]:
        compare(f"resolve_alias {alias}", seed, view.resolve_alias(alias), ref.resolve_alias(alias))
    suffixes = {e.hex[-n:] for e in ids for n in (1, 2, 4, 8, 32)} | {"", "ffff"}
    for suffix in sorted(suffixes):
        for limit in (1, 3, 10, 1000):
            compare(
                f"match_suffix {suffix!r} {limit}",
                seed,
                list(view.match_suffix(suffix, limit=limit)),
                list(ref.match_suffix(suffix, limit=limit)),
            )


def check_queries(
    seed: int,
    view: Snapshot,
    ref: Snapshot,
    sides: Sides,
    ids: Sequence[EntityId],
    concrete: Sequence[str],
    rng: random.Random,
) -> None:
    for query in random_queries(rng, ids, sides, concrete):
        compare(f"query {query}", seed, view.query(query), ref.query(query))
    relations = sorted({e.rel for tx in sides.raw for r in tx.revisions for e in r.edges}, key=str)
    for entity in [*ids, MISSING]:
        for direction, depth in pick(
            rng, [(d, n) for d in ("out", "in", "both") for n in (1, 2, 3)], 4
        ):
            rels = rng.choice((None, None, tuple(pick(rng, relations, 1))))
            q = NeighborQuery(entity, rels, direction, depth)  # type: ignore[arg-type]
            compare(f"neighbors {q}", seed, list(view.neighbors(q)), list(ref.neighbors(q)))


def feed(
    snapshot: Snapshot, raw: Snapshot, start: FeedPosition, limit: int, cap: int
) -> list[object]:
    """Every page of the change feed after ``start``, each resumed from the ``next`` of the one
    before, until one says nothing more follows."""
    pages: list[object] = []
    position = start
    while True:
        found = change_page(
            snapshot, raw, position, limit, session=None, migration=lambda _: None, scan_cap=cap
        )
        pages.append(found)
        if not found["more"]:
            return pages
        assert len(pages) <= 10 * raw.seq + 10, "the feed does not advance"
        text = str(found["next"]).removeprefix("tx:")
        seq, _, entity = text.partition("/")
        position = FeedPosition(int(seq), EntityId(UUID(entity)) if entity else None)


def check_feed(seed: int, view: Snapshot, raw: Snapshot, ref: Snapshot) -> None:
    for start in sorted({0, raw.seq // 2}):
        for limit, cap in FEED_PAGES:
            got = feed(view, raw, FeedPosition(start), limit, cap)
            want = feed(ref, ref, FeedPosition(start), limit, cap)
            compare(f"changes since tx:{start} limit {limit} cap {cap}", seed, got, want)


@pytest.mark.parametrize("seed", range(CASES))
def test_every_answer_over_the_view_is_the_answer_over_the_visible_part(
    backend: str, ontology: str, tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    env = StoreEnv(backend, ontology, tmp_path, seed=seed)
    visible = random_graph(env, rng)
    sides = split(env, visible)
    concrete = env.builder.concrete_classes
    ids = sorted({r.id for tx in sides.raw for r in tx.revisions})
    stores = [env.store]
    if isinstance(env.store, FileStore):  # the file store reads through its index or without it
        stores.append(FileStore(env.store.layout.root, env.graph_id, use_index=False))
    for at in sorted({1, max(1, sides.head // 2), sides.head}):
        ref = ViewSnapshot(sides.reference.at(at))
        for store in stores:
            raw = store.snapshot(at_seq=at)
            view = VisibleSnapshot(raw, ReadView(visible))
            check_point_reads(seed, view, ref, sides, ids, rng)
            check_queries(seed, view, ref, sides, ids, concrete, rng)
            if at == sides.head:  # the feed reads up to the head
                check_feed(seed, view, raw, ref)
            # a hidden entity is a missing one: no revision, history, edges or neighbours
            for entity in ids:
                if entity not in sides.kept:
                    assert view.get(entity) is None and not view.history(entity)


def random_mask(rng: random.Random, env: StoreEnv) -> AttributeMask:
    """Per concrete type a random set of its attributes (none and all included); now and then
    a type without an entry."""
    found = []
    for name in env.builder.concrete_classes:
        if rng.random() < 0.1:
            continue  # no entry: the type loses every attribute
        attributes = sorted(env.builder.model.classes[name].attributes)
        size = rng.choice((0, len(attributes))) if rng.random() < 0.2 else None
        count = size if size is not None else rng.randint(0, len(attributes))
        found.append((name, frozenset(rng.sample(attributes, count))))
    return tuple(found)


def attribute_queries(rng: random.Random, sides: Sides, concrete: Sequence[str]) -> list[Query]:
    """Queries that test, compare and sort by attributes (masked or not), on sibling types and
    without a type."""
    values: dict[str, list[object]] = {}
    for tx in sides.raw:
        for revision in tx.revisions:
            for name, value in revision.attrs.items():
                if isinstance(value, str | int | bool):
                    values.setdefault(name, []).append(value)
    names = sorted(values)
    if not names:
        return []
    queries = []
    for _ in range(14):
        name = rng.choice(names)
        where = rng.choice(
            (
                Cmp(name, "exists"),
                Not(Cmp(name, "exists")),
                Cmp(name, "eq", rng.choice(values[name])),
                Cmp(name, "ne", rng.choice(values[name])),
                None,
            )
        )
        queries.append(
            Query(
                types=tuple(TermRef(t) for t in pick(rng, concrete, rng.choice((0, 0, 1, 2)))),
                where=where,
                record_status=rng.choice(STATUSES),
                order=rng.choice(
                    ((), (OrderKey(rng.choice(names)),), (OrderKey(rng.choice(names), True),))
                ),
                limit=rng.choice(LIMITS),
                offset=rng.choice(OFFSETS),
            )
        )
    return queries


@pytest.mark.parametrize("seed", range(CASES // 2))
def test_every_answer_over_an_attribute_view_is_the_answer_over_the_store_without_them(
    backend: str, ontology: str, tmp_path: Path, seed: int
) -> None:
    rng = random.Random(1000 + seed)
    env = StoreEnv(backend, ontology, tmp_path, seed=seed)
    visible = random_graph(env, rng)
    if rng.random() < 0.5:  # half of the cases without a type half
        visible = frozenset(env.builder.concrete_classes)
        types: frozenset[str] | None = None
    else:
        types = visible
    mask = random_mask(rng, env)
    sides = split(env, visible, mask)
    concrete = env.builder.concrete_classes
    ids = sorted({r.id for tx in sides.raw for r in tx.revisions})
    for at in sorted({1, max(1, sides.head // 2), sides.head}):
        ref = ViewSnapshot(sides.reference.at(at))
        raw = env.store.snapshot(at_seq=at)
        view = VisibleSnapshot(raw, ReadView(types, mask))
        check_point_reads(seed, view, ref, sides, ids, rng)
        check_queries(seed, view, ref, sides, ids, concrete, rng)
        if at == sides.head:
            check_feed(seed, view, raw, ref)
        for query in attribute_queries(rng, sides, concrete):
            compare(f"query {query}", seed, view.query(query), ref.query(query))


def test_the_comparison_catches_a_view_that_shows_too_much(env: StoreEnv) -> None:
    """Positive control: a view of one type more than the reference holds differs from it, so the
    comparison above does not pass for want of anything to compare."""
    first, second = env.builder.concrete_classes[:2]
    env.write(lambda d: (d.create(first), d.create(second)))
    sides = split(env, frozenset({first}))
    everything = Query(record_status=STATUSES[1])
    reference = ViewSnapshot(sides.reference.at()).query(everything)
    exact = VisibleSnapshot(env.store.snapshot(), ReadView(frozenset({first}))).query(everything)
    wider = VisibleSnapshot(env.store.snapshot(), ReadView(frozenset({first, second}))).query(
        everything
    )
    assert exact == reference and reference.total == 1
    assert wider != reference and wider.total == 2


def test_the_comparison_catches_a_view_that_shows_an_attribute_too_many(env: StoreEnv) -> None:
    """Positive control of the attribute half: a view that keeps one attribute more than the
    reference holds differs from it."""
    first = env.builder.concrete_classes[0]
    env.write(lambda d: d.create(first))
    current = env.store.snapshot().query(Query(record_status=STATUSES[1])).items
    held = sorted(attribute for r in current for attribute in r.attrs)
    assert held, "the created entity holds an attribute"
    everything = Query(record_status=STATUSES[1])
    narrow = ((first, frozenset(held[1:])),)
    wide = ((first, frozenset(held)),)
    reference = ViewSnapshot(split(env, frozenset({first}), narrow).reference.at()).query(
        everything
    )
    exact = VisibleSnapshot(env.store.snapshot(), ReadView(None, narrow)).query(everything)
    wider = VisibleSnapshot(env.store.snapshot(), ReadView(None, wide)).query(everything)
    assert exact == reference and wider != reference


def test_the_feed_comparison_catches_a_view_that_shows_too_much(env: StoreEnv) -> None:
    """Positive control of the feed: over a view of one type more, or of one attribute more,
    than the reference holds, the feed differs from the reference feed."""
    first, second = env.builder.concrete_classes[:2]
    created: list[EntityId] = []
    env.write(lambda d: created.extend((d.create(first), d.create(second))))
    env.write(lambda d: d.update(created[0], {"extra": "changed"}))
    start = FeedPosition(0)
    sides = split(env, frozenset({first}))
    raw = env.store.snapshot()
    ref = ViewSnapshot(sides.reference.at())
    reference = feed(ref, ref, start, 50, SCAN_CAP)
    exact = VisibleSnapshot(raw, ReadView(frozenset({first})))
    wider = VisibleSnapshot(raw, ReadView(frozenset({first, second})))
    assert feed(exact, raw, start, 50, SCAN_CAP) == reference
    assert feed(wider, raw, start, 50, SCAN_CAP) != reference
    narrow = ((first, frozenset()),)
    masked = ViewSnapshot(split(env, frozenset({first}), narrow).reference.at())
    hidden_attrs = VisibleSnapshot(raw, ReadView(frozenset({first}), narrow))
    assert feed(hidden_attrs, raw, start, 50, SCAN_CAP) == feed(masked, masked, start, 50, SCAN_CAP)
    assert feed(exact, raw, start, 50, SCAN_CAP) != feed(masked, masked, start, 50, SCAN_CAP)
