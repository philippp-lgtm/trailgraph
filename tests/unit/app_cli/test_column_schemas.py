"""The schemas of the cells of a function's result columns in the tool manifest (tool manifest plan
TM6): one per column type, nullability, lists, the enum values and entity attributes of the model
the caller sees, and what the manifest of the example graphs types."""

import json
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from support import tools
from support.tools import ITEMS, NOTHING, PERSONAL, Scene

from graph_cli.app_cli import tool_manifest as tm
from graph_cli.app_cli.column_schemas import column_schema, entity_schema, untyped
from graph_cli.kernel.canonical import hash_value
from graph_cli.kinetic.return_types import COLUMN_KINDS, ColumnType

COMPANY = tools.company()
INVENTORY = tools.inventory()
STAFF = tools.staff()
MODEL = INVENTORY.model
CHECKER = FormatChecker()


def manifest(scene: Scene) -> dict[str, Any]:
    found = tm.build_manifest(
        scene.entry,
        scene.model,
        functions=scene.functions,
        actions=scene.actions,
        views=scene.views,
        obligations=frozenset(),
    )
    return json.loads(json.dumps(found))


def by_name(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in document["tools"]}


def cells(tool: dict[str, Any]) -> dict[str, Any]:
    """The schema of the columns of a function tool."""
    return tool["output_schema"]["properties"]["items"]["items"]["properties"]


def valid(schema: dict[str, Any], value: Any) -> bool:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=CHECKER).is_valid(value)


def not_null(kind: str, **more: Any) -> dict[str, Any]:
    return column_schema(ColumnType(kind, nullable=False, **more), "S.", MODEL)


# ---- one schema per type ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("string", "text", 1),
        ("integer", 7, "7"),
        ("boolean", False, "false"),
        ("decimal", "12.50", 12.5),
        ("date", "2026-03-01", "01.03.2026"),
        ("datetime", "2026-03-01T10:00:00.000Z", "2026-03-01"),
        ("lang", {"lang": "en", "value": "Word"}, "Word"),
        ("ref", "6ff7c31b-0b86-4d25-a7f5-9dd1e0fb4b6f", "a name"),
        ("any", {"a": [1, None]}, None),
    ],
)
def test_a_type_accepts_the_cells_it_stands_for_and_refuses_others(
    kind: str, good: Any, bad: Any
) -> None:
    schema = not_null(kind)
    assert valid(schema, good)
    if kind != "any":
        assert not valid(schema, bad)


def test_every_kind_has_a_valid_schema() -> None:
    for kind in COLUMN_KINDS:
        extra: dict[str, Any] = {"enum": "inv:Condition"} if kind == "enum" else {}
        found = column_schema(ColumnType(kind, **extra), "S.", MODEL)
        Draft202012Validator.check_schema(found)
        assert found["description"] == "S."


def test_an_enum_lists_the_values_of_the_model_it_is_rendered_for() -> None:
    schema = not_null("enum", enum="inv:Condition")
    assert schema["enum"] == [v.name for v in MODEL.enums["inv:Condition"].values]
    assert valid(schema, "damaged") and not valid(schema, "broken")


def test_an_enum_the_model_lacks_is_a_string() -> None:
    assert not_null("enum", enum="inv:Gone") == {"type": "string", "description": "S."}
    assert not_null("enum") == {"type": "string", "description": "S."}


def test_a_reference_names_its_class_and_takes_what_a_command_takes() -> None:
    schema = not_null("ref", of_class="inv:Item")
    assert schema["x-graph-cli"] == {"class": "inv:Item"}
    assert valid(schema, "ITEM-0001") and valid(schema, "6ff7c31b") and not valid(schema, "Item 1")
    assert "x-graph-cli" not in not_null("ref")


def test_a_list_is_an_array_of_the_type() -> None:
    schema = column_schema(ColumnType("date", many=True, nullable=False), "S.", MODEL)
    assert schema["type"] == "array" and schema["items"]["format"] == "date"
    assert valid(schema, ["2026-03-01"]) and valid(schema, []) and not valid(schema, "2026-03-01")


