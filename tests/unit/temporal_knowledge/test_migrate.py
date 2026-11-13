"""``plan_migration``: derived renames and every step over current revisions of all statuses
(migration plan M2-M4, step 2)."""

import random
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import cache
from typing import Any
from uuid import UUID

import pytest
from support.modules import compile_modules, header
from support.snapshots import AT, DictSnapshot, edge, eid, revision

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import TxId
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import (
    RECORD_STATUSES,
    EdgeRecord,
    RecordStatus,
    Revision,
    revision_content_hash,
)
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import Renames, derive_renames
from graph_cli.ontology_layer.migration import MigrationDoc, check_migration, parse_migration
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.migrate import (
    MANY_TO_SINGLE,
    MigrationPlan,
    MigrationStamp,
    plan_migration,
)

ALL = Query(record_status=frozenset(RECORD_STATUSES), limit=sys.maxsize)
HASH = "sha256:" + "ab" * 32
OTHER_HASH = "sha256:" + "cd" * 32
STAMP = MigrationStamp(
    tx_id=TxId(UUID("0192f000-0000-7000-8000-0000000000bb")),
    tx_seq=40,
    recorded_at=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
    version="2.0.0",
)

OLD = """enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, worn: {summary: Worn.}, broken: {summary: Broken.}}
  Kind:
    summary: Kind.
    values: {light: {summary: Light.}, heavy: {summary: Heavy.}}
lifecycles:
  Flow:
    summary: Flow.
    states:
      open: {summary: Open., initial: true}
      held: {summary: Held.}
      closed: {summary: Closed., terminal: true}
    transitions:
      - {from: open, to: held}
      - {from: held, to: closed}
classes:
  Base:
    summary: Root of the hierarchy.
    attributes:
      name: {summary: Name., type: string, role: label}
  Item:
    summary: Item.
    is_a: Base
    attributes:
      note: {summary: Note., type: string}
      body: {summary: Body., type: text}
      tags: {summary: Tags., type: string, many: true}
      owner: {summary: Owner., type: string}
      condition: {summary: Condition., type: Condition}
      past: {summary: Past conditions., type: Condition, many: true}
      state: {summary: State., lifecycle: Flow}
      legacy: {summary: Legacy., type: string}
  SubItem:
    summary: Specialized item.
    is_a: Item
    attributes:
      extra: {summary: Extra., type: string}
  Gadget:
    summary: Gadget.
    attributes:
      name: {summary: Name., type: string}
  Part:
    summary: Part.
  Scan:
    summary: A stored scan.
    is_a: files:File
relations:
  link:
    summary: Link.
    domain: Item
    range: Item
    qualifiers:
      kind: {summary: Kind., type: Kind}
      remark: {summary: Remark., type: string}
      weight: {summary: Weight., type: string}
      grades: {summary: Grades., type: Kind, many: true}
  stale: {summary: Stale., domain: Item, range: Item}
  uses: {summary: Uses., domain: Item, range: Part}
"""

NEW = """enums:
  Condition:
    summary: Condition.
    values: {new: {summary: New.}, used: {summary: Used.}, broken: {summary: Broken.}}
  Kind:
    summary: Kind.
    values: {light: {summary: Light.}, heavy: {summary: Heavy.}, medium: {summary: Medium.}}
datatypes:
  Rank: {summary: A rank., base: integer, min: 1, max: 5}
lifecycles:
  Flow:
    summary: Flow.
    states:
      open: {summary: Open., initial: true}
      paused: {summary: Paused.}
      closed: {summary: Closed., terminal: true}
    transitions:
      - {from: open, to: paused}
      - {from: paused, to: closed}
classes:
  Root:
    summary: Root of the hierarchy.
    renamed_from: Base
    attributes:
      label: {summary: Name., type: string, role: label, renamed_from: name}
  Item:
    summary: Item.
    is_a: Root
    attributes:
      note: {summary: Note., type: lang_string}
      body: {summary: Body., type: lang_text}
      tags: {summary: Tags., type: string}
      custodian: {summary: Custodian., type: string, many: true, renamed_from: owner}
      condition: {summary: Condition., type: Condition}
      past: {summary: Past conditions., type: Condition, many: true}
      state: {summary: State., lifecycle: Flow}
      priority: {summary: Priority., type: Rank}
  SubItem:
    summary: Specialized item.
    is_a: Item
    attributes:
      extra: {summary: Extra., type: string}
relations:
  link:
    summary: Link.
    domain: Item
    range: Item
    qualifiers:
      kind: {summary: Kind., type: Kind}
      note: {summary: Note., type: string}
      grades: {summary: Grades., type: Kind, many: true}
"""

