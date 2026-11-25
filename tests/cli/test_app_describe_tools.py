"""S: ``graph describe tools`` over the inventory graph with functions, actions and views
(tool manifest plan TM1, TM3, TM6): the manifest, its selection and errors, the text form, and that
the answers of real calls validate against the output schema of their tool."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from jsonschema import Draft202012Validator
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.read_limits import install

ENV = {"GRAPH_CLI_GRAPH": "example_graph", "GRAPH_CLI_SPILL_BYTES": "0"}
FIXTURE_ROOT = FIXTURES
PUBLISHED = {
    "functions": FIXTURE_ROOT / "functions" / "inventory" / "valid",
    "actions": FIXTURE_ROOT / "actions" / "inventory" / "valid",
    "views": FIXTURE_ROOT / "views" / "inventory" / "valid",
}
NAMES = [
    "run_items_by_condition",
    "run_items_in_location",
    "do_move_item",
    "do_retire_item",
    "view_item_catalog",
    "view_items_in_location",
    "view_location_overview",
]


def documents() -> dict[str, Any]:
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "inv:Location", "local": place, "attrs": attrs}
        for place, attrs in (
            ("shelf", {"name": "Shelf room", "code": "SH"}),
            ("cellar", {"name": "Cellar", "code": "CE"}),
        )
    ]
    for index in range(40):
        ops.append(
            {
                "op": "create",
                "type": "inv:Item",
                "local": f"item{index}",
                "attrs": {
                    "name": f"Item number {index:02d} of the stock",
                    "condition": "damaged" if index % 3 else "good",
                },
                "edges": [
                    {"rel": "inv:located_in", "target": "@shelf" if index % 2 else "@cellar"}
                ],
            }
        )
    return {"ops": ops}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    for kind, folder in PUBLISHED.items():
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    write_documents(root, "example_graph", [documents()])
    return root


def ok(run_cli: CliRunner, *args: str) -> Outcome:
    outcome = app(run_cli, *args, env=ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def tools(run_cli: CliRunner, *args: str) -> dict[str, dict[str, Any]]:
    manifest = ok(run_cli, "describe", "tools", *args).data
    return {t["name"]: t for t in manifest["tools"]}


def test_the_manifest_lists_every_tool_with_its_schemas(run_cli: CliRunner, root: Path) -> None:
    outcome = ok(run_cli, "describe", "tools")
    manifest = outcome.data
    assert manifest["schema"] == "graph_cli.model/1" and manifest["tools_format"] == 1
    assert manifest["graph"]["name"] == "example_graph"
    assert [t["name"] for t in manifest["tools"]] == NAMES
    for tool in manifest["tools"]:
        Draft202012Validator.check_schema(tool["input_schema"])
        Draft202012Validator.check_schema(tool["output_schema"])
    assert set(manifest["errors"]) == {c for t in manifest["tools"] for c in t["errors"]}


def test_a_filter_narrows_the_list_and_a_foreign_name_is_not_found(
    run_cli: CliRunner, root: Path
) -> None:
    assert list(tools(run_cli, "--kind", "action")) == ["do_move_item", "do_retire_item"]
    assert list(
        tools(run_cli, "--tool", "view_item_catalog", "--tool", "run_items_by_condition")
    ) == [
        "run_items_by_condition",
        "view_item_catalog",
    ]
    assert list(tools(run_cli, "--module", "inventory", "--kind", "function")) == [
        "run_items_by_condition",
        "run_items_in_location",
    ]
    missing = app(run_cli, "describe", "tools", "--tool", "run_items_by_conditon", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "tool.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "run_items_by_condition"
    assert missing.error["hint"] == "graph describe tools"
    unknown = app(run_cli, "describe", "tools", "--module", "no_such", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")


def test_the_text_form_has_a_line_per_tool(run_cli: CliRunner, root: Path) -> None:
    result = run_cli("graph", "describe", "tools", "--kind", "action", env=ENV)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split()[0] for line in lines] == ["do_move_item", "do_retire_item"]
    assert "action" in lines[0] and "Store an item in another location." in lines[0]


def test_a_graph_without_entries_has_a_manifest_without_tools(
    run_cli: CliRunner, root: Path
) -> None:
    create_graph(run_cli, "empty_graph", "inventory")
    outcome = ok_graph(run_cli, "empty_graph")
    assert outcome.data["tools"] == [] and outcome.data["errors"] == {}


def ok_graph(run_cli: CliRunner, graph: str) -> Outcome:
    outcome = app(run_cli, "describe", "tools", env={**ENV, "GRAPH_CLI_GRAPH": graph})
    assert outcome.code == 0, outcome.envelope
    return outcome


# ---- the answers of real calls validate against the output schema of their tool ---------------


def check(tool: dict[str, Any], answer: Any) -> None:
    errors = list(Draft202012Validator(tool["output_schema"]).iter_errors(answer))
    assert errors == [], (tool["name"], [e.message for e in errors])


def test_the_answer_of_a_function_validates_against_its_tool(
    run_cli: CliRunner, root: Path
) -> None:
    by_name = tools(run_cli)
    for args in (
        ("--param", "condition=damaged"),
        ("--param", "condition=new"),
        ("--param", "condition=damaged", "--limit", "1"),
    ):
        outcome = ok(run_cli, "run", "items_by_condition", *args)
        check(by_name["run_items_by_condition"], {**outcome.data, "page": outcome.envelope["page"]})
    shown = ok(run_cli, "run", "items_by_condition", "--param", "condition=damaged")
    assert shown.data["items"]
    assert set(shown.data["items"][0]) == set(
        by_name["run_items_by_condition"]["output_schema"]["properties"]["items"]["items"][
            "properties"
        ]
    )


def test_the_answer_of_an_action_validates_against_its_tool(run_cli: CliRunner, root: Path) -> None:
    by_name = tools(run_cli)
    items = ok(run_cli, "find", "--type", "inv:Item").data["items"]
    place = ok(run_cli, "find", "--type", "inv:Location").data["items"][0]["alias"]
    moved = ok(
        run_cli,
        *(
            "do",
            "move_item",
            "--param",
            f"item={items[0]['alias']}",
            "--param",
            f"location={place}",
        ),
        "--dry-run",
    )
    check(by_name["do_move_item"], moved.data)
    assert moved.data["dry_run"] is True
    retired = ok(
        run_cli,
        *("do", "retire_item", "--param", f"item={items[0]['alias']}", "--reason", "scrapped"),
    )
    check(by_name["do_retire_item"], retired.data)


def test_the_answer_of_a_view_validates_against_its_tool(run_cli: CliRunner, root: Path) -> None:
    by_name = tools(run_cli)
    place = ok(run_cli, "find", "--type", "inv:Location").data["items"][0]["alias"]
    entity = ok(run_cli, "view", place, "--name", "location_overview")
    check(by_name["view_location_overview"], entity.data)
    paged = ok(run_cli, "view", "--name", "item_catalog")
    check(by_name["view_item_catalog"], paged.data)
    limited = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "1024")
    check(by_name["view_item_catalog"], limited.data)


def test_a_view_page_with_a_cursor_validates_too(run_cli: CliRunner, root: Path) -> None:
    by_name = tools(run_cli)
    first = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "1024")
    assert first.data["more"] is True and first.data["next"]
    second = ok(run_cli, "view", "--name", "item_catalog", "--cursor", first.data["next"])
    check(by_name["view_item_catalog"], second.data)


# ---- what a caller who sees or may do less is given ----------------------------------------------

READS_ITEMS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [inv:Item]
    writes:
      inv:Item: {transition: true, link: true, unlink: true}
no_project: {"*": write}
"""
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
NO_EXPLORE = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""


