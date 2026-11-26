"""The ``lookup`` tool of the manifest (tool manifest plan TM5, Q5): listed when a tool the caller
may use has a reference input and the caller holds ``read.find``, its ``for`` over exactly those
inputs, the names the descriptions of the inputs give it, and what it maps to."""

import json
import re
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from support import tools
from support.tools import ITEMS, NOTHING, PERSONAL, Scene

from graph_cli.app_cli import tool_manifest as tm
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import ENTITY_REF_PATTERN, EXACT_ENTITY_REF_PATTERN
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.lookup import MAX_LOOKUP_TEXTS
from graph_cli.services import describe_tools as dt
from graph_cli.services.tool_errors import on_error

COMPANY = tools.company()
# the release in which items have a label and locations have none; the first has no label at all
INVENTORY = tools.inventory(version="1.1.0")
UNNAMED = tools.inventory()
STAFF = tools.staff()
NO_OBLIGATIONS: frozenset[str] = frozenset()


def manifest(scene: Scene, *, find: bool = True, **selection: Any) -> dict[str, Any]:
    found = tm.build_manifest(
        scene.entry,
        scene.model,
        functions=scene.functions,
        actions=scene.actions,
        views=scene.views,
        obligations=NO_OBLIGATIONS,
        selection=tm.Selection(**selection),
        find=find,
    )
    return json.loads(json.dumps(found))


def by_name(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in document["tools"]}


def lookup_of(document: dict[str, Any]) -> dict[str, Any]:
    return by_name(document)["lookup"]


def slots(tool: dict[str, Any]) -> list[str]:
    found: list[str] = tool["input_schema"]["properties"]["for"]["enum"]
    return found


def class_of(node: Any) -> str | None:
    """The class a property schema of a reference names, however it is wrapped (a list, a
    nullable value); ``None`` for any other property."""
    if isinstance(node, list):
        return next((c for c in map(class_of, node) if c), None)
    if not isinstance(node, dict):
        return None
    extension = node.get("x-graph-cli")
    if isinstance(extension, dict) and isinstance(extension.get("class"), str):
        return str(extension["class"])
    return next((c for c in map(class_of, node.values()) if c), None)


def ref_inputs_of(document: dict[str, Any], scene: Scene) -> list[str]:
    """``<tool>.<property>`` of every reference input of the tools of the document whose class
    has a name to look up (the slots the tool should serve)."""
    model = scene.model
    return [
        f"{t['name']}.{name}"
        for t in document["tools"]
        if t["kind"] != "lookup"
        for name, schema in t["input_schema"]["properties"].items()
        if (cls := class_of(schema)) in model.classes
        and any(model.label_attributes(c) for c in model.subtypes(cls))
    ]


def all_ref_inputs_of(document: dict[str, Any]) -> list[str]:
    return [
        f"{t['name']}.{name}"
        for t in document["tools"]
        if t["kind"] != "lookup"
        for name, schema in t["input_schema"]["properties"].items()
        if class_of(schema)
    ]


# ---- when it is listed ---------------------------------------------------------------------


def test_the_lookup_tool_comes_last_and_serves_every_reference_input() -> None:
    for catalog in (COMPANY, INVENTORY, STAFF):
        document = manifest(catalog.scene())
        assert [t["name"] for t in document["tools"]][-1] == "lookup"
        assert [t["kind"] for t in document["tools"]].count("lookup") == 1
        assert slots(lookup_of(document)) == ref_inputs_of(document, catalog.scene())
        assert slots(lookup_of(document))  # the example graphs all have some
    # references of a class without a name are left out
    leaving = manifest(INVENTORY.scene())
    assert set(slots(lookup_of(leaving))) < set(all_ref_inputs_of(leaving))


def test_a_caller_without_read_find_gets_no_lookup_tool() -> None:
    document = manifest(COMPANY.scene(), find=False)
    assert "lookup" not in by_name(document)
    assert all_ref_inputs_of(document)  # the references stay: the caller has the ids already
    assert [t["kind"] for t in document["tools"]] == [
        t["kind"] for t in manifest(COMPANY.scene())["tools"] if t["kind"] != "lookup"
    ]