AREA = (
    header("area_a", "aa", "1.0.0", ("files",))
    + """classes:
  Part:
    summary: Part.
    renamed_from: ex:Part
  Scan:
    summary: A stored scan.
    is_a: files:File
    renamed_from: ex:Scan
relations:
  uses:
    summary: Uses.
    range: Part
    renamed_from: "ex:uses"
"""
)

STEPS = """steps:
  - retype: {from: ex:Gadget, to: ex:Item, attributes: {name: label}}
  - fill: {type: ex:Item, attribute: priority, value: "1"}
  - map_values: {type: ex:Item, attribute: condition, values: {worn: used}}
  - map_values: {type: ex:Item, attribute: past, values: {worn: used}}
  - map_values: {type: ex:Item, attribute: state, values: {held: paused}}
  - map_qualifier_values: {relation: ex:link, qualifier: kind, values: {heavy: medium}}
  - map_qualifier_values: {relation: ex:link, qualifier: grades, values: {heavy: medium}}
  - drop: {type: ex:Item, attribute: legacy}
  - drop_edges: {relation: ex:stale}
  - rename_qualifier: {relation: ex:link, from: remark, to: note}
  - drop_qualifier: {relation: ex:link, qualifier: weight}
  - convert: {type: ex:Item, attribute: note, to: lang_string}
  - convert: {type: ex:Item, attribute: body, to: lang_text}
  - convert: {type: ex:Item, attribute: custodian, to: many}
  - convert: {type: ex:Item, attribute: tags, to: single}
"""


def preface(text: str = STEPS) -> str:
    return "format: graph_cli.migration/1\nfrom: 1.0.0\nsummary: Test migration.\n" + text


@cache
def old_model() -> CompiledModel:
    main = header("example", "ex", "1.0.0", ("files",)) + OLD
    return compile_modules("example", {"example": main})


@cache
def new_model() -> CompiledModel:
    main = header("example", "ex", "2.0.0", ("area_a", "files")) + NEW
    return compile_modules("example", {"example": main, "area_a": AREA})


def renames() -> Renames:
    return derive_renames(old_model(), new_model())


def doc(text: str = STEPS) -> MigrationDoc:
    parsed = parse_migration(preface(text))
    assert parsed.doc is not None, [p.to_json() for p in parsed.problems]
    problems = check_migration(parsed.doc, old_model(), new_model(), renames(), language="en")
    assert problems == (), [p.to_json() for p in problems]
    return parsed.doc


def plan(
    snapshot: DictSnapshot, text: str | None = STEPS, *, language: str | None = "en"
) -> MigrationPlan:
    return plan_migration(
        old_model(),
        new_model(),
        renames(),
        None if text is None else doc(text),
        snapshot,
        stamp=STAMP,
        language=language,
    )


def lang(text: str, tag: str = "en") -> dict[str, JsonValue]:
    return {"lang": tag, "value": text}


def one(
    name: str,
    type: str,
    attrs: Mapping[str, JsonValue] | None = None,
    edges: Sequence[EdgeRecord] = (),
    **kwargs: Any,
) -> Revision:
    return revision(name, type, attrs, edges, **kwargs)


def migrated(
    snapshot: DictSnapshot, text: str | None = STEPS, **kwargs: Any
) -> dict[str, Revision]:
    """The migrated revisions by entity name (the names the test gave ``eid``)."""
    result = plan(snapshot, text, **kwargs).revisions
    names = {eid(n): n for n in NAMES}
    return {names[e]: r for e, r in result.items()}


