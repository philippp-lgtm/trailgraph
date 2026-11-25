"""The tool manifest of ``graph describe tools`` (tool manifest plan TM2, TM3, TM6, TM8): the
document, its selection, the schemas of the answers, the hashes, the error map, and what callers
who see less are given (goldens for ``company``, ``inventory``, a reader of items and readers
holding markings)."""

import json
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from support import tools
from support.fixtures import GOLDENS, assert_golden
from support.tools import ITEMS, NOTHING, PERSONAL, Scene

from graph_cli.app_cli import tool_manifest as tm
from graph_cli.app_cli.commands.do import DO_OUTPUT
from graph_cli.app_cli.commands.read.entity import VIEW_OUTPUT
from graph_cli.app_cli.commands.read.query import RUN_OUTPUT
from graph_cli.app_cli.output_schemas import DEFS
from graph_cli.cli_kit.output_schemas import obj, ref
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.error_codes import ERROR_CODES
from graph_cli.kernel.errors import GraphCliError
from graph_cli.services.describe_tools import KINDS, ToolColumn

COMPANY = tools.company()
INVENTORY = tools.inventory()
STAFF = tools.staff()
NO_OBLIGATIONS: frozenset[str] = frozenset()


def manifest(
    scene: Scene,
    obligations: frozenset[str] = NO_OBLIGATIONS,
    **selection: Any,
) -> dict[str, Any]:
    found = tm.build_manifest(
        scene.entry,
        scene.model,
        functions=scene.functions,
        actions=scene.actions,
        views=scene.views,
        obligations=obligations,
        selection=tm.Selection(**selection),
    )
    return json.loads(json.dumps(found))


def names(document: dict[str, Any]) -> list[str]:
    return [t["name"] for t in document["tools"]]


def by_name(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in document["tools"]}


def refs_in(node: Any) -> list[str]:
    """Every ``$ref`` and ``$defs`` key in a schema."""
    if isinstance(node, list):
        return [found for item in node for found in refs_in(item)]
    if not isinstance(node, dict):
        return []
    here = [k for k in node if k in ("$ref", "$defs")]
    return here + [found for value in node.values() for found in refs_in(value)]


# ---- the document -----------------------------------------------------------------------


def test_the_manifest_is_a_model_document_with_tools_and_errors() -> None:
    document = manifest(COMPANY.scene())
    assert list(document) == [
        "schema",
        "graph",
        "ontology",
        "tools_format",
        "version",
        "tools",
        "errors",
    ]
    assert document["schema"] == "graph_cli.model/1" and document["tools_format"] == 1
    assert document["ontology"] == {"bundle": "company", "version": "1.1.0"}
    assert document["version"].startswith("sha256:")


def test_a_tool_has_every_key_of_the_format() -> None:
    for tool in manifest(COMPANY.scene())["tools"]:
        assert list(tool) == [
            "name",
            "kind",
            "entry",
            "description",
            "input_schema",
            "output_schema",
            "annotations",
            "status",
            "replaced_by",
            "featured",
            "module",
            "hash",
            "entry_hash",
            "errors",
        ]
        assert tool["kind"] in KINDS
        assert tool["hash"].startswith("sha256:") and tool["entry_hash"].startswith("sha256:")
        assert set(tool["annotations"]) == {"read_only", "dry_run", "reason", "paging"}


def test_tools_come_functions_then_actions_then_views_each_by_name() -> None:
    document = manifest(COMPANY.scene())
    kinds = [t["kind"] for t in document["tools"]]
    assert kinds == sorted(kinds, key=KINDS.index)
    for kind in KINDS:
        wanted = [t["name"] for t in document["tools"] if t["kind"] == kind]
        assert wanted == sorted(wanted)


def test_a_graph_without_entries_has_an_empty_manifest() -> None:
    empty = replace(COMPANY.scene(), functions=(), actions=(), views=())
    document = manifest(empty)
    assert document["tools"] == [] and document["errors"] == {}
    assert document["version"] == hash_value({"tools": [], "errors": {}})


def test_the_manifest_is_the_same_every_time_and_the_version_follows_what_is_listed() -> None:
    first, again = manifest(COMPANY.scene()), manifest(COMPANY.scene())
    assert first == again
    assert first["version"] == hash_value({"tools": first["tools"], "errors": first["errors"]})
    narrower = manifest(COMPANY.scene(), kinds=("action",))
    assert narrower["version"] != first["version"]