def test_no_reference_input_no_lookup_tool() -> None:
    plain = tuple(f for f in COMPANY.functions if all(p.type != "ref" for p in f.defn.params))
    assert plain  # functions without a reference exist
    scene = replace(COMPANY.scene(), functions=plain, actions=(), views=())
    document = manifest(scene)
    assert "lookup" not in by_name(document) and document["tools"]
    assert all_ref_inputs_of(document) == []
    assert manifest(replace(scene, functions=(), views=()))["tools"] == []  # nothing at all


def test_the_lookup_tool_is_listed_for_one_entity_view_alone() -> None:
    entity = tuple(v for v in COMPANY.views if v.name == "position_overview")
    document = manifest(replace(COMPANY.scene(), functions=(), actions=(), views=entity))
    assert slots(lookup_of(document)) == ["view_position_overview._entity"]


def test_a_tool_the_caller_may_not_use_adds_no_slot() -> None:
    scene = COMPANY.scene()
    full = slots(lookup_of(manifest(scene)))
    assert {"do_assign_position.person", "view_unit_overview._entity"} <= set(full)
    functions_only = replace(scene, actions=(), views=())  # what a caller without them is given
    only = slots(lookup_of(manifest(functions_only)))
    assert only and all(s.startswith("run_") for s in only) and set(only) < set(full)


def test_a_class_without_a_name_to_look_up_is_no_slot() -> None:
    """Items have a label in this release, locations have none: a lookup for a location could
    find nothing, so no tool offers it and no description sends the model to the lookup tool."""
    document = manifest(INVENTORY.scene())
    assert slots(lookup_of(document)) == ["do_move_item.item", "do_retire_item.item"]
    tool = by_name(document)["run_items_in_location"]
    assert "lookup" not in json.dumps(tool["input_schema"])
    assert "looked up first." in json.dumps(tool["input_schema"]["properties"]["location"])
    entity = by_name(document)["view_location_overview"]
    assert "lookup" not in json.dumps(entity["input_schema"])
    # nothing has a name in the first release: no lookup tool at all, every reference is an id
    bare = manifest(UNNAMED.scene())
    assert "lookup" not in by_name(bare) and "lookup" not in json.dumps(bare["tools"])
    assert (
        tm.lookup_refs(UNNAMED.model, UNNAMED.functions, UNNAMED.actions, UNNAMED.views, find=True)
        == ()
    )


def test_a_class_is_served_by_the_label_of_a_subclass() -> None:
    """The candidates of a slot are the class and its subclasses: a parent without a label of its
    own is served by the names of its subclasses (effective attributes are copied down)."""
    model = COMPANY.model
    base = "org:OrgUnit"
    assert model.label_attributes(base) and len(model.subtypes(base)) > 1
    stripped = replace(
        model.classes[base],
        attributes={n: a for n, a in model.classes[base].attributes.items() if a.role is None},
    )
    parent_bare = replace(model, classes={**model.classes, base: stripped})
    assert not parent_bare.label_attributes(base)
    assert dt.lookup_slot(parent_bare, dt.ref_type("unit", "", base))
    # a class and subclasses that all lack a name: not a slot
    nameless = {
        name: replace(
            found, attributes={n: a for n, a in found.attributes.items() if a.role is None}
        )
        for name, found in model.classes.items()
    }
    assert not dt.lookup_slot(replace(model, classes=nameless), dt.ref_type("unit", "", base))


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_the_slots_of_a_reader_of_items_name_only_what_it_sees() -> None:
    document = manifest(INVENTORY.reader(ITEMS))
    assert slots(lookup_of(document)) == ["do_retire_item.item"]
    text = json.dumps(document)
    for word in ("Location", "Category", "located_in", "move_item", "items_in_location"):
        assert word not in text, word


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_the_slots_of_a_reader_without_markings_name_no_marked_attribute() -> None:
    document = manifest(STAFF.reader(held=NOTHING))
    text = json.dumps(document)
    for word in ("salary", "badge_code", "PayGrade", "pay_grade", "stf:pay", "email"):
        assert word not in text, word
    assert slots(lookup_of(document)) == ref_inputs_of(document, STAFF.reader(held=NOTHING))
    personal = STAFF.reader(held=PERSONAL)
    assert slots(lookup_of(manifest(personal))) == ref_inputs_of(manifest(personal), personal)