NAMES = [f"e{i}" for i in range(40)]


# ---- renames --------------------------------------------------------------------------------


def test_the_class_is_renamed_and_its_identity_kept() -> None:
    part = one("e1", "ex:Part", alias="PART-0001", status="superseded")
    result = migrated(DictSnapshot(part), None)["e1"]
    assert (result.type, result.rev, result.prev_rev) == ("aa:Part", 2, 1)
    assert (result.id, result.alias, result.record_status) == (part.id, "PART-0001", "superseded")
    assert (result.created_at, result.valid_from, result.valid_to) == (AT, None, None)
    assert (result.ontology_version, result.tx_seq, result.tx_id) == ("2.0.0", 40, STAMP.tx_id)
    assert result.recorded_at == STAMP.recorded_at
    assert result.content_hash == revision_content_hash(result) != part.content_hash


def test_an_attribute_inherited_through_a_renamed_superclass_is_renamed() -> None:
    """``name`` is declared on ``Base`` (now ``Root``, attribute ``label``): every subclass
    entity stores it under the new name, and its class is untouched."""
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"name": "Hammer"}),
        one("e2", "ex:SubItem", {"name": "Saw", "extra": "x"}),
    )
    result = migrated(snapshot, None)
    # fill is absent without a step: the items only change by the rename
    assert result["e1"].attrs == {"label": "Hammer"}
    assert result["e2"].attrs == {"label": "Saw", "extra": "x"}
    assert (result["e1"].type, result["e2"].type) == ("ex:Item", "ex:SubItem")


def test_a_relation_is_renamed_on_every_edge_and_keeps_its_identity() -> None:
    edges = [
        edge("ex:uses", "e9", name="a", note="x"),
        edge("ex:link", "e2", name="b"),
        edge("ex:uses", "e8", name="c"),
    ]
    snapshot = DictSnapshot(one("e1", "ex:Item", edges=edges), one("e2", "ex:Item"))
    result = migrated(snapshot, None)["e1"]
    assert [(e.rel, e.edge_id, e.target, dict(e.qualifiers)) for e in result.edges] == [
        ("aa:uses", edges[0].edge_id, eid("e9"), {"note": "x"}),
        ("ex:link", edges[1].edge_id, eid("e2"), {}),
        ("aa:uses", edges[2].edge_id, eid("e8"), {}),
    ]


def test_an_entity_with_nothing_to_change_gets_no_revision() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:SubItem", {"extra": "x"}),
        one("e2", "ex:Item", {"label": "Already new"}),
        one("e3", "ex:Part"),
    )
    result = plan(snapshot, None)
    assert [str(e) for e in result.revisions] == [str(eid("e3"))]
    assert result.examined == 3
    assert result.to_json()["unchanged"] == 2


# ---- the steps, single values -----------------------------------------------------------------


def test_retype_moves_exactly_the_class_and_maps_attributes() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Gadget", {"name": "Widget"}),
        one("e2", "ex:Item", {"name": "Plain"}),
    )
    result = migrated(snapshot)
    gadget = result["e1"]
    assert gadget.type == "ex:Item"
    assert gadget.attrs == {"label": "Widget", "priority": 1}  # the map, then fill
    assert result["e2"].type == "ex:Item"


def test_a_retyped_entitys_unrelated_attribute_of_the_same_name_is_not_renamed() -> None:
    """``Gadget.name`` is not ``Base.name``: the old model says which class declares a stored
    attribute, so only the derived rename of ``Base.name`` reaches ``Item`` entities."""
    steps = "steps:\n  - retype: {from: ex:Gadget, to: ex:Item}\n"
    snapshot = DictSnapshot(
        one("e1", "ex:Gadget", {"name": "Widget"}), one("e2", "ex:Item", {"name": "Plain"})
    )
    result = migrated(snapshot, steps)
    assert result["e1"].attrs == {"name": "Widget"}  # left for check_graph to judge
    assert result["e2"].attrs == {"label": "Plain"}


