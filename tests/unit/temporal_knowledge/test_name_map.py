"""``NameMap``: old revisions under current names and the hints for old names (migration plan
M7), and the pinned versions of a graph stream (``audit.pins``)."""

from dataclasses import replace

from support.modules import single
from support.snapshots import edge, revision

from graph_cli.audit.pins import pinned_versions
from graph_cli.kernel.canonical import canonical_bytes
from graph_cli.kernel.records import Revision, revision_content_hash
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.migration import parse_migration
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.name_map import Hop, NameMap, other_major

V1 = """enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, worn: {summary: Worn.}}
classes:
  Item:
    summary: Item.
    attributes:
      name: {summary: Name., type: string, role: label}
      condition: {summary: Condition., type: Condition}
  Gadget:
    summary: Gadget.
    attributes:
      name: {summary: Name., type: string}
relations:
  link: {summary: Link., domain: Item, range: Item}
"""
V2 = """enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, used: {summary: Used.}}
classes:
  Item:
    summary: Item.
    attributes:
      label: {summary: Name., type: string, role: label, renamed_from: name}
      condition: {summary: Condition., type: Condition}
relations:
  tie: {summary: Link., domain: Item, range: Item, renamed_from: link}
"""
V3 = """enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, used: {summary: Used.}}
classes:
  Article:
    summary: Item.
    renamed_from: Item
    attributes:
      title: {summary: Name., type: string, role: label, renamed_from: label}
      condition: {summary: Condition., type: Condition}
relations:
  tie: {summary: Link., domain: Article, range: Article}
"""
MIGRATION = """format: graph_cli.migration/1
from: 1.0.0
summary: Gadgets are items.
steps:
  - retype: {from: ex:Gadget, to: ex:Item, attributes: {name: label}}
  - map_values: {type: ex:Item, attribute: condition, values: {worn: used}}
  - fill: {type: ex:Item, attribute: condition, value: new}
"""


def hop(old: str, new: str, a: CompiledModel, b: CompiledModel, migration: str = "") -> Hop:
    doc = parse_migration(migration).doc if migration else None
    return Hop(old, new, a, b, derive_renames(a, b), doc)


def chain() -> NameMap:
    two, three = single("2.0.0", V2), single("3.0.0", V3)
    one_one = single("1.1.0", V1)
    return NameMap(
        ["1.0.0", "1.1.0", "2.0.0", "3.0.0"],
        {1: hop("1.1.0", "2.0.0", one_one, two, MIGRATION), 2: hop("2.0.0", "3.0.0", two, three)},
    )


def written(rev: Revision, version: str) -> Revision:
    return replace(rev, ontology_version=version)


def test_a_revision_is_mapped_through_every_later_release() -> None:
    old = revision(
        "hammer", "ex:Item", {"name": "Hammer", "condition": "worn"}, [edge("ex:link", "saw")]
    )
    mapped = chain().map(old)
    assert mapped.type == "ex:Article"
    assert mapped.attrs == {"title": "Hammer", "condition": "used"}  # fill is not mapped
    assert [str(e.rel) for e in mapped.edges] == ["ex:tie"]
    assert mapped.edges[0].edge_id == old.edges[0].edge_id
    assert (mapped.id, mapped.rev, mapped.tx_seq, mapped.ontology_version) == (
        old.id,
        old.rev,
        old.tx_seq,
        "1.0.0",
    )
    assert mapped.content_hash == revision_content_hash(mapped) != old.content_hash


def test_a_retyped_class_follows_later_renames() -> None:
    gadget = revision("drill", "ex:Gadget", {"name": "Drill"})
    mapped = chain().map(written(gadget, "1.1.0"))
    assert (mapped.type, mapped.attrs) == ("ex:Article", {"title": "Drill"})


def test_what_needs_no_mapping_is_the_same_object() -> None:
    names = chain()
    current = written(revision("a", "ex:Article", {"title": "A"}), "3.0.0")
    assert names.map(current) is current
    unknown = written(revision("b", "ex:Item", {"name": "B"}), "0.9.0")
    assert names.map(unknown) is unknown
    plain = written(revision("c", "ex:Item", {"condition": "new"}), "2.0.0")
    renamed = names.map(plain)
    assert renamed.attrs == {"condition": "new"} and renamed.type == "ex:Article"
    assert names.changes("1.0.0") and names.changes("2.0.0") and not names.changes("3.0.0")
    assert not names.changes("0.9.0")


