"""S: the typed columns of a function tool (tool manifest plan TM6): the answers of ``graph run``
and ``graph call`` validate against the output schema the manifest gives, for every kind of column
the inference knows (entities, references, attributes of every datatype, qualifiers, counts, lists,
literals) and for the null of an ``OPTIONAL MATCH``; a corrupted cell does not."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from jsonschema import Draft202012Validator, FormatChecker
from support.admin import admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES

ENV = {"GRAPH_CLI_GRAPH": "example_graph", "GRAPH_CLI_SPILL_BYTES": "0"}

ITEM_CARDS = """\
schema: graph_cli.function/1
name: item_cards
summary: Every item with its location and the item it is a part of.
use_when: A full picture of each item.
query: |
  MATCH (i:Item)-[:located_in]->(l:Location)
  OPTIONAL MATCH (i)-[:part_of]->(p:Item)
  RETURN i AS item, i.id AS id, i.alias AS alias, i.name AS name, i.quantity AS quantity,
    i.condition AS condition, i.acquired_on AS acquired_on, i.status AS status, i.rev AS rev,
    i.created_at AS created_at, l AS location, p AS parent, p.name AS parent_name,
    p.id AS parent_id, 'fixed' AS tag, 1.5 AS ratio, true AS flag
  ORDER BY name
returns:
  - {column: item, summary: The item.}
  - {column: id, summary: Its id.}
  - {column: alias, summary: Its alias.}
  - {column: name, summary: Its name.}
  - {column: quantity, summary: Its quantity.}
  - {column: condition, summary: Its condition.}
  - {column: acquired_on, summary: When it was acquired.}
  - {column: status, summary: Its availability.}
  - {column: rev, summary: Its revision.}
  - {column: created_at, summary: When it was created.}
  - {column: location, summary: Where it is.}
  - {column: parent, summary: The item it is a part of.}
  - {column: parent_name, summary: Its name.}
  - {column: parent_id, summary: Its id.}
  - {column: tag, summary: A literal.}
  - {column: ratio, summary: A decimal literal.}
  - {column: flag, summary: A boolean literal.}
examples:
  - question: Show every item with its place.
"""

CONDITION_COUNTS = """\
schema: graph_cli.function/1
name: condition_counts
summary: Items per condition with their names.
use_when: How many items are in each condition.
query: |
  MATCH (i:Item)
  RETURN i.condition AS condition, count(i) AS items, collect(i.name) AS names,
    collect(i) AS cards, sum(i.quantity) AS units
  ORDER BY condition
returns:
  - {column: condition, summary: The condition.}
  - {column: items, summary: How many.}
  - {column: names, summary: Their names.}
  - {column: cards, summary: The items.}
  - {column: units, summary: Their units., type: integer}
examples:
  - question: How many items are there per condition?
"""


DECIMALS = """\
schema: graph_cli.function/1
name: decimal_cells
summary: Decimal values written in several ways.
use_when: Checking how decimals are shown.
query: |
  MATCH (l:Location)
  RETURN 1e3 AS thousand, 0.0000001 AS tiny, 1e-7 * 1 AS product, -0.0 AS negative_zero,
    1.50 AS kept_zeros, 1e999999999 AS huge, 0.1 + 0.2 AS sum
  LIMIT 1
returns:
  - {column: thousand, summary: An exponent literal.}
  - {column: tiny, summary: A small literal.}
  - {column: product, summary: A small product., type: decimal}
  - {column: negative_zero, summary: Negative zero., type: decimal}
  - {column: kept_zeros, summary: Trailing zeros.}
  - {column: huge, summary: A literal with a huge exponent.}
  - {column: sum, summary: A sum., type: decimal}
examples:
  - question: How are decimals shown?