def test_retype_does_not_touch_subclasses_of_its_source() -> None:
    steps = "steps:\n  - retype: {from: ex:Item, to: ex:SubItem}\n"
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A"}), one("e2", "ex:SubItem", {"label": "B"})
    )
    result = migrated(snapshot, steps)
    assert set(result) == {"e1"}
    assert result["e1"].type == "ex:SubItem"


def test_fill_sets_a_constant_where_absent_on_the_class_and_its_subclasses() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A"}),
        one("e2", "ex:SubItem", {"label": "B", "priority": 4}),
        one("e3", "ex:SubItem", {"label": "C"}),
    )
    result = migrated(
        snapshot, "steps:\n  - fill: {type: ex:Item, attribute: priority, value: '3'}\n"
    )
    assert result["e1"].attrs["priority"] == 3  # stored as the integer a write would store
    assert result["e3"].attrs["priority"] == 3
    assert "e2" not in result  # present: kept, no revision


def test_fill_of_a_language_text_is_tagged_with_the_graph_language() -> None:
    steps = "steps:\n  - fill: {type: ex:Item, attribute: note, value: Hello}\n"
    snapshot = DictSnapshot(one("e1", "ex:Item", {"label": "A"}))
    assert migrated(snapshot, steps, language="de")["e1"].attrs["note"] == lang("Hello", "de")
    tagged = "steps:\n  - fill: {type: ex:Item, attribute: note, value: '\"Hallo\"@de'}\n"
    assert migrated(snapshot, tagged, language=None)["e1"].attrs["note"] == lang("Hallo", "de")


def test_fill_of_a_many_attribute_stores_a_list_and_of_a_state_the_name() -> None:
    steps = (
        "steps:\n"
        "  - fill: {type: ex:Item, attribute: custodian, value: nobody}\n"
        "  - fill: {type: ex:Item, attribute: state, value: open}\n"
    )
    snapshot = DictSnapshot(one("e1", "ex:Item", {"label": "A"}))
    attrs = migrated(snapshot, steps)["e1"].attrs
    assert (attrs["custodian"], attrs["state"]) == (["nobody"], "open")


def test_map_values_rewrites_enum_values_and_states() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A", "condition": "worn", "state": "held"}),
        one("e2", "ex:SubItem", {"label": "B", "condition": "broken", "state": "open"}),
    )
    result = migrated(snapshot)
    assert (result["e1"].attrs["condition"], result["e1"].attrs["state"]) == ("used", "paused")
    assert (result["e2"].attrs["condition"], result["e2"].attrs["state"]) == ("broken", "open")


def test_map_qualifier_values_rename_and_drop_qualifier_change_the_edges_of_their_relation() -> (
    None
):
    edges = [
        edge("ex:link", "e2", name="a", kind="heavy", remark="r", weight="9"),
        edge("ex:link", "e3", name="b", kind="light"),
        edge("ex:see", "e2", name="c", kind="heavy", remark="r", weight="9"),
    ]
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A"}, edges),
        one("e2", "ex:Item", {"label": "B"}),
        one("e3", "ex:Item", {"label": "C"}),
    )
    result = migrated(snapshot)["e1"]
    assert [(e.edge_id, dict(e.qualifiers)) for e in result.edges] == [
        (edges[0].edge_id, {"kind": "medium", "note": "r"}),
        (edges[1].edge_id, {"kind": "light"}),
        (edges[2].edge_id, {"kind": "heavy", "remark": "r", "weight": "9"}),  # another relation
    ]
    assert [(e.rel, e.target) for e in result.edges] == [
        ("ex:link", eid("e2")),
        ("ex:link", eid("e3")),
        ("ex:see", eid("e2")),
    ]


def test_drop_removes_the_stored_value_of_the_old_name() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A", "legacy": "x"}),
        one("e2", "ex:Part"),
    )
    result = migrated(snapshot, "steps:\n  - drop: {type: ex:Item, attribute: legacy}\n")
    assert result["e1"].attrs == {"label": "A"}