def test_a_tool_hash_covers_what_a_harness_builds_its_tool_from() -> None:
    tool = by_name(manifest(COMPANY.scene()))["run_members_of_unit"]
    covered = {
        k: tool[k] for k in ("name", "description", "input_schema", "output_schema", "annotations")
    }
    assert tool["hash"] == hash_value(covered)


def test_an_enum_change_changes_the_tool_hash_but_not_the_entry_hash() -> None:
    scene = INVENTORY.scene()
    shorter = replace(
        scene.model,
        enums={
            **scene.model.enums,
            "inv:Condition": replace(
                scene.model.enums["inv:Condition"],
                values=scene.model.enums["inv:Condition"].values[:3],
            ),
        },
    )
    before = by_name(manifest(scene))["run_items_by_condition"]
    after = by_name(manifest(replace(scene, model=shorter)))["run_items_by_condition"]
    assert before["entry_hash"] == after["entry_hash"]
    assert before["hash"] != after["hash"]
    assert before["input_schema"]["properties"]["condition"]["enum"][-1] == "damaged"
    assert after["input_schema"]["properties"]["condition"]["enum"][-1] == "worn"


def test_other_tools_keep_their_hash_when_an_enum_changes() -> None:
    scene = INVENTORY.scene()
    shorter = replace(
        scene.model,
        enums={
            "inv:Condition": replace(
                scene.model.enums["inv:Condition"],
                values=scene.model.enums["inv:Condition"].values[:3],
            )
        },
    )
    before, after = by_name(manifest(scene)), by_name(manifest(replace(scene, model=shorter)))
    same = [n for n in before if before[n]["hash"] == after[n]["hash"]]
    assert "run_items_in_location" in same and "run_items_by_condition" not in same


# ---- the schemas --------------------------------------------------------------------------


def every_tool() -> list[dict[str, Any]]:
    return [
        t
        for scene in (COMPANY.scene(), INVENTORY.scene(), STAFF.scene())
        for t in manifest(scene)["tools"]
    ]


def test_every_schema_is_valid_json_schema_2020_12_with_an_object_at_its_root() -> None:
    assert len(every_tool()) > 30
    for tool in every_tool():
        for key in ("input_schema", "output_schema"):
            schema = tool[key]
            Draft202012Validator.check_schema(schema)
            assert schema["type"] == "object", (tool["name"], key)


def test_every_schema_stands_alone() -> None:
    for tool in every_tool():
        assert refs_in(tool["input_schema"]) == [] and refs_in(tool["output_schema"]) == []


def test_input_schemas_are_closed_and_output_schemas_stay_open() -> None:
    for tool in every_tool():
        assert tool["input_schema"]["additionalProperties"] is False
        assert "additionalProperties" not in tool["output_schema"]


def test_a_function_answers_the_run_answer_with_its_page_and_typed_columns() -> None:
    tool = by_name(manifest(COMPANY.scene()))["run_members_of_unit"]
    schema = tool["output_schema"]
    assert list(schema["properties"]) == ["function", "columns", "items", "page"]
    assert schema["required"] == ["function", "columns", "items", "page"]
    row = schema["properties"]["items"]["items"]
    entry = next(f for f in COMPANY.functions if f.name == "members_of_unit")
    columns = [r.column for r in entry.defn.returns]
    assert list(row["properties"]) == columns and row["required"] == columns
    for r in entry.defn.returns:
        assert row["properties"][r.column] == {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": r.summary,
        }
    assert schema["properties"]["page"]["required"] == [
        "total",
        "shown",
        "limit",
        "offset",
        "truncated",
        "next_offset",
    ]


def test_the_function_answer_is_the_run_output_but_for_items_and_page() -> None:
    """``function_output`` rebuilds ``RUN_OUTPUT``: it must not drift from it."""
    base = tm.standalone(RUN_OUTPUT)
    built = tm.function_output([ToolColumn("a", "A.", None)], COMPANY.model)
    assert set(base["properties"]) | {"page"} == set(built["properties"])
    for key in set(base["properties"]) - {"items"}:
        assert built["properties"][key] == base["properties"][key]
    assert base["properties"]["items"]["type"] == "array"
    assert built["properties"]["page"] == tm.standalone(ref("page"))