def listed(run_cli: CliRunner, env: dict[str, str], *args: str) -> dict[str, Any]:
    outcome = app(run_cli, "describe", "tools", *args, env=env)
    assert outcome.code == 0, outcome.envelope
    data: dict[str, Any] = outcome.data
    return data


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_caller_with_a_read_limit_gets_a_manifest_without_what_it_may_not_see(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    install(run_cli, tmp_path / "policies", READS_ITEMS)
    manifest = listed(run_cli, ENV)
    assert [t["name"] for t in manifest["tools"]] == ["run_items_by_condition", "do_retire_item"]
    text = json.dumps(manifest)
    for word in (
        "Location",
        "Category",
        "located_in",
        "in_category",
        "move_item",
        "items_in_location",
    ):
        assert word not in text, word
    # what the listings say about the same caller
    assert [f["name"] for f in ok(run_cli, "describe", "functions").data["functions"]] == [
        "items_by_condition"
    ]


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_an_entry_the_caller_may_not_use_is_no_tool(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    full = listed(run_cli, EXPLORE)
    assert [t["kind"] for t in full["tools"]].count("action") == 2
    install(run_cli, tmp_path / "policies", NO_EXPLORE)
    manifest = listed(run_cli, EXPLORE)
    seen = {
        kind: app(run_cli, "describe", f"{kind}s", "--include-not-allowed", env=EXPLORE)
        for kind in ("function", "action", "view")
    }
    refused = {
        kind: [i["name"] for i in o.data[f"{kind}s"] if i.get("allowed") is False]
        for kind, o in seen.items()
    }
    assert refused["function"] and refused["action"]  # they exist, but not for this caller
    prefix = {"function": "run_", "action": "do_", "view": "view_"}
    tool_names = {t["name"] for t in manifest["tools"]}
    for kind, names in refused.items():
        for name in names:
            assert prefix[kind] + name not in tool_names, name
    assert not [t for t in manifest["tools"] if t["kind"] in ("function", "action")]
    # another caller of the same graph still has them
    assert len(listed(run_cli, ENV)["tools"]) == len(NAMES)


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_policy_that_demands_a_reason_makes_the_reason_of_an_action_required(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    before = tools(run_cli, "--kind", "action")
    assert "_reason" not in before["do_move_item"]["input_schema"]["required"]
    policy = READS_ITEMS.replace(
        "no_project", "obligations:\n  - {action: [write.unlink], require_reason: true}\nno_project"
    )
    install(run_cli, tmp_path / "policies", policy.replace("reads: [inv:Item]\n    ", ""))
    after = tools(run_cli, "--kind", "action")
    assert "_reason" in after["do_move_item"]["input_schema"]["required"]
    assert after["do_move_item"]["annotations"]["reason"] == "required"
    assert after["do_move_item"]["hash"] != before["do_move_item"]["hash"]
