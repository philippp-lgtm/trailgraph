"""``VisibleSnapshot`` on every store (type read limits plan V5, V9).

A snapshot that shows the entities of one type only. Everything a hidden entity answers must be
what the raw snapshot answers for an id it does not hold; every revision handed out loses its
edges to hidden entities and keeps a consistent hash.
"""

import ast
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest
from harness import REPO_ROOT
from support.stores import StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import NeighborQuery, Query
from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import revision_content_hash
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.temporal_knowledge import changes

REL = TermRef("core:source")
MISSING = EntityId(UUID(int=7))
HEAD = 3


@dataclass
class World:
    """``A*`` are of the visible type, ``B*`` of the hidden one."""

    env: StoreEnv
    ids: dict[str, EntityId]
    a: str
    b: str

    def raw(self, seq: int | None = None) -> Snapshot:
        return self.env.store.snapshot(at_seq=seq)

    def view(self, seq: int | None = None, *, over: Snapshot | None = None) -> VisibleSnapshot:
        return VisibleSnapshot(over or self.raw(seq), ReadView(frozenset({self.a})))

    def names(self, entities: list[EntityId]) -> list[str]:
        by_id = {entity: name for name, entity in self.ids.items()}
        return sorted(by_id[e] for e in entities)


@pytest.fixture
def world(env: StoreEnv) -> World:
    """A1 -> B1, A2; A2 -> B1; A3 -> A1; B1 -> A1; B2 -> B1.

    Three txs: everything (A1 and B1 with an alias); B2 retracted; A2 updated.
    """
    classes = env.builder.concrete_classes
    a, b = classes[0], classes[1]
    ids: dict[str, EntityId] = {}

    def build(d: TxDraft) -> None:
        ids["A1"] = d.create(a, alias="AA-0001")
        ids["A2"] = d.create(a, alias=None)
        ids["A3"] = d.create(a, alias=None)
        ids["B1"] = d.create(b, alias="BB-0001")
        ids["B2"] = d.create(b, alias=None)
        for source, target in (
            ("A1", "B1"),
            ("A1", "A2"),
            ("A2", "B1"),
            ("A3", "A1"),
            ("B1", "A1"),
            ("B2", "B1"),
        ):
            d.link(ids[source], REL, ids[target])

    env.write(build, reason="build the graph")
    env.write(lambda d: d.retract(ids["B2"]))
    env.write(lambda d: d.update(ids["A2"]))
    return World(env, ids, a, b)


def test_a_hidden_entity_answers_as_a_missing_one(world: World) -> None:
    raw, view = world.raw(), world.view()
    hidden = world.ids["B1"]
    assert view.get(hidden) is None and raw.get(MISSING) is None
    assert view.get_many([hidden, MISSING]) == raw.get_many([MISSING])
    assert view.get_revision(hidden, 1) is None and raw.get_revision(MISSING, 1) is None
    assert view.history(hidden) == raw.history(MISSING) == []
    assert view.incoming(hidden) == raw.incoming(MISSING) == []
    assert view.incoming(hidden, REL) == raw.incoming(MISSING, REL)
    assert not view.sees(hidden) and not view.sees(MISSING)


def test_a_visible_entity_loses_its_hidden_edges_and_keeps_a_consistent_hash(world: World) -> None:
    raw, view = world.raw(), world.view()
    stored = raw.get(world.ids["A1"])
    shown = view.get(world.ids["A1"])
    assert stored is not None and shown is not None
    assert [e.target for e in stored.edges] == [world.ids["B1"], world.ids["A2"]]
    assert [e.target for e in shown.edges] == [world.ids["A2"]]
    assert shown.edges[0] == stored.edges[1]  # the kept edge keeps its id
    assert shown.content_hash == revision_content_hash(shown) != stored.content_hash
    assert (shown.rev, shown.tx_seq, shown.attrs) == (stored.rev, stored.tx_seq, stored.attrs)
    # without a hidden edge the stored revision itself is handed out
    assert view.get(world.ids["A3"]) == raw.get(world.ids["A3"])
    many = view.get_many([world.ids["A1"], world.ids["A3"], world.ids["B1"]])
    assert set(many) == {world.ids["A1"], world.ids["A3"]}
    assert many[world.ids["A1"]] == shown


def test_get_revision_and_history_are_stripped_and_hashes_match(world: World) -> None:
    raw, view = world.raw(), world.view()
    a2 = world.ids["A2"]
    assert [m.rev for m in view.history(a2)] == [m.rev for m in raw.history(a2)] == [1, 2]
    for meta in view.history(a2):
        revision = view.get_revision(a2, meta.rev)
        assert revision is not None
        assert meta.content_hash == revision.content_hash == revision_content_hash(revision)
        assert all(e.target != world.ids["B1"] for e in revision.edges)
        before = raw.get_revision(a2, meta.rev)
        assert before is not None and meta.content_hash != before.content_hash
        assert (meta.rev, meta.tx_seq, meta.tx_id) == (
            revision.rev,
            revision.tx_seq,
            revision.tx_id,
        )
    assert view.get_revision(a2, 3) is None
    # an entity without hidden edges has the raw history
    assert view.history(world.ids["A3"]) == raw.history(world.ids["A3"])