def test_an_action_answers_the_write_result_and_a_view_the_view_model() -> None:
    document = by_name(manifest(COMPANY.scene()))
    assert document["do_assign_position"]["output_schema"] == tm.standalone(DO_OUTPUT)
    assert document["view_unit_overview"]["output_schema"] == tm.standalone(VIEW_OUTPUT)
    action = document["do_assign_position"]["output_schema"]["properties"]
    assert {"dry_run", "findings", "action", "entities"} <= set(action)
    assert {"view", "sections", "bytes"} <= set(
        document["view_unit_overview"]["output_schema"]["properties"]
    )


def test_standalone_inlines_every_definition_and_refuses_a_cycle() -> None:
    assert tm.standalone(ref("page")) == DEFS["page"]
    nested = tm.standalone(obj({"p": ref("page"), "q": {"type": "array", "items": ref("hint")}}))
    assert refs_in(nested) == [] and nested["properties"]["q"]["items"] == DEFS["hint"]
    with pytest.raises(ValueError, match="refers to itself"):
        tm.standalone(ref("loop"), {"loop": obj({"again": ref("loop")})})


# ---- errors --------------------------------------------------------------------------------


def test_a_tool_lists_the_codes_of_its_command_the_call_and_the_transport() -> None:
    document = by_name(manifest(COMPANY.scene()))
    run = document["run_members_of_unit"]["errors"]
    do = document["do_assign_position"]["errors"]
    view = document["view_unit_overview"]["errors"]
    assert {"function.parameter_invalid", "function.stale"} <= set(run)
    assert {"action.precondition_failed", "action.reason_required", "action.stale"} <= set(do)
    assert {"view.cursor_stale", "view.stale"} <= set(view)
    assert "access.batch_too_large" in do
    for codes in (run, do, view):  # a call of an entry the caller cannot see is tool.not_found
        assert not {"function.not_found", "action.not_found", "view.not_found"} & set(codes)
    for codes in (run, do, view):
        assert {
            "tool.not_found",
            "tool.hash_mismatch",
            "remote.unauthorized",
            "access.denied",
        } <= set(codes)
        assert codes == sorted(codes)
        for left_out in tm.NOT_FOR_TOOLS:
            assert left_out not in codes


def test_the_error_map_has_every_listed_code_with_its_registry_facts_and_on_error() -> None:
    document = manifest(COMPANY.scene())
    listed = {c for t in document["tools"] for c in t["errors"]}
    assert set(document["errors"]) == listed
    for code, info in document["errors"].items():
        spec = ERROR_CODES[code]
        assert info["category"] == spec.category.value
        assert info["retryable"] is spec.retryable and info["summary"] == spec.summary
    assert document["errors"]["action.precondition_failed"]["on_error"] == "check_state"
    assert document["errors"]["view.cursor_stale"]["on_error"] == "restart"
    assert document["errors"]["tool.hash_mismatch"]["on_error"] == "refresh"
    assert document["errors"]["backend.lock_timeout"]["on_error"] == "retry"
    assert document["errors"]["function.parameter_invalid"]["on_error"] == "fix_input"


def test_the_error_map_covers_only_the_codes_of_the_tools_listed() -> None:
    views_only = manifest(COMPANY.scene(), kinds=("view",))
    assert "view.cursor_stale" in views_only["errors"]
    assert "action.precondition_failed" not in views_only["errors"]
    assert "function.parameter_invalid" not in views_only["errors"]


# ---- the selection -------------------------------------------------------------------------


def test_tool_selects_by_name_in_the_order_of_the_manifest() -> None:
    document = manifest(COMPANY.scene(), tools=("view_unit_overview", "run_employer_of"))
    assert names(document) == ["run_employer_of", "view_unit_overview"]


def test_a_name_that_is_no_tool_is_tool_not_found_with_the_close_names() -> None:
    with pytest.raises(GraphCliError) as info:
        manifest(COMPANY.scene(), tools=("run_employer_off", "do_nothing"))
    error = info.value
    assert error.code == "tool.not_found" and error.details["name"] == "run_employer_off"
    assert error.details["did_you_mean"][0] == "run_employer_of"
    assert error.details["missing"] == ["run_employer_off", "do_nothing"]
    assert "1 more" in error.message


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_hidden_tool_is_as_unknown_as_a_missing_one_and_is_never_suggested() -> None:
    reader = INVENTORY.reader(ITEMS)
    with pytest.raises(GraphCliError) as info:
        manifest(reader, tools=("do_move_item",))
    assert info.value.code == "tool.not_found"
    assert "do_move_item" not in info.value.details["did_you_mean"]
    assert info.value.details["did_you_mean"] == ["do_retire_item"]