def test_drop_edges_removes_every_edge_of_the_relation_at_the_source() -> None:
    edges = [edge("ex:stale", "e2", name="a"), edge("ex:link", "e2", name="b")]
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A"}, edges), one("e2", "ex:Item", {"label": "B"})
    )
    result = plan(snapshot, "steps:\n  - drop_edges: {relation: ex:stale}\n")
    assert [e.rel for e in result.revisions[eid("e1")].edges] == ["ex:link"]
    assert eid("e2") not in result.revisions  # the target is not rewritten
    overlay = result.overlay(snapshot)
    assert [(s, e.rel) for s, e in overlay.incoming(eid("e2"))] == [(eid("e1"), "ex:link")]
    assert len(snapshot.incoming(eid("e2"))) == 2  # the base still has both


def test_convert_to_a_language_type_tags_with_the_graph_language() -> None:
    steps = (
        "steps:\n"
        "  - convert: {type: ex:Item, attribute: note, to: lang_string}\n"
        "  - convert: {type: ex:Item, attribute: body, to: lang_text}\n"
    )
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A", "note": "plain", "body": "long"}),
        one("e2", "ex:Item", {"label": "B", "note": lang("Hallo", "de")}),
    )
    result = migrated(snapshot, steps)
    assert result["e1"].attrs["note"] == lang("plain")
    assert result["e1"].attrs["body"] == lang("long")
    assert "e2" not in result  # already tagged: nothing to do


def test_convert_to_many_wraps_and_to_single_unwraps() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A", "owner": "ann", "tags": ["x"]}),
        one("e2", "ex:Item", {"label": "B", "owner": "bob", "tags": []}),
        one("e3", "ex:Item", {"label": "C", "tags": "plain"}),
    )
    result = migrated(snapshot)
    assert result["e1"].attrs["custodian"] == ["ann"]
    assert result["e1"].attrs["tags"] == "x"
    assert "tags" not in result["e2"].attrs  # an empty list is no value
    assert result["e3"].attrs["tags"] == "plain"


# ---- the steps, many values ---------------------------------------------------------------------


def test_every_value_step_applies_to_each_item_of_a_many_attribute() -> None:
    edges = [edge("ex:link", "e2", name="a", grades=["heavy", "light", "heavy"])]
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A", "past": ["worn", "new", "worn"]}, edges),
        one("e2", "ex:Item", {"label": "B"}),
    )
    result = migrated(snapshot)["e1"]
    assert result.attrs["past"] == ["used", "new", "used"]
    assert dict(result.edges[0].qualifiers) == {"grades": ["medium", "light", "medium"]}


def test_many_to_single_with_more_than_one_value_is_a_finding_and_keeps_the_value() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"label": "A", "tags": ["x", "y"]}),
        one("e2", "ex:Item", {"label": "B", "tags": ["z"]}),
    )
    result = plan(snapshot)
    [finding] = result.findings
    assert (finding.code, finding.severity) == (MANY_TO_SINGLE, Severity.ERROR)
    assert finding.target == {"id": str(eid("e1")), "type": "ex:Item", "path": "attrs.tags"}
    assert finding.actual == 2
    assert result.revisions[eid("e1")].attrs["tags"] == ["x", "y"]  # left for check_graph
    assert result.revisions[eid("e2")].attrs["tags"] == "z"
    judged = check_graph(new_model(), result.overlay(snapshot))
    assert [(f.target["id"], f.severity) for f in judged] == [(str(eid("e1")), Severity.ERROR)]


def test_the_finding_code_is_registered() -> None:
    from graph_cli.catalog.registries import all_finding_codes

    spec = all_finding_codes()[MANY_TO_SINGLE]
    assert (spec.scope, spec.layer, spec.default_severity) == ("data", "migration", Severity.ERROR)


# ---- statuses -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "superseded", "retracted"])
def test_entities_of_every_status_migrate_with_their_status_kept(status: RecordStatus) -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"name": "A", "condition": "worn"}, status=status, rev=3)
    )
    result = migrated(snapshot)["e1"]
    assert result.record_status == status
    assert (result.rev, result.prev_rev) == (4, 3)
    assert result.attrs["condition"] == "used"
    assert result.attrs["label"] == "A"


# ---- effects: counts, samples, unused steps ------------------------------------------------------