"""


def documents() -> dict[str, Any]:
    ops: list[dict[str, Any]] = [
        {
            "op": "create",
            "type": "inv:Location",
            "local": "shelf",
            "attrs": {"name": "Shelf", "code": "SH"},
        }
    ]
    for index, name in enumerate(("Drill", "Drill bit", "Saw")):
        attrs: dict[str, Any] = {"name": name, "condition": ("good", "worn", "damaged")[index]}
        if index == 0:
            attrs["acquired_on"] = "2026-01-02"
        edges: list[dict[str, Any]] = [{"rel": "inv:located_in", "target": "@shelf"}]
        if index == 1:
            edges.append({"rel": "inv:part_of", "target": "@item0"})
        ops.append(
            {
                "op": "create",
                "type": "inv:Item",
                "local": f"item{index}",
                "attrs": attrs,
                "edges": edges,
            }
        )
    return {"ops": ops}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    folder = tmp_path / "functions"
    shutil.copytree(FIXTURES / "functions" / "inventory" / "valid", folder)
    (folder / "item_cards.yaml").write_text(ITEM_CARDS)
    (folder / "condition_counts.yaml").write_text(CONDITION_COUNTS)
    (folder / "decimal_cells.yaml").write_text(DECIMALS)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope
    assert published.warning_codes == ["admin.function_single_ref"]  # every column is typed
    write_documents(root, "example_graph", [documents()])
    return root


def manifest(run_cli: CliRunner) -> dict[str, dict[str, Any]]:
    found = app(run_cli, "describe", "tools", env=ENV)
    assert found.code == 0, found.envelope
    return {t["name"]: t for t in found.data["tools"]}


def unwrapped(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema of a column without its ``null`` alternative."""
    return schema["anyOf"][0] if "anyOf" in schema else schema


def answers(run_cli: CliRunner, tool: dict[str, Any]) -> dict[str, Any]:
    called = app(run_cli, "call", tool["name"], env=ENV)
    assert called.code == 0, called.envelope
    return dict(called.data)