def test_hints_name_the_current_term_and_every_step() -> None:
    names = chain()
    [item] = names.hints("class", "ex:Item")
    assert item.text() == "ex:Item was renamed to ex:Article in 3.0.0"
    [gadget] = names.hints("class", "Gadget")  # a local name finds the old CURIE
    assert gadget.text() == "ex:Gadget was retyped to ex:Item in 2.0.0, then to ex:Article in 3.0.0"
    assert gadget.current == "ex:Article"
    [link] = names.hints("relation", "link")
    assert link.to_json() == {
        "from": "ex:link",
        "to": "ex:tie",
        "steps": [{"to": "ex:tie", "in": "2.0.0"}],
    }
    found = names.hints("attribute", "name")
    texts = sorted(h.text() for h in found)
    assert texts == [
        "name was renamed to label (on ex:Item) in 2.0.0, then to title (on ex:Article) in 3.0.0",
    ]
    assert names.hints("class", "ex:Nothing") == ()
    assert names.hints("class", "ex:Item", known=lambda hint: False) == ()


def test_a_major_without_renames_needs_no_hop() -> None:
    one, two = single("1.0.0", V1), single("2.0.0", V1)
    names = NameMap(["1.0.0", "2.0.0"], {0: hop("1.0.0", "2.0.0", one, two)})
    old = revision("x", "ex:Item", {"name": "X"})
    assert names.map(old) is old and not names.changes("1.0.0")
    assert names.hop("1.0.0", "2.0.0") is not None and names.hop("2.0.0", "3.0.0") is None
    assert not other_major("2.1.0", "2.0.0") and other_major("1.9.0", "2.0.0")


def test_the_migrating_transform_says_what_applied() -> None:
    one, two = single("1.0.0", V1), single("2.0.0", V2)
    step = hop("1.0.0", "2.0.0", one, two, MIGRATION)
    before = revision("hammer", "ex:Item", {"name": "Hammer"})
    applied = step.migrating.applied(before)
    assert applied == [
        {"kind": "rename_attribute", "class": "ex:Item", "from": "name", "to": "label"},
        {
            "kind": "fill",
            "type": "ex:Item",
            "attribute": "condition",
            "value": "new",
            "index": 2,
        },
    ]
    assert step.mapping.applied(before) == applied[:1]  # fill is no read-time mapping


def test_an_extended_map_adds_a_release_being_made() -> None:
    one, two = single("1.0.0", V1), single("2.0.0", V2)
    names = NameMap(["1.0.0"], {}).extended(hop("1.0.0", "2.0.0", one, two))
    assert names.chain == ("1.0.0", "2.0.0")
    [hint] = names.hints("relation", "ex:link")
    assert hint.current == "ex:tie"


def _line(event: dict[str, object]) -> bytes:
    return canonical_bytes(event) + b"\n"


def test_pinned_versions_follow_the_ok_pin_intents() -> None:
    def pin(old: str, new: str, status: str = "ok") -> bytes:
        return _line(
            {
                "actions": ["maint.ontology_pin"],
                "result": {"status": status},
                "change": {"pin": {"from": {"version": old}, "to": {"version": new}}},
            }
        )

    lines = [
        _line(
            {
                "actions": ["maint.graph_create"],
                "result": {"status": "ok"},
                "graph": {"ontology": {"version": "1.0.0"}},
            }
        ),
        _line({"actions": ["write.create"], "result": {"status": "ok"}}),
        pin("1.0.0", "1.1.0"),
        pin("1.1.0", "2.0.0", status="denied"),
        b"{not json maint.ontology_pin\n",
        pin("1.1.0", "2.0.0"),
        pin("2.0.0", "2.0.0"),
    ]
    assert pinned_versions(lines) == ["1.0.0", "1.1.0", "2.0.0"]