def test_effects_count_entities_edges_and_types_and_sample_the_smallest_ids() -> None:
    snapshot = DictSnapshot(
        *[
            one(
                f"e{i}",
                "ex:Item" if i % 2 else "ex:SubItem",
                {"name": f"n{i}", "condition": "worn"},
                [edge("ex:stale", "e30", name=f"s{i}"), edge("ex:uses", "e31", name=f"u{i}")],
            )
            for i in range(10)
        ],
        one("e30", "ex:Item", {"label": "T"}),
    )
    result = plan(snapshot)
    by_kind = {(e.kind, e.index): e for e in result.steps}
    drop_edges = by_kind[("drop_edges", 8)]
    assert (drop_edges.entities, drop_edges.edges) == (10, 10)
    assert dict(drop_edges.by_type) == {"ex:Item": 5, "ex:SubItem": 5}
    assert drop_edges.samples == tuple(sorted(eid(f"e{i}") for i in range(10)))[:3]
    mapped = by_kind[("map_values", 2)]
    assert (mapped.entities, mapped.edges) == (10, 0)
    relation = next(e for e in result.renames if e.kind == "rename_relation")
    assert (relation.what, relation.entities, relation.edges) == (
        {"from": "ex:uses", "to": "aa:uses"},
        10,
        10,
    )
    attribute = next(e for e in result.renames if e.what.get("from") == "name")
    assert dict(attribute.what) == {"class": "ex:Root", "from": "name", "to": "label"}
    assert attribute.entities == 10
    cls = next(e for e in result.renames if e.kind == "rename_class" and "Part" in str(e.what))
    assert cls.entities == 0


def test_a_step_that_changes_nothing_is_unused() -> None:
    snapshot = DictSnapshot(one("e1", "ex:Item", {"label": "A", "condition": "new"}))
    result = plan(snapshot)
    # only the fill changes the item; steps that find no value or edge to change are unused
    assert result.unused_steps == (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
    assert result.steps[1].entities == 1


def test_the_preview_serializes() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"name": "A", "condition": "worn", "tags": ["x", "y"]}),
        one("e2", "ex:Part"),
    )
    preview: Any = plan(snapshot).to_json()
    assert preview["entities"] == 2 and preview["unchanged"] == 0
    assert preview["by_type"] == {"aa:Part": 1, "ex:Item": 1}
    assert preview["findings"]["count"] == 1
    assert preview["steps"][2] == {
        "kind": "map_values",
        "type": "ex:Item",
        "attribute": "condition",
        "values": {"worn": "used"},
        "index": 2,
        "entities": 1,
        "edges": 0,
        "by_type": {"ex:Item": 1},
        "samples": [str(eid("e1"))],
    }


# ---- blobs ------------------------------------------------------------------------------------


def test_the_blobs_of_a_moved_file_subclass_are_listed() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Scan", {"title": "A", "content": HASH, "media_type": "application/pdf"}),
        one("e2", "ex:Scan", {"title": "B", "content": OTHER_HASH, "media_type": "text/plain"}),
        one("e3", "ex:Part"),
    )
    result = plan(snapshot, None)
    assert {r.type for r in result.revisions.values()} == {"aa:Scan", "aa:Part"}
    assert result.blobs(new_model()) == (HASH, OTHER_HASH)
    assert plan(DictSnapshot(one("e3", "ex:Part")), None).blobs(new_model()) == ()


# ---- the overlay -------------------------------------------------------------------------------


def test_the_overlay_answers_as_the_migrated_graph() -> None:
    snapshot = DictSnapshot(
        one("e1", "ex:Item", {"name": "A"}), one("e2", "ex:Part", alias="PART-0001")
    )
    overlay = plan(snapshot, None).overlay(snapshot)
    part = overlay.get(eid("e2"))
    assert part is not None and part.type == "aa:Part"
    assert [r.type for r in overlay.current_revisions()] == ["ex:Item", "aa:Part"]
    assert overlay.unchanged == {} and overlay.created == frozenset()
    assert overlay.touched == {eid("e1"), eid("e2")}


# ---- a plan applied is a plan done; identity is kept ----------------------------------