def test_a_class_the_model_lost_is_no_slot() -> None:
    scene = COMPANY.scene()
    without = {k: v for k, v in scene.model.classes.items() if k != "org:Department"}
    stale = replace(scene, model=replace(scene.model, classes=without))
    left = slots(lookup_of(manifest(stale)))
    assert "view_members_report.unit" in slots(lookup_of(manifest(scene)))
    assert left
    assert "view_members_report.unit" not in left and "do_assign_position.person" in left


# ---- what it says -------------------------------------------------------------------------


def test_the_lookup_tool_has_the_keys_of_a_tool_but_no_entry() -> None:
    tool = lookup_of(manifest(COMPANY.scene()))
    assert (tool["kind"], tool["entry"], tool["entry_hash"], tool["module"]) == (
        "lookup",
        None,
        None,
        None,
    )
    assert (tool["status"], tool["replaced_by"], tool["featured"]) == ("active", None, False)
    assert tool["annotations"] == {
        "read_only": True,
        "dry_run": False,
        "reason": None,
        "paging": None,
    }
    covered = {
        k: tool[k] for k in ("name", "description", "input_schema", "output_schema", "annotations")
    }
    assert tool["hash"] == hash_value(covered)
    assert tool["description"].startswith("Find the entities that go by a name")


def test_the_input_is_a_closed_object_of_text_for_and_limit() -> None:
    schema = lookup_of(manifest(COMPANY.scene()))["input_schema"]
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == ["text", "for", "limit"]
    assert schema["required"] == ["text", "for"]
    assert schema["properties"]["limit"]["default"] == 50
    valid = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    slot = slots(lookup_of(manifest(COMPANY.scene())))[0]
    good = [
        {"text": "a", "for": slot},
        {"text": ["a", "b c"], "for": slot, "limit": 3},
        {"text": ["x"] * MAX_LOOKUP_TEXTS, "for": slot},
    ]
    for given in good:
        assert valid.is_valid(given), given
    bad = [
        {"text": "a"},
        {"for": slot},
        {"text": "a", "for": "run_nothing.x"},
        {"text": "", "for": slot},
        {"text": "   ", "for": slot},
        {"text": [], "for": slot},
        {"text": ["x"] * (MAX_LOOKUP_TEXTS + 1), "for": slot},
        {"text": [""], "for": slot},
        {"text": 5, "for": slot},
        {"text": "a", "for": slot, "limit": 0},
        {"text": "a", "for": slot, "limit": 501},
        {"text": "a", "for": slot, "limit": None},
        {"text": "a", "for": slot, "offset": 1},
    ]
    for given in bad:
        assert not valid.is_valid(given), given


def test_the_output_is_the_lookup_answer_with_its_page() -> None:
    from graph_cli.app_cli.commands.read.find import LOOKUP_OUTPUT

    schema = lookup_of(manifest(COMPANY.scene()))["output_schema"]
    Draft202012Validator.check_schema(schema)
    base = tm.standalone(LOOKUP_OUTPUT)
    assert list(schema["properties"]) == [*base["properties"], "page"]
    assert schema["required"] == [*base["required"], "page"]
    for key in base["properties"]:
        assert schema["properties"][key] == base["properties"][key]
    assert schema["properties"]["page"] == tm.standalone(tm.ref("page"))
    assert "additionalProperties" not in schema and schema["type"] == "object"


