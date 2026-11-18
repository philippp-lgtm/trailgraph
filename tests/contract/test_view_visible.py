"""Views through ``VisibleSnapshot`` (views plan V10, leak catalogue 1; step 6 hooks of the
reviews): a reader who does not see an entity gets it in no row, no neighbour, no count and no id,
also when the entity is hidden by the class it was *stored* under (F1b: a revision from a query
keeps its edges to it while ``get_many`` answers nothing), and with the load budget of a view
spent.

Every test runs on each backend and toy ontology: the view names a visible class ``A`` of the
ontology's first two concrete classes and the relation ``core:source``; ``B`` is hidden.
"""

from dataclasses import dataclass, replace
from typing import Any

import pytest
from support.stores import StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import Revision, revision_content_hash
from graph_cli.kinetic import card
from graph_cli.kinetic.card import render, spec_of
from graph_cli.kinetic.view_compile import compile_views
from graph_cli.kinetic.view_entries import ViewEntry, load_view_entries
from graph_cli.kinetic.view_page import render_set
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.names import MappedSnapshot
from graph_cli.services.visibility import VisibleSnapshot, filter_model

REL = TermRef("core:source")
ENTITY_VIEW = """\
schema: graph_cli.view/1
name: around
summary: What points at an entity and what it points at.
use_when: Neighbours of an entity in both directions, as an entity view.
takes: {a}
sections:
  - kind: neighbors
    id: sources
    relation: core:source
    direction: in
    fields: all
    relations: all
  - kind: neighbors
    id: targets
    relation: core:source
    fields: all
    relations: all
"""
SET_VIEW = """\
schema: graph_cli.view/1
name: everything
summary: Every entity of the visible class with its relations.
use_when: Every entity of a class with the neighbours it points at, as a set view.
sections:
  - kind: list
    id: rows
    type: {a}
    fields: all
    relations: all
"""


@dataclass
class World:
    """``A1 -> B1, A2``; ``A2 -> B1``; ``A3 -> A1, B2``; ``B1 -> A1``; ``B2 -> B1``. ``A*`` are
    of the visible class, ``B*`` of the hidden one; B1 and A1 have aliases."""

    env: StoreEnv
    ids: dict[str, EntityId]
    a: str
    b: str

    def raw(self, seq: int | None = None) -> Snapshot:
        return self.env.store.snapshot(at_seq=seq)

    @property
    def model(self) -> CompiledModel:
        return self.env.builder.model

    def limited(self) -> CompiledModel:
        return filter_model(self.model, frozenset({self.a}))

    def view(self, snapshot: Snapshot | None = None) -> VisibleSnapshot:
        return VisibleSnapshot(snapshot or self.raw(), ReadView(frozenset({self.a})))

    def entry(self, source: str) -> ViewEntry:
        text = source.format(a=self.a)
        document, problems = compile_views(
            {"probe.yaml": text.replace("probe", "probe")}, self.model
        )
        assert [p.to_json() for p in problems] == []
        return next(e for e in load_view_entries(document))

    def around(self, snapshot: Snapshot, entity: str, *, model: CompiledModel | None = None) -> Any:
        found = snapshot.get(self.ids[entity])
        assert found is not None
        entry = self.entry(ENTITY_VIEW.replace("name: around", "name: probe"))
        made = render(
            spec_of(entry, model or self.limited(), {}), found, snapshot, model or self.limited()
        )
        return made.to_json()

    def everything(self, snapshot: Snapshot) -> Any:
        entry = self.entry(SET_VIEW.replace("name: everything", "name: probe"))
        return render_set(spec_of(entry, self.limited(), {}), snapshot, self.limited()).to_json()


def build_world(env: StoreEnv) -> World:
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
            ("A3", "B2"),
            ("B1", "A1"),
            ("B2", "B1"),
        ):
            d.link(ids[source], REL, ids[target])

    env.write(build, reason="build the graph")
    return World(env, ids, a, b)


@pytest.fixture
def world(env: StoreEnv) -> World:
    return build_world(env)


def refs(found: Any, section: str) -> list[str]:
    [one] = [s for s in found["sections"] if s["id"] == section]
    return [i["ref"] for i in one.get("items", [])]


def group_refs(found: Any, section: str) -> list[list[str]]:
    """The refs of every neighbour list of every row of a section."""
    [one] = [s for s in found["sections"] if s["id"] == section]
    return [
        [i["ref"] for g in row.get("relations", []) for i in g["items"]] for row in one["items"]
    ]


