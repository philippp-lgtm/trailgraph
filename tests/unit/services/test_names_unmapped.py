"""The signal for revisions the name map cannot vouch for (field visibility plan R3, step 3):
``NameMaps`` records which major switches could not be read, ``GraphNames`` and
``MappedSnapshot`` tell which revisions were handed out behind one."""

from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

from support.snapshots import eid, revision

from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import OntologyPin, Revision
from graph_cli.services.names import GraphNames, MappedSnapshot, NameMaps
from graph_cli.temporal_knowledge.name_map import Hop, NameMap

ENTRY = GraphEntry(
    id=GraphId(uuid4()),
    name="example_graph",
    state="active",
    pin=OntologyPin("inventory", "3.0.0", "sha256:3"),
    alias_prefixes={},
    extensions=(),
)


def maps(readable: dict[str, bool]) -> NameMaps:
    """A ``NameMaps`` over the chain 1.0.0 -> 2.0.0 -> 3.0.0 whose hop from each old version
    reads as ``readable`` says; no hop maps anything."""
    made = NameMaps(None, None, None)  # type: ignore[arg-type]

    def chain(entry: GraphEntry) -> tuple[list[str], bool]:
        return ["1.0.0", "2.0.0", "3.0.0"], True

    def hop(entry: GraphEntry, old: str, new: str) -> tuple[Hop | None, bool]:
        return None, readable[old]

    made._chain = chain  # type: ignore[method-assign]
    made._hop = hop  # type: ignore[method-assign]
    return made


def test_an_unreadable_hop_is_recorded_and_makes_the_map_incomplete() -> None:
    found, complete = maps({"1.0.0": False, "2.0.0": True}).build(ENTRY)
    assert found.unreadable == {0} and not complete
    found, complete = maps({"1.0.0": True, "2.0.0": True}).build(ENTRY)
    assert found.unreadable == frozenset() and complete


class History:
    """Two revisions of one entity: rev 1 written under ``old``, rev 2 current."""

    def __init__(self, old: str) -> None:
        base = revision("hammer", "ex:Item", {"name": "Hammer"})
        self.revisions = {
            1: replace(base, ontology_version=old),
            2: replace(base, rev=2, ontology_version="3.0.0"),
        }

    def get(self, entity: EntityId) -> Revision | None:
        return self.revisions[2] if entity == eid("hammer") else None

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        return self.revisions.get(rev) if entity == eid("hammer") else None

    def get_many(self, entities: Sequence[EntityId]) -> dict[EntityId, Revision]:
        return {e: r for e in entities if (r := self.get(e)) is not None}


def test_the_mapped_snapshot_tells_which_revisions_it_handed_out_unmapped() -> None:
    names = maps({"1.0.0": False, "2.0.0": True})
    for old, expected in (("1.0.0", True), ("2.0.0", False), ("3.0.0", False)):
        graph = GraphNames(names, ENTRY)
        snapshot = MappedSnapshot(History(old), graph, current=False)  # type: ignore[arg-type]
        assert not snapshot.unmapped(eid("hammer"), 1)  # nothing handed out yet
        found = snapshot.get_revision(eid("hammer"), 1)
        assert found is not None and found.attrs == {"name": "Hammer"}
        assert snapshot.unmapped(eid("hammer"), 1) is expected, old
        assert graph.unmapped(found) is expected, old
        assert snapshot.get_revision(eid("hammer"), 2) is not None
        assert not snapshot.unmapped(eid("hammer"), 2)  # a current revision is never mapped


def test_a_revision_of_the_pinned_major_never_builds_the_map() -> None:
    made = NameMaps(None, None, None)  # type: ignore[arg-type]

    def fail(entry: GraphEntry) -> tuple[NameMap, bool]:
        raise AssertionError("the map is not needed")

    made._build = fail  # type: ignore[method-assign]
    current = replace(revision("hammer", "ex:Item"), ontology_version="3.1.0")
    assert not GraphNames(made, ENTRY).unmapped(current)