def test_the_errors_of_the_lookup_tool() -> None:
    document = manifest(COMPANY.scene())
    codes = lookup_of(document)["errors"]
    assert codes == sorted(codes) == list(tm.tool_errors("lookup"))
    assert {"tool.not_found", "tool.hash_mismatch", "access.denied"} <= set(codes)
    assert {"usage.invalid_argument", "usage.invalid_input", "internal.error"} <= set(codes)
    assert "usage.missing_argument" in codes  # a missing text or for
    # the codes a lookup tool cannot get: it names no type or relation, its slot is an enum
    for never in (
        "term.unknown",
        "term.no_labels",  # every slot has a name to look up
        "query.ambiguous_term",
        "view.parameter_unknown",
        "graph.not_selected",
    ):
        assert never not in codes, never
    for code in codes:
        assert code in document["errors"] and on_error(code)


def test_a_reference_input_names_the_lookup_tool_and_its_slot_when_it_is_listed() -> None:
    listed, bare = manifest(COMPANY.scene()), manifest(COMPANY.scene(), find=False)
    for slot in slots(lookup_of(listed)):
        tool_name, _, name = slot.partition(".")
        now = by_name(listed)[tool_name]["input_schema"]["properties"][name]
        before = by_name(bare)[tool_name]["input_schema"]["properties"][name]
        text = json.dumps(now)
        assert f"with the lookup tool (for: '{slot}')" in text, slot
        assert "lookup" not in json.dumps(before) and "looked up first." in json.dumps(before)
        # nothing else of the property changes
        assert text.replace(f", with the lookup tool (for: '{slot}')", "") == json.dumps(before)
    # a tool without a reference has the same hash, one with a reference another
    for name, tool in by_name(listed).items():
        if name == "lookup":
            continue
        refs = [s for s in slots(lookup_of(listed)) if s.startswith(name + ".")]
        assert (tool["hash"] != by_name(bare)[name]["hash"]) == bool(refs), name
        assert tool["entry_hash"] == by_name(bare)[name]["entry_hash"]


def test_a_stale_reference_names_no_lookup_tool() -> None:
    scene = INVENTORY.scene()
    without = {k: v for k, v in scene.model.classes.items() if k != "inv:Location"}
    stale = replace(scene, model=replace(scene.model, classes=without))
    tool = by_name(manifest(stale))["run_items_in_location"]
    assert "lookup" not in json.dumps(tool["input_schema"]["properties"]["location"])


# ---- selection and one tool ---------------------------------------------------------------


def test_the_lookup_tool_is_selected_by_its_name_and_by_its_kind() -> None:
    scene = COMPANY.scene()
    assert list(by_name(manifest(scene, tools=("lookup",)))) == ["lookup"]
    assert list(by_name(manifest(scene, kinds=("lookup",)))) == ["lookup"]
    assert "lookup" not in by_name(manifest(scene, kinds=("function", "action", "view")))
    assert "lookup" in by_name(manifest(scene, featured=True))  # it has no flag, like a view
    assert "lookup" in by_name(manifest(scene, exclude_deprecated=True))
    assert "lookup" not in by_name(manifest(scene, module="organization"))  # it belongs to none


def test_asking_for_the_lookup_tool_of_a_caller_that_has_none_is_tool_not_found() -> None:
    with pytest.raises(GraphCliError) as info:
        manifest(COMPANY.scene(), find=False, tools=("lookup",))
    assert info.value.code == "tool.not_found" and info.value.details["name"] == "lookup"
    assert "lookup" not in info.value.details["did_you_mean"]
    with pytest.raises(GraphCliError) as info:
        manifest(COMPANY.scene(), tools=("lookp",))
    assert info.value.details["did_you_mean"][0] == "lookup"


def scene_args(scene: Scene) -> tuple[CompiledModel, Any, Any, Any, frozenset[str]]:
    return (scene.model, scene.functions, scene.actions, scene.views, NO_OBLIGATIONS)