def test_a_past_revision_is_judged_by_the_current_type(world: World) -> None:
    other = VisibleSnapshot(world.raw(), ReadView(frozenset({world.b})))
    # B2 is retracted but of a visible type for ``other``: still visible, edges to A* stripped
    assert other.get(world.ids["B2"]) is not None
    assert other.get_revision(world.ids["A1"], 1) is None
    # as of seq 1 an entity created later has no current revision and no revision either
    early = VisibleSnapshot(world.raw(1), ReadView(frozenset({world.a})))
    assert early.get_revision(world.ids["A2"], 2) is None
    assert [m.rev for m in early.history(world.ids["A2"])] == [1]


def test_incoming_keeps_visible_sources_only(world: World) -> None:
    raw, view = world.raw(), world.view()
    assert world.names([s for s, _ in raw.incoming(world.ids["A1"])]) == ["A3", "B1"]
    assert world.names([s for s, _ in view.incoming(world.ids["A1"])]) == ["A3"]
    assert world.names([s for s, _ in view.incoming(world.ids["A2"], REL)]) == ["A1"]
    assert view.incoming(world.ids["A3"]) == []


def test_aliases_of_hidden_entities_are_unknown(world: World) -> None:
    raw, view = world.raw(), world.view()
    assert view.resolve_alias("AA-0001") == raw.resolve_alias("AA-0001") is not None
    assert raw.resolve_alias("BB-0001") is not None
    assert view.resolve_alias("BB-0001") is None
    assert view.resolve_alias("ZZ-0001") is None


def test_match_suffix_never_lets_hidden_ids_eat_the_limit(world: World) -> None:
    raw, view = world.raw(), world.view()
    assert view.match_suffix("") != []  # every id ends with ""
    everything = list(raw.match_suffix("", limit=100))
    assert len(everything) == 5
    assert world.names(list(view.match_suffix("", limit=100))) == ["A1", "A2", "A3"]
    # the limit applies to what is visible: the first two visible ids, whichever hidden ones sort
    # before them
    first = [e for e in everything if view.sees(e)][:2]
    assert list(view.match_suffix("", limit=2)) == first
    own = world.ids["B1"].hex[-6:]
    assert view.match_suffix(own) == []
    assert world.ids["A1"] in view.match_suffix(world.ids["A1"].hex[-6:])


def test_tx_shows_metadata_only(world: World) -> None:
    raw, view = world.raw(), world.view()
    assert view.tx(0) is None and view.tx(HEAD + 1) is None
    stored, shown = raw.tx(1), view.tx(1)
    assert stored is not None and shown is not None
    assert stored.revisions and stored.alias_allocations and stored.changeset
    assert (shown.changeset, shown.resolution) == ({}, {})
    assert (shown.revisions, shown.alias_allocations, shown.blobs) == ((), (), ())
    assert shown == replace(
        stored, changeset={}, resolution={}, revisions=(), alias_allocations=(), blobs=()
    )
    assert shown.reason == "build the graph" and shown.request == stored.request


def test_seq_and_warnings_pass_through(world: World) -> None:
    raw, view = world.raw(), world.view()
    assert (view.seq, view.warnings) == (raw.seq, raw.warnings)
    assert world.view(2).seq == 2


def test_queries_carry_the_visible_set(world: World) -> None:
    view = world.view()
    page = view.query(Query())
    assert world.names([r.id for r in page.items]) == ["A1", "A2", "A3"] and page.total == 3
    assert all(r.content_hash == revision_content_hash(r) for r in page.items)
    only = {r.id: r for r in page.items}
    assert [e.target for e in only[world.ids["A1"]].edges] == [world.ids["A2"]]
    # the type filter of the caller narrows, never widens
    hidden_type = view.query(Query(types=(TermRef(world.b),)))
    assert hidden_type.items == () and hidden_type.total == 0
    both = view.query(Query(types=(TermRef(world.a), TermRef(world.b))))
    assert both.total == 3
    # a caller-set visible set is intersected
    nothing = view.query(Query(visible=frozenset({TermRef(world.b)})))
    assert nothing.total == 0
    # all statuses: B2 is retracted and hidden anyway
    page = view.query(Query(record_status=frozenset({"active", "retracted"})))
    assert page.total == 3