def test_a_listing_holds_no_hidden_entity_and_no_edge_to_one(world: World) -> None:
    seen = world.everything(world.view())
    rows = refs(seen, "rows")
    assert len(rows) == 3 and not {str(world.ids[n]) for n in ("B1", "B2")} & set(rows)
    assert "BB-0001" not in str(seen)
    a1 = next(r for r in seen["sections"][0]["items"] if r["ref"] == "AA-0001")
    [group] = a1["relations"]
    assert group["count"] == 1 and [i["ref"] for i in group["items"]] == [str(world.ids["A2"])]
    a3 = next(r for r in seen["sections"][0]["items"] if r["ref"] == str(world.ids["A3"]))
    [group] = a3["relations"]
    assert group["count"] == 1  # A1; B2 is hidden: not an item, not in the count
    raw = world.everything(VisibleSnapshot(world.raw(), ReadView(frozenset({world.a, world.b}))))
    assert len(refs(raw, "rows")) == 3  # the control: the same view over everything sees B1


def test_neighbours_and_counts_leave_out_hidden_sources_and_targets(world: World) -> None:
    seen = world.around(world.view(), "A1")
    assert refs(seen, "sources") == [str(world.ids["A3"])]  # B1 points at A1 and is hidden
    [sources] = [s for s in seen["sections"] if s["id"] == "sources"]
    assert sources["count"] == 1
    assert refs(seen, "targets") == [str(world.ids["A2"])]
    [targets] = [s for s in seen["sections"] if s["id"] == "targets"]
    assert targets["count"] == 1
    text = str(seen)
    assert "BB-0001" not in text and str(world.ids["B1"]) not in text
    open_view = VisibleSnapshot(world.raw(), ReadView(frozenset({world.a, world.b})))
    wide = world.around(open_view, "A1", model=world.model)
    assert len(refs(wide, "sources")) == 2 and len(refs(wide, "targets")) == 2  # the control


def test_the_counts_do_not_grow_when_the_load_budget_is_spent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing left to load, an in-edge count is the raw number of the snapshot the view
    hands out: it never includes a hidden source, and it is marked approximate."""
    monkeypatch.setattr(card, "ENTITY_LOADS", 0)
    seen = world.around(world.view(), "A1")
    [sources] = [s for s in seen["sections"] if s["id"] == "sources"]
    assert sources["count"] == 1 and sources.get("approx") is True
    [targets] = [s for s in seen["sections"] if s["id"] == "targets"]
    assert targets["count"] <= 2  # the visible edge, plus at most the unread ones
    assert "BB-0001" not in str(seen) and str(world.ids["B1"]) not in str(seen)
    assert "items" not in sources  # nothing was read, nothing is named


# ---- F1b: an entity hidden by the class it was stored under -----------------------------------


class RetypeNames:
    """The name mapping of a major release that retyped ``source`` into ``target``: revisions of
    ``source`` written before ``seq`` read as ``target``; their own class is the stored one."""

    def __init__(self, source: str, target: str, seq: int) -> None:
        self.source, self.target, self.seq = source, target, seq

    def map(self, revision: Revision) -> Revision:
        if revision.type != self.source or revision.tx_seq >= self.seq:
            return revision
        moved = replace(revision, type=TermRef(self.target))
        return replace(moved, content_hash=revision_content_hash(moved))

    def own_class(self, revision: Revision) -> str:
        return revision.type

    def unmapped(self, revision: Revision) -> bool:
        return False


def before_the_retype(world: World) -> tuple[VisibleSnapshot, int]:
    """A migration moves B2 to the visible class; the view of the state before it reads B2 as
    that class (the mapping), though it was stored under the hidden one."""
    world.env.migrate(lambda d: d.revise(world.ids["B2"], type=TermRef(world.a)))
    seq = world.raw().seq
    names = RetypeNames(world.b, world.a, seq)
    shown = frozenset({world.a})
    mapping = MappedSnapshot(
        world.raw(seq - 1),
        names,  # type: ignore[arg-type]
        current=True,
        classes=(world.a, world.b),
        shown=shown.__contains__,
    )
    return VisibleSnapshot(mapping, ReadView(shown), mapping.unmapped, mapping.own_class), seq


def test_an_entity_stored_under_a_hidden_class_is_in_no_row_no_item_no_count_and_no_id(
    world: World,
) -> None:
    view, _seq = before_the_retype(world)
    b2 = world.ids["B2"]
    assert view.get_many([b2]) == {} and not view.sees(b2)  # the premise
    kept = view.query(Query())  # the rows of a listing come from here
    a3 = next(r for r in kept.items if r.id == world.ids["A3"])
    assert all(e.target != b2 for e in a3.edges)  # the snapshot strips the edge ...
    listing = world.everything(view)
    rows = refs(listing, "rows")
    assert str(b2) not in rows and len(rows) == 3
    assert str(b2) not in str(listing)
    a3_row = next(r for r in listing["sections"][0]["items"] if r["ref"] == str(world.ids["A3"]))
    [group] = a3_row["relations"]
    assert [i["ref"] for i in group["items"]] == ["AA-0001"] and group["count"] == 1
    around = world.around(view, "A3")
    assert refs(around, "targets") == ["AA-0001"]
    [targets] = [s for s in around["sections"] if s["id"] == "targets"]
    assert targets["count"] == 1 and str(b2) not in str(around)
    assert group_refs(listing, "rows")  # ... and the row builder would drop it anyway