def test_the_hash_of_one_tool_is_the_hash_in_the_manifest() -> None:
    """``call --expect-hash`` builds one head: it must come out as the manifest's tool."""
    scene = COMPANY.scene()
    document = by_name(manifest(scene))
    refs = tm.lookup_refs(scene.model, scene.functions, scene.actions, scene.views, find=True)
    assert refs
    for name in ("lookup", "run_members_of_unit", "do_assign_position", "view_unit_overview"):
        (head,) = tm.heads(*scene_args(scene), only=name, refs=refs)
        assert tm.tool_hash(head, scene.model) == document[name]["hash"], name
    # without the right the lookup tool is no head and no input names it
    none = tm.lookup_refs(scene.model, scene.functions, scene.actions, scene.views, find=False)
    assert none == () and tm.heads(*scene_args(scene), only="lookup", refs=none) == ()
    (head,) = tm.heads(*scene_args(scene), only="run_members_of_unit", refs=none)
    assert (
        tm.tool_hash(head, scene.model)
        == by_name(manifest(scene, find=False))["run_members_of_unit"]["hash"]
    )


def test_the_lookup_head_alone_types_no_function(monkeypatch: pytest.MonkeyPatch) -> None:
    scene = COMPANY.scene()
    refs = dt.ref_inputs(scene.model, scene.functions, scene.actions, scene.views)

    def refuse(entry: Any, model: Any) -> Any:
        raise AssertionError("a function was typed for the lookup tool")

    monkeypatch.setattr(dt, "column_types_of", refuse)
    (head,) = tm.heads(*scene_args(scene), only="lookup", refs=refs)
    assert head.kind == "lookup" and head.entry is None


# ---- the slots ----------------------------------------------------------------------------


def test_a_slot_is_a_tool_property_and_maps_to_a_kind_qualified_lookup_for() -> None:
    scene = COMPANY.scene()
    found = {
        r.value: r for r in dt.ref_inputs(scene.model, scene.functions, scene.actions, scene.views)
    }
    assert found["run_members_of_unit.unit"].lookup_for == "function:members_of_unit.unit"
    assert found["view_members_report.unit"].lookup_for == "view:members_report.unit"
    assert found["view_unit_overview._entity"].lookup_for == "view:unit_overview._entity"
    assert found["do_assign_position.person"].lookup_for == "action:assign_position.person"
    assert found["view_unit_overview._entity"].of_class == "org:OrgUnit"
    # a view may share its name with a function: two tools, two slots, two forms
    clash = (
        dt.RefInput("function", "x", "run_x", "p", "c"),
        dt.RefInput("view", "x", "view_x", "p", "c"),
    )
    assert [r.value for r in clash] == ["run_x.p", "view_x.p"]
    assert [r.lookup_for for r in clash] == ["function:x.p", "view:x.p"]


def test_the_slots_follow_the_order_of_the_manifest() -> None:
    scene = COMPANY.scene()
    values = [
        r.value for r in dt.ref_inputs(scene.model, scene.functions, scene.actions, scene.views)
    ]
    names = [t["name"] for t in manifest(scene)["tools"]]
    order = [name.partition(".")[0] for name in values]
    assert order == sorted(order, key=names.index)
    entity_first = [v for v in values if v.startswith("view_unit_overview.")]
    assert entity_first == ["view_unit_overview._entity"]


# ---- a caller limited to its catalog -----------------------------------------------------------


def catalog_scene() -> Scene:
    """The company example as a caller sees it whose grants name a function and an entity view."""
    return replace(
        COMPANY.scene(),
        functions=tuple(f for f in COMPANY.functions if f.name == "members_of_unit"),
        actions=(),
        views=tuple(v for v in COMPANY.views if v.name == "unit_overview"),
    )


def catalog_manifest(**selection: Any) -> dict[str, Any]:
    scene = catalog_scene()
    found = tm.build_manifest(
        scene.entry,
        scene.model,
        functions=scene.functions,
        actions=scene.actions,
        views=scene.views,
        obligations=NO_OBLIGATIONS,
        selection=tm.Selection(**selection),
        find=True,
        rows=COMPANY.functions,
        exact_ids=True,
    )
    return json.loads(json.dumps(found))