def test_a_nullable_type_also_accepts_null_and_a_type_that_never_is_null_does_not() -> None:
    nullable = column_schema(ColumnType("integer"), "S.", MODEL)
    assert nullable["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert valid(nullable, None) and valid(nullable, 3)
    assert not valid(not_null("integer"), None)
    both = column_schema(ColumnType("integer", many=True), "S.", MODEL)
    assert valid(both, None) and valid(both, [1, 2]) and not valid(both, [None])


def test_an_untyped_column_is_any_value_and_says_so() -> None:
    schema = column_schema(None, "S.", MODEL)
    assert schema == untyped("S.") == {"description": "S.", "x-graph-cli": {"typed": False}}
    assert valid(schema, {"anything": [1]}) and valid(schema, None)


# ---- entities --------------------------------------------------------------------------------


def lean_item() -> dict[str, Any]:
    return {
        "id": "6ff7c31b-0b86-4d25-a7f5-9dd1e0fb4b6f",
        "alias": "ITEM-0001",
        "type": "inv:Item",
        "rev": 3,
        "record_status": "active",
        "attrs": {"name": "Drill", "quantity": 2, "condition": "good", "acquired_on": "2026-01-02"},
        "edges": [
            {"rel": "inv:located_in", "target": "6ff7c31b", "target_rev": 1, "qualifiers": {}}
        ],
    }


def test_an_entity_is_the_lean_entity_with_the_attributes_of_its_class() -> None:
    schema = entity_schema("inv:Item", MODEL)
    assert schema["x-graph-cli"] == {"class": "inv:Item"}
    assert schema["required"] == ["id", "alias", "type", "rev", "record_status", "attrs", "edges"]
    attrs = schema["properties"]["attrs"]["properties"]
    assert set(attrs) == set(MODEL.classes["inv:Item"].attributes)
    assert attrs["condition"]["enum"] == [v.name for v in MODEL.enums["inv:Condition"].values]
    assert attrs["quantity"] == {"type": "integer"}
    assert attrs["acquired_on"] == {"type": "string", "format": "date"}
    assert valid(schema, lean_item())
    assert valid(schema, {**lean_item(), "valid_from": "2026-01-01", "valid_to": None})


def test_an_entity_with_an_attribute_of_the_wrong_type_or_a_missing_key_is_refused() -> None:
    schema = entity_schema("inv:Item", MODEL)
    assert not valid(schema, {**lean_item(), "attrs": {"quantity": "two"}})
    assert not valid(schema, {**lean_item(), "attrs": {"condition": "broken"}})
    for key in ("id", "alias", "type", "rev", "record_status", "attrs", "edges"):
        assert not valid(schema, {k: v for k, v in lean_item().items() if k != key}), key
    assert valid(schema, {**lean_item(), "attrs": {}})  # attributes that are not set are absent


def test_an_entity_of_a_subclass_may_carry_more_attributes() -> None:
    schema = entity_schema("inv:Item", MODEL)
    assert valid(schema, {**lean_item(), "attrs": {"name": "Drill", "other": [1, 2]}})


def test_an_entity_without_a_known_class_is_the_lean_entity_with_any_attributes() -> None:
    for of_class in (None, "inv:Gone"):
        schema = entity_schema(of_class, MODEL)
        assert schema["properties"]["attrs"] == {"type": "object", "additionalProperties": {}}
        assert valid(schema, lean_item())
    assert "x-graph-cli" not in entity_schema(None, MODEL)


def test_a_list_attribute_is_an_array_and_a_language_tagged_one_an_object() -> None:
    from support.fixtures import compiled_model

    notes = compiled_model("notes_and_sources", "1.3.0")
    attrs = entity_schema("nas:Topic", notes)["properties"]["attrs"]["properties"]
    assert attrs["synonyms"] == {"type": "array", "items": {"type": "string"}}
    glossary = entity_schema("nas:GlossaryEntry", notes)["properties"]["attrs"]["properties"]
    assert glossary["heading"]["type"] == "array"
    assert glossary["usage_hint"]["required"] == ["lang", "value"]


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_the_attributes_of_an_entity_are_the_ones_the_caller_sees() -> None:
    everything = by_name(manifest(extended_staff().scene()))["run_employee_cards"]
    personal = by_name(manifest(extended_staff().reader(held=PERSONAL)))["run_employee_cards"]
    nothing = by_name(manifest(extended_staff().reader(held=NOTHING)))["run_employee_cards"]

    def attrs(tool: dict[str, Any]) -> set[str]:
        employee = cells(tool)["employee"]  # a node of a MATCH is never null
        return set(employee["properties"]["attrs"]["properties"])

    assert {"email", "salary", "badge_code", "name", "shift"} <= attrs(everything)
    assert {"email", "name"} <= attrs(personal) and "salary" not in attrs(personal)
    assert {"pay_grade", "birth_date", "badge_code"} <= attrs(personal)
    assert attrs(nothing) == {"name", "phone", "shift", "stage"}
    text = json.dumps(nothing)
    for word in ("salary", "badge_code", "email", "birth_date", "PayGrade", "pay_grade"):
        assert word not in text, word


EMPLOYEE_CARDS = """\
schema: graph_cli.function/1
name: employee_cards
summary: Employees with their shift.
use_when: Who works which shift.
query: |
  MATCH (e:Employee) RETURN e AS employee, e.shift AS shift, e.id AS id
returns:
  - column: employee
    summary: The employee.
  - column: shift
    summary: The shift.
  - column: id
    summary: The id of the employee.
examples:
  - question: Who works which shift?
"""


def extended_staff() -> tools.Catalog:
    return tools.staff({"employee_cards.yaml": EMPLOYEE_CARDS})


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_reader_gets_the_enum_values_it_sees_in_a_column() -> None:
    tool = by_name(manifest(extended_staff().reader(held=PERSONAL)))["run_employee_cards"]
    shift = cells(tool)["shift"]["anyOf"][0]
    assert shift["enum"] == ["day", "night"]


# ---- the manifest of the example graphs -----------------------------------------------------


def function_tools(catalog: tools.Catalog) -> list[dict[str, Any]]:
    return [t for t in manifest(catalog.scene())["tools"] if t["kind"] == "function"]


@pytest.mark.parametrize("catalog", [COMPANY, INVENTORY], ids=["company", "inventory"])
def test_every_function_column_of_the_example_graphs_is_typed(catalog: tools.Catalog) -> None:
    found = function_tools(catalog)
    assert found
    for tool in found:
        for column, schema in cells(tool).items():
            assert schema.get("x-graph-cli") != {"typed": False}, (tool["name"], column)


def test_the_example_columns_have_the_types_of_the_attributes_they_read() -> None:
    document = by_name(manifest(COMPANY.scene()))
    shareholders = cells(document["run_shareholders_of"])
    assert shareholders["percent"]["anyOf"][0]["pattern"]  # a decimal travels as text
    assert shareholders["since"]["anyOf"][0]["format"] == "date"
    alias = shareholders["shareholder_alias"]["anyOf"][0]
    assert alias["type"] == "string" and "x-graph-cli" not in alias  # a holder of no known class
    assert cells(document["run_reporting_chain"])["level"]["anyOf"][0] == {"type": "integer"}
    required = cells(document["run_required_trainings"])["completed_on"]
    assert required["anyOf"][0]["format"] == "date"
    in_location = cells(by_name(manifest(INVENTORY.scene()))["run_items_in_location"])
    assert in_location["quantity"]["anyOf"][0] == {"type": "integer"}


def test_a_column_that_nothing_types_stays_marked_untyped() -> None:
    document = by_name(manifest(STAFF.scene()))
    echoed = cells(document["run_by_grade"])
    assert echoed["grade"] == {"description": "The grade.", "x-graph-cli": {"typed": False}}
    assert echoed["name"]["anyOf"][0] == {"type": "string"}


def test_a_changed_enum_changes_the_tool_hash_through_the_output_but_not_the_entry_hash() -> None:
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
    assert cells(before)["condition"]["anyOf"][0]["enum"][-1] == "damaged"
    assert cells(after)["condition"]["anyOf"][0]["enum"][-1] == "worn"
    assert before["entry_hash"] == after["entry_hash"] and before["hash"] != after["hash"]
    assert after["hash"] == hash_value(
        {
            k: after[k]
            for k in ("name", "description", "input_schema", "output_schema", "annotations")
        }
    )


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_reader_of_items_gets_columns_without_what_it_may_not_see() -> None:
    document = manifest(INVENTORY.reader(ITEMS))
    text = json.dumps(document)
    for word in ("Location", "Category", "located_in", "in_category"):
        assert word not in text, word
    assert cells(by_name(document)["run_items_by_condition"])["item"]["anyOf"][0] == {
        "type": "string"
    }