def test_neighbors_never_walk_a_hidden_node(world: World) -> None:
    view = world.view()
    found = view.neighbors(NeighborQuery(world.ids["A3"], None, "out", 3))
    assert world.names([e for e, _, _ in found]) == ["A1", "A2"]  # B1 behind A1 and A2 is hidden
    assert all(r.content_hash == revision_content_hash(r) for _, _, r in found)
    assert view.neighbors(NeighborQuery(world.ids["B1"], None, "both", 2)) == []
    assert view.neighbors(NeighborQuery(world.ids["A1"], None, "in", 2, frozenset())) == []


def test_every_type_visible_equals_the_raw_snapshot(world: World) -> None:
    raw = world.raw()
    everything = frozenset({world.a, world.b})
    view = VisibleSnapshot(raw, ReadView(everything))
    for name, entity in world.ids.items():
        assert view.get(entity) == raw.get(entity), name
        assert view.history(entity) == list(raw.history(entity)), name
        assert view.incoming(entity) == list(raw.incoming(entity)), name
    assert view.query(Query(record_status=frozenset({"active", "retracted"}))).total == 5


# ---- no command prints a transaction body --------------------------------------------------

TX_BODY = {"changeset", "resolution", "revisions", "alias_allocations", "blobs"}
TX_READS = {"tx", "changes_since"}  # the calls that hand out a tx record, body included
TX_READERS = {  # the modules of the app and server distributions that read a tx through a snapshot
    "services/read.py",  # history and the change feed: request, reason, actor session, tx id;
    # as-of: ontology version
    "services/visibility.py",  # the wrapper itself
    "temporal_knowledge/as_of.py",  # the time of a tx
    "temporal_knowledge/changes.py",  # the change feed's raw read: ``touched``, see below
    "kernel/migration_marker.py",  # marker_committed: the tx id at a seq, on a raw snapshot
    "services/names.py",  # the mapped layer below the view: delegates, and the migration record
}
# Modules that read a tx body on purpose, below the read limits, and hand out only what the
# caller may see (``history``'s ``migration``: versions, summary, what changed the entity).
BODY_READERS = {"services/names.py"}
# Modules that read the revisions of a tx: the change feed takes ``(id, rev)`` pairs of a raw tx
# (``touched``) and reads every revision through the caller's view (change feed plan CF4).
REVISION_READERS = {"temporal_knowledge/changes.py"}
WRITE_SIDE = {"temporal_knowledge/working.py"}  # the write path's overlay; it delegates to a base


def tx_calls_and_body_reads(root: Path) -> tuple[set[str], set[str], set[str]]:
    """The modules that call ``.tx(`` or ``.changes_since(``, those of them that touch a body
    field other than ``revisions``, and those that touch ``revisions``."""
    callers: set[str] = set()
    readers: set[str] = set()
    revision_readers: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("storage/", "kernel/ports/")) or relative in WRITE_SIDE:
            continue
        tree = ast.parse(path.read_text())
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        calls = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in TX_READS
            for n in ast.walk(tree)
        )
        if calls:
            callers.add(relative)
            if attributes & TX_BODY - {"revisions"}:
                readers.add(relative)
            if "revisions" in attributes:
                revision_readers.add(relative)
    return callers, readers, revision_readers


def test_no_read_command_reads_the_body_of_a_tx() -> None:
    """The view hands out tx metadata only, and nothing on the read side asks for more: a
    module that reads a tx (``tx`` or ``changes_since``) must not touch its changeset,
    resolution, alias allocations or blobs, only the change feed's core touches its revisions,
    and the modules that read one are the known ones."""
    callers, readers, revision_readers = tx_calls_and_body_reads(
        REPO_ROOT / "packages" / "graph_cli" / "src" / "graph_cli"
    )
    server, _, _ = tx_calls_and_body_reads(REPO_ROOT / "packages" / "graph_cli_server" / "src")
    assert callers == TX_READERS
    assert server == set()
    assert readers == BODY_READERS
    assert revision_readers == REVISION_READERS
    # positive controls: the scanner sees an attribute read next to a tx call, and a
    # ``changes_since`` call as a tx read
    probe = ast.parse("def f(s):\n    return s.tx(1).changeset\n")
    assert any(isinstance(n, ast.Attribute) and n.attr in TX_BODY for n in ast.walk(probe))
    feed = ast.parse("def f(s):\n    return s.changes_since(0)\n")
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in TX_READS
        for n in ast.walk(feed)
    )


def test_the_feed_takes_ids_and_revision_numbers_only_from_a_raw_tx() -> None:
    """``touched`` is the feed's one read of a raw tx body (CF4): it hands out ``(id, rev)``
    pairs and nothing of what a revision holds."""
    source = ast.parse(Path(changes.__file__).read_text())
    body_reads = {
        node.name
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(n, ast.Attribute) and n.attr in TX_BODY for n in ast.walk(node))
    }
    assert body_reads == {"touched"}
