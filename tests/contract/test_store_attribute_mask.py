"""The attribute mask in query and neighbor reads on every store (contracts B13.5; field
visibility plan F5, step 3).

The memory store, the file store with and without the SQLite index and the replay view at an
earlier seq (``--as-of``) must answer a query with ``attributes`` exactly as the reference
evaluator answers the same query without a mask over revisions that never held the hidden
attributes ("as if the type did not have it"), and agree with explicit expectations on a small
graph of two types that declare the same attribute name independently (siblings).
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace

import pytest
from support.stores import StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import (
    Cmp,
    NeighborQuery,
    Not,
    OrderKey,
    Pred,
    Query,
    RelConstraint,
)
from graph_cli.kernel.query_eval import Universe, run_neighbors, run_query
from graph_cli.kernel.read_view import AttributeMask
from graph_cli.kernel.records import EdgeRecord, Revision, revision_content_hash, strip_revision
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files.store import FileStore

REL = TermRef("core:source")
ALL_STATUSES = frozenset({"active", "superseded", "retracted"})
HEAD = 3


@dataclass
class World:
    """``A*`` and ``B*`` are of two sibling types; both store ``code`` and ``secret``."""

    env: StoreEnv
    ids: dict[str, EntityId]
    a: TermRef
    b: TermRef

    def names(self, entities: Iterable[EntityId]) -> list[str]:
        by_id = {entity: name for name, entity in self.ids.items()}
        return [by_id[entity] for entity in entities]

    @property
    def mask(self) -> AttributeMask:
        """``code`` and ``rank`` visible on A, ``secret`` on B: the same name ``code`` is
        visible on one sibling and hidden on the other."""
        return ((self.a, frozenset({"code", "rank"})), (self.b, frozenset({"secret"})))

    @property
    def only_a(self) -> AttributeMask:
        """B has no entry: its entities keep no attribute."""
        return ((self.a, frozenset({"code", "rank", "secret"})),)


@pytest.fixture
def world(env: StoreEnv) -> World:
    """Three txs: everything; A2's secret changes; B2 gets a code and A3 is retracted."""
    classes = env.builder.concrete_classes
    assert len(classes) >= 2
    a, b = TermRef(classes[0]), TermRef(classes[1])
    ids: dict[str, EntityId] = {}
    values: dict[str, dict[str, JsonValue]] = {
        "A1": {"code": "x", "secret": "s1", "rank": 3},
        "A2": {"code": "y", "secret": "s2", "rank": 1},
        "A3": {"secret": "s3", "rank": 2},
        "B1": {"code": "x", "secret": "s1"},
        "B2": {"rank": 0},
    }

    def build(d: TxDraft) -> None:
        for name, attrs in values.items():
            ids[name] = d.create(a if name.startswith("A") else b, attrs=attrs)
        d.link(ids["A1"], REL, ids["B1"])
        d.link(ids["B1"], REL, ids["A2"])

    env.write(build)
    env.write(lambda d: d.update(ids["A2"], {"secret": "s9"}))

    def third(d: TxDraft) -> None:
        d.update(ids["B2"], {"code": "z"})
        d.retract(ids["A3"])

    env.write(third)
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


class Stripped:
    """A universe whose revisions never held what ``mask`` hides: the oracle."""

    def __init__(self, inner: Universe, mask: AttributeMask) -> None:
        self._inner = inner
        self._keep = dict(mask)

    def _strip(self, revision: Revision) -> Revision:
        keep = self._keep.get(revision.type, frozenset())
        return strip_revision(revision, None, keep, rehash=True)

    def current(self, entity: EntityId) -> Revision | None:
        found = self._inner.current(entity)
        return None if found is None else self._strip(found)

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        for revision in self._inner.current_revisions(types):
            yield self._strip(revision)

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self._inner.incoming(entity, rel)


def eq(field: str, value: JsonValue) -> Cmp:
    return Cmp(field, "eq", value)


def query_cases(world: World) -> dict[str, tuple[Query, list[str]]]:
    """Name -> (query with ``world.mask``, the names it returns at the head in order)."""
    mask = world.mask

    def q(where: Pred | None = None, **changes: object) -> Query:
        return replace(Query(where=where, attributes=mask), **changes)  # type: ignore[arg-type]

    by_id = sorted(["A1", "A2", "B1", "B2"], key=lambda n: world.ids[n])
    return {
        "sibling: a value hidden on one type": (q(eq("code", "x")), ["A1"]),
        "sibling: the other way round": (q(eq("secret", "s1")), ["B1"]),
        "exists": (q(Cmp("code", "exists")), ["A1", "A2"]),
        "exists false": (q(Cmp("code", "exists", False)), _ids(world, "B1", "B2")),
        "ne needs a visible value": (q(Cmp("code", "ne", "x")), ["A2"]),
        "not": (q(Not(eq("code", "x"))), _ids(world, "A2", "B1", "B2")),
        "a hidden value never matches": (q(eq("secret", "s9")), []),
        "typed and kept": (q(eq("code", "x"), types=(world.a,)), ["A1"]),
        "typed and hidden": (q(eq("code", "z"), types=(world.b,)), []),
        "order by a partly hidden attribute": (
            q(order=(OrderKey("rank", numeric=True),)),
            ["A2", "A1", *_ids(world, "B1", "B2")],  # B's rank is hidden: missing, last
        ),
        "order descending": (
            q(order=(OrderKey("secret", descending=True),)),
            ["B1", *_ids(world, "A1", "A2", "B2")],  # A's secret is hidden
        ),
        "no attribute named": (q(), by_id),
        "a system field only": (q(eq("type", world.b)), _ids(world, "B1", "B2")),
        "all statuses": (
            q(Cmp("rank", "exists"), record_status=ALL_STATUSES),
            _ids(world, "A1", "A2", "A3"),
        ),
        "type outside the map": (
            replace(q(Cmp("secret", "exists")), attributes=world.only_a),
            ["A1", "A2"],  # B1's secret is gone with every other attribute of B
        ),
        "with a type limit": (
            q(Cmp("code", "exists"), visible=frozenset({world.a})),
            ["A1", "A2"],
        ),
    }