def reference_schemas(document: dict[str, Any]) -> list[dict[str, Any]]:
    """The scalar schema of every reference input of the tools of the document."""
    found: list[dict[str, Any]] = []
    for tool in document["tools"]:
        for schema in tool["input_schema"]["properties"].values():
            for node in (schema, *schema.get("anyOf", ())):
                for item in (node, node.get("items", {})):
                    if isinstance(item, dict) and "x-graph-cli" in item:
                        found.append(item)
    return found


def test_a_catalog_caller_is_listed_the_lookup_tool_for_the_inputs_of_its_tools_only() -> None:
    document = catalog_manifest()
    assert [t["name"] for t in document["tools"]] == [
        "run_members_of_unit",
        "view_unit_overview",
        "lookup",
    ]
    assert slots(lookup_of(document)) == [
        "run_members_of_unit.unit",
        "view_unit_overview._entity",
    ]


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_catalog_callers_references_take_an_id_or_an_alias_never_trailing_hex() -> None:
    plain = manifest(COMPANY.scene())
    exact = catalog_manifest()
    assert reference_schemas(plain) and reference_schemas(exact)
    for schema in reference_schemas(exact):
        assert schema["pattern"] == EXACT_ENTITY_REF_PATTERN
        assert "trailing hex" not in schema.get("description", "")
        assert not re.fullmatch(schema["pattern"], "1f0e5d6c7b8a")  # a suffix is no reference
        assert re.fullmatch(schema["pattern"], "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a")
        assert re.fullmatch(schema["pattern"], "DEPT-0001")
    for tool in exact["tools"]:
        for schema in tool["input_schema"]["properties"].values():
            assert "trailing hex" not in json.dumps(schema)
    # every other caller keeps what the manifest said before
    for schema in reference_schemas(plain):
        assert schema["pattern"] == ENTITY_REF_PATTERN
    assert any("trailing hex" in json.dumps(t["input_schema"]) for t in plain["tools"])


def test_the_lookup_tool_of_a_catalog_caller_says_what_it_lists_and_leaves_out() -> None:
    narrow = lookup_of(catalog_manifest())["description"]
    wide = lookup_of(manifest(COMPANY.scene()))["description"]
    assert "texts[].excluded" not in narrow and "first page of active" in narrow
    assert "texts[].excluded" in wide
    assert narrow != wide


def test_the_hash_of_a_catalog_callers_tool_is_the_hash_in_its_manifest() -> None:
    scene = catalog_scene()
    document = by_name(catalog_manifest())
    refs = tm.lookup_refs(scene.model, scene.functions, scene.actions, scene.views, find=True)
    for name in ("lookup", "run_members_of_unit", "view_unit_overview"):
        (head,) = tm.heads(
            *scene_args(scene), only=name, refs=refs, rows=COMPANY.functions, exact_ids=True
        )
        assert tm.tool_hash(head, scene.model) == document[name]["hash"], name
    # the exactness is part of what a harness builds its tool from
    (loose,) = tm.heads(*scene_args(scene), only="run_members_of_unit", refs=refs)
    assert tm.tool_hash(loose, scene.model) != document["run_members_of_unit"]["hash"]


NEVER_TO_A_CATALOG_CALLER = {
    "entity.ambiguous_ref",
    "function.parameter_type",
    "action.parameter_type",
    "view.parameter_type",
    "view.class_mismatch",
}


def test_the_error_lists_of_a_catalog_caller_leave_out_the_codes_it_is_never_answered() -> None:
    narrow = catalog_manifest()
    for tool in narrow["tools"]:
        assert not NEVER_TO_A_CATALOG_CALLER & set(tool["errors"]), tool["name"]
        assert "entity.not_found" in tool["errors"] or tool["kind"] == "lookup", tool["name"]
    assert not NEVER_TO_A_CATALOG_CALLER & set(narrow["errors"])
    # everyone else still gets them
    plain = manifest(COMPANY.scene())
    listed = {c for t in plain["tools"] for c in t["errors"]}
    assert listed >= NEVER_TO_A_CATALOG_CALLER
    # and the lists differ per caller
    assert (
        by_name(narrow)["run_members_of_unit"]["errors"]
        != by_name(plain)["run_members_of_unit"]["errors"]
    )