def test_kind_module_featured_and_deprecation_narrow_the_list() -> None:
    scene = COMPANY.scene()
    full = manifest(scene)
    assert {t["kind"] for t in manifest(scene, kinds=("function",))["tools"]} == {"function"}
    module = manifest(scene, module="org")
    assert module["tools"] and {t["module"] for t in module["tools"]} == {"organization"}
    assert names(manifest(scene, module="organization")) == names(module)
    featured = manifest(scene, featured=True)
    assert [t for t in featured["tools"] if t["kind"] != "view" and not t["featured"]] == []
    assert len(featured["tools"]) < len(full["tools"])
    assert {t["name"] for t in full["tools"] if t["kind"] == "view"} <= set(names(featured))


def test_deprecated_tools_are_listed_unless_excluded() -> None:
    scene = COMPANY.scene()
    assert "view_legacy_unit_overview" in names(manifest(scene))
    assert "view_legacy_unit_overview" not in names(manifest(scene, exclude_deprecated=True))
    kept = by_name(manifest(scene))["view_legacy_unit_overview"]
    assert (kept["status"], kept["replaced_by"]) == ("deprecated", "view_unit_overview")


def test_an_unknown_module_is_term_unknown() -> None:
    with pytest.raises(GraphCliError) as info:
        manifest(COMPANY.scene(), module="no_such")
    assert info.value.code == "term.unknown"


def test_the_filters_combine() -> None:
    document = manifest(
        COMPANY.scene(), tools=("run_employer_of", "do_assign_position"), kinds=("action",)
    )
    assert names(document) == ["do_assign_position"]


# ---- rendered for the caller -------------------------------------------------------------------


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_reader_of_items_gets_a_manifest_without_what_it_may_not_see() -> None:
    document = manifest(INVENTORY.reader(ITEMS))
    text = json.dumps(document)
    for word in (
        "Location",
        "Category",
        "located_in",
        "in_category",
        "move_item",
        "items_in_location",
    ):
        assert word not in text, word
    assert names(document) == ["run_items_by_condition", "do_retire_item"]
    assert set(manifest(INVENTORY.scene())["errors"]) >= set(document["errors"])


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_reader_without_markings_gets_no_tool_over_a_marked_attribute() -> None:
    document = manifest(STAFF.reader(held=NOTHING))
    text = json.dumps(document)
    for word in ("salary", "badge_code", "PayGrade", "pay_grade", "stf:pay", "email"):
        assert word not in text, word
    assert "do_set_phone" in names(document)


def test_the_manifest_of_every_reader_differs_by_what_it_sees() -> None:
    versions = {
        manifest(STAFF.scene())["version"],
        manifest(STAFF.reader(held=PERSONAL))["version"],
        manifest(STAFF.reader(held=NOTHING))["version"],
    }
    assert len(versions) == 3


def test_a_caller_whose_policy_demands_a_reason_gets_it_required() -> None:
    plain = by_name(manifest(COMPANY.scene()))["do_assign_position"]
    needs = {
        n.action for n in next(a for a in COMPANY.actions if a.name == "assign_position").needs
    }
    bound = by_name(manifest(COMPANY.scene(), frozenset(needs)))["do_assign_position"]
    assert "_reason" not in plain["input_schema"]["required"]
    assert "_reason" in bound["input_schema"]["required"]
    assert bound["annotations"]["reason"] == "required" and bound["hash"] != plain["hash"]


# ---- goldens ---------------------------------------------------------------------------------


def golden(name: str, document: dict[str, Any]) -> None:
    data = json.loads(json.dumps(document))
    del data["graph"]["id"]
    assert_golden(GOLDENS / "tools" / f"{name}.json", (json.dumps(data, indent=2) + "\n").encode())


def test_goldens_of_the_example_graphs() -> None:
    golden("company_1.1.0", manifest(COMPANY.scene()))
    golden("inventory_1.1.0", manifest(INVENTORY.scene()))


def test_goldens_of_a_reader_of_items() -> None:
    golden("limited_inventory_items", manifest(INVENTORY.reader(ITEMS)))


@pytest.mark.parametrize(
    ("label", "held"), [("holder", tools.EVERYTHING), ("personal", PERSONAL), ("nothing", NOTHING)]
)
def test_goldens_of_readers_with_markings(label: str, held: frozenset[str]) -> None:
    scene = STAFF.scene() if held == tools.EVERYTHING else STAFF.reader(held=held)
    golden(f"markings_staff_{label}", manifest(scene))