def _ids(world: World, *names: str) -> list[str]:
    return sorted(names, key=lambda n: world.ids[n])


def test_masked_queries_on_every_store(world: World) -> None:
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        for case, (query, expected) in query_cases(world).items():
            page = snapshot.query(query)
            assert world.names(r.id for r in page.items) == expected, (label, case)
            assert page.total == len(expected), (label, case)


def test_masked_queries_answer_as_if_the_attributes_were_never_stored(world: World) -> None:
    reference = reference_of(world)
    cases = [query for query, _ in query_cases(world).values()]
    cases += [
        Query(attributes=world.mask, where=Cmp("code", "exists"), limit=1, offset=1),
        Query(attributes=world.mask, order=(OrderKey("code"),), limit=2, offset=1),
        Query(attributes=world.mask, rels=(RelConstraint(REL, "out"),)),
        Query(attributes=world.mask, where=Cmp("rank", "exists", False)),
    ]
    for label, snapshot_at in readers(world):
        for seq in range(1, HEAD + 1):
            snapshot, view = snapshot_at(seq), reference.at(seq)
            for query in cases:
                assert query.attributes is not None
                oracle = Stripped(view, query.attributes)
                expected = run_query(oracle, replace(query, attributes=None))
                assert snapshot.query(query) == expected, (label, seq, query)


def test_total_counts_what_the_caller_can_see(world: World) -> None:
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        masked = Query(where=Cmp("code", "exists"), attributes=world.mask, limit=1)
        page = snapshot.query(masked)
        assert (page.total, len(page.items)) == (2, 1), label
        assert snapshot.query(replace(masked, attributes=None)).total == 4, label


def test_returned_revisions_lose_the_hidden_attributes_and_are_rehashed(world: World) -> None:
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        raw = {r.id: r for r in snapshot.query(Query()).items}
        items = {
            world.names([r.id])[0]: r for r in snapshot.query(Query(attributes=world.mask)).items
        }
        a1, b1 = items["A1"], items["B1"]
        assert a1.attrs == {"code": "x", "rank": 3}, label
        assert b1.attrs == {"secret": "s1"}, label
        assert a1.edges == raw[world.ids["A1"]].edges  # the mask leaves edges alone
        for name, revision in items.items():
            before = raw[world.ids[name]]
            assert (revision.rev, revision.tx_seq, revision.id) == (
                before.rev,
                before.tx_seq,
                before.id,
            ), (label, name)
            assert revision.content_hash == revision_content_hash(revision), (label, name)
        outside = {
            world.names([r.id])[0]: r for r in snapshot.query(Query(attributes=world.only_a)).items
        }
        assert outside["B1"].attrs == {} and outside["B1"].edges == raw[world.ids["B1"]].edges
        assert outside["B1"].type == world.b and outside["B1"].alias == raw[world.ids["B1"]].alias
        # The unmasked query hands the stored revisions out unchanged.
        assert snapshot.query(Query()).items == tuple(raw.values())


def test_content_hash_is_compared_on_the_stripped_revision(world: World) -> None:
    for label, snapshot_at in readers(world):
        snapshot = snapshot_at(HEAD)
        stored = snapshot.get(world.ids["A1"])
        assert stored is not None
        shown = strip_revision(stored, None, {"code", "rank"}, rehash=True)
        assert shown.content_hash != stored.content_hash
        found = snapshot.query(
            Query(where=eq("content_hash", shown.content_hash), attributes=world.mask)
        )
        assert world.names(r.id for r in found.items) == ["A1"], label
        hidden = Query(where=eq("content_hash", stored.content_hash), attributes=world.mask)
        assert snapshot.query(hidden).items == (), label
        ordered = snapshot.query(Query(order=(OrderKey("content_hash"),), attributes=world.mask))
        hashes = [r.content_hash for r in ordered.items]
        assert hashes == sorted(hashes), label


def test_masked_neighbors_on_every_store(world: World) -> None:
    reference = reference_of(world)
    for label, snapshot_at in readers(world):
        for seq in (HEAD, 2, 1):
            snapshot, view = snapshot_at(seq), reference.at(seq)
            for start in ("A1", "B1", "A2"):
                q = NeighborQuery(world.ids[start], None, "both", 2, None, world.mask)
                found = list(snapshot.neighbors(q))
                assert found == run_neighbors(view, q), (label, seq, start)
                oracle = run_neighbors(Stripped(view, world.mask), replace(q, attributes=None))
                assert [(e, edge) for e, edge, _ in found] == [(e, x) for e, x, _ in oracle]
                for (_, _, mine), (_, _, theirs) in zip(found, oracle, strict=True):
                    assert mine == theirs, (label, seq, start)
        snapshot = snapshot_at(HEAD)
        ((entity, _, revision),) = snapshot.neighbors(
            NeighborQuery(world.ids["A1"], None, "out", 1, None, world.mask)
        )
        assert entity == world.ids["B1"] and revision.attrs == {"secret": "s1"}, label