TYPES = ("ex:Item", "ex:SubItem", "ex:Gadget", "ex:Part", "ex:Scan")


def random_graph(seed: int) -> DictSnapshot:
    rng = random.Random(seed)
    count = rng.randint(8, 30)
    revisions = []
    for i in range(count):
        type_ = rng.choice(TYPES)
        attrs: dict[str, JsonValue] = {}
        if type_ in ("ex:Item", "ex:SubItem"):
            attrs = {"name": f"n{i}"}
            if rng.random() < 0.5:
                attrs["condition"] = rng.choice(["new", "worn", "broken"])
            if rng.random() < 0.5:
                attrs["past"] = [rng.choice(["new", "worn"]) for _ in range(rng.randint(1, 3))]
            if rng.random() < 0.4:
                attrs["note"] = f"note {i}"
            if rng.random() < 0.4:
                attrs["owner"] = f"o{i}"
            if rng.random() < 0.4:
                attrs["tags"] = [f"t{i}"] * rng.randint(0, 1)
            if rng.random() < 0.3:
                attrs["legacy"] = "old"
        elif type_ == "ex:Gadget":
            attrs = {"name": f"g{i}"}
        edges = [
            edge(
                rng.choice(["ex:link", "ex:stale", "ex:uses", "ex:see"]),
                f"e{rng.randrange(count)}",
                name=f"{i}-{j}",
                **(
                    {"kind": rng.choice(["light", "heavy"]), "remark": "r"}
                    if rng.random() < 0.5
                    else {}
                ),
            )
            for j in range(rng.randint(0, 3))
        ]
        revisions.append(
            one(
                f"e{i}",
                type_,
                attrs,
                edges,
                status=rng.choice(["active", "active", "superseded", "retracted"]),
                rev=rng.randint(1, 3),
                alias=f"AL-{i:04d}" if rng.random() < 0.7 else None,
            )
        )
    return DictSnapshot(*revisions)


def current(snapshot: DictSnapshot) -> list[Revision]:
    return list(snapshot.query(ALL).items)


def apply(snapshot: DictSnapshot, result: MigrationPlan) -> DictSnapshot:
    merged = {r.id: r for r in current(snapshot)}
    merged.update(result.revisions)
    return DictSnapshot(*merged.values())


@pytest.mark.parametrize("seed", range(25))
def test_a_plan_keeps_identity_and_applying_it_leaves_nothing_to_do(seed: int) -> None:
    snapshot = random_graph(seed)
    result = plan(snapshot)
    assert not result.findings
    for entity, new in result.revisions.items():
        old = snapshot.get(entity)
        assert old is not None
        assert (new.id, new.alias, new.record_status, new.created_at) == (
            old.id,
            old.alias,
            old.record_status,
            old.created_at,
        )
        assert (new.valid_from, new.valid_to) == (old.valid_from, old.valid_to)
        assert (new.rev, new.prev_rev) == (old.rev + 1, old.rev)
        assert new.content_hash != old.content_hash == revision_content_hash(old)
        assert new.content_hash == revision_content_hash(new)
        before, after = {e.edge_id: e for e in old.edges}, {e.edge_id: e for e in new.edges}
        assert set(after) <= set(before)  # edges are only removed, never made up
        for edge_id, kept in after.items():
            assert (kept.target, kept.target_rev) == (
                before[edge_id].target,
                before[edge_id].target_rev,
            )
    assert list(result.revisions) == sorted(result.revisions)
    again = plan(apply(snapshot, result))
    assert again.revisions == {}
    assert again.unused_steps == tuple(range(15))


@pytest.mark.parametrize("seed", range(5))
def test_the_migrated_random_graph_has_no_old_names_left(seed: int) -> None:
    snapshot = random_graph(seed)
    migrated_graph = apply(snapshot, plan(snapshot))
    old_types = {"ex:Gadget", "ex:Part", "ex:Scan"}
    for r in current(migrated_graph):
        assert r.type not in old_types
        assert not {"name", "owner", "legacy"} & set(r.attrs)
        assert all(e.rel not in ("ex:stale", "ex:uses") for e in r.edges)