def validator(tool: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(tool["output_schema"], format_checker=FormatChecker())


def test_the_answer_of_every_kind_of_column_validates_against_its_schema(
    run_cli: CliRunner, root: Path
) -> None:
    for name in ("run_item_cards", "run_condition_counts"):
        tool = manifest(run_cli)[name]
        data = answers(run_cli, tool)
        assert data["items"], name
        found = list(validator(tool).iter_errors(data))
        assert [e.message for e in found] == [], name


def test_the_schemas_are_the_types_the_query_makes_certain(run_cli: CliRunner, root: Path) -> None:
    cards = manifest(run_cli)["run_item_cards"]["output_schema"]["properties"]["items"]["items"]
    columns = cards["properties"]
    assert unwrap_all(columns) == {
        "item": "entity inv:Item",
        "id": "ref inv:Item",
        "alias": "ref inv:Item",
        "name": "string",
        "quantity": "integer",
        "condition": "enum",
        "acquired_on": "date",
        "status": "string",
        "rev": "integer",
        "created_at": "date-time",
        "location": "entity inv:Location",
        "parent": "entity inv:Item",
        "parent_name": "string",
        "parent_id": "ref inv:Item",
        "tag": "string",
        "ratio": "decimal",
        "flag": "boolean",
    }
    nullable = {c for c, s in columns.items() if "anyOf" in s}
    assert nullable == {
        "alias",
        "name",
        "quantity",
        "condition",
        "acquired_on",
        "status",
        "parent",
        "parent_name",
        "parent_id",
    }
    counts = manifest(run_cli)["run_condition_counts"]["output_schema"]["properties"]["items"]
    shape = counts["items"]["properties"]
    assert shape["items"] == {"type": "integer", "description": "How many."}
    assert shape["names"]["type"] == "array" and shape["cards"]["type"] == "array"
    assert shape["units"]["anyOf"][0] == {"type": "integer"}


def unwrap_all(columns: dict[str, Any]) -> dict[str, str]:
    """A short name of the type each column schema describes."""
    found: dict[str, str] = {}
    for column, schema in columns.items():
        inner = unwrapped(schema)
        cls = inner.get("x-graph-cli", {}).get("class")
        if inner.get("type") == "object":
            found[column] = f"entity {cls}"
        elif "enum" in inner:
            found[column] = "enum"
        elif "format" in inner:
            found[column] = inner["format"]
        elif "pattern" in inner and cls is not None:
            found[column] = f"ref {cls}"
        elif "pattern" in inner:
            found[column] = "decimal"
        else:
            found[column] = inner["type"]
    return found


def test_the_null_of_an_optional_match_is_valid_and_a_wrong_cell_is_not(
    run_cli: CliRunner, root: Path
) -> None:
    tool = manifest(run_cli)["run_item_cards"]
    data = answers(run_cli, tool)
    rows = data["items"]
    assert [r["parent"] is None for r in rows].count(True) == 2  # two items are no part of another
    assert any(r["parent"] is not None for r in rows)
    assert validator(tool).is_valid(data)
    for column, bad in (
        ("quantity", "many"),
        ("condition", "broken"),
        ("acquired_on", "02.01.2026"),
        ("item", "Drill"),
        ("id", "a name"),
        ("flag", "yes"),
    ):
        row = {**rows[0], column: bad}
        assert not validator(tool).is_valid({**data, "items": [row]}), column
    wrong = {**rows[0], "item": {**rows[0]["item"], "attrs": {"quantity": "many"}}}
    assert not validator(tool).is_valid({**data, "items": [wrong]})


def test_a_call_answers_the_cells_run_does(run_cli: CliRunner, root: Path) -> None:
    tool = manifest(run_cli)["run_item_cards"]
    ran = app(run_cli, "run", "item_cards", env=ENV)
    assert ran.code == 0, ran.envelope
    assert answers(run_cli, tool)["items"] == ran.data["items"]
    first = ran.data["items"][0]
    assert first["ratio"] == "1.5" and first["flag"] is True and first["tag"] == "fixed"
    assert set(first["item"]) >= {"id", "alias", "type", "rev", "record_status", "attrs", "edges"}
    assert json.dumps(first["created_at"]).count("T") == 1


def test_the_describe_of_the_function_lists_the_same_types(run_cli: CliRunner, root: Path) -> None:
    described = app(run_cli, "describe", "function", "item_cards", env=ENV)
    assert described.code == 0, described.envelope
    types = {c["column"]: c for c in described.data["function"]["returns"]}
    assert (types["item"]["type"], types["item"]["class"], types["item"]["nullable"]) == (
        "entity",
        "inv:Item",
        False,
    )
    assert (types["parent"]["type"], types["parent"]["nullable"]) == ("entity", True)
    assert (types["condition"]["type"], types["condition"]["enum"]) == ("enum", "inv:Condition")
    assert types["ratio"]["type"] == "decimal" and types["ratio"]["nullable"] is False
    counts = app(run_cli, "describe", "function", "condition_counts", env=ENV)
    units = {c["column"]: c for c in counts.data["function"]["returns"]}
    assert units["units"]["type"] == "integer"  # declared
    assert (units["names"]["type"], units["names"]["many"]) == ("string", True)
    text = run_cli("graph", "describe", "function", "condition_counts", env=ENV)
    assert text.returncode == 0, text.stderr
    assert "  names: list of string (not null)  Their names." in text.stdout
    assert "  units: integer  Their units." in text.stdout
    assert "  cards: list of entity inv:Item (not null)  The items." in text.stdout


def test_decimals_are_shown_in_plain_notation_and_validate(run_cli: CliRunner, root: Path) -> None:
    tool = manifest(run_cli)["run_decimal_cells"]
    data = answers(run_cli, tool)
    (row,) = data["items"]
    assert row == {
        "thousand": "1000",
        "tiny": "0.0000001",
        "product": "0.0000001",
        "negative_zero": "0.0",
        "kept_zeros": "1.50",
        "huge": "1E+999999999",  # beyond the bound: not a text of a billion digits
        "sum": "0.3",
    }
    assert validator(tool).is_valid(data)
    ran = app(run_cli, "run", "decimal_cells", env=ENV)
    assert ran.data["items"] == [row]
