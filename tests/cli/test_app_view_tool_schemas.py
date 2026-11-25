"""S: the answers of ``graph view`` and ``graph call view_*`` match the output schema of their tool
in ``describe tools`` (tool manifest plan TM6, step 4), over the inventory graph: entity views, the
pages of a set view, a trimmed answer, a ``rows`` section that gave an error, and the paging
contract of the tools. The schemas are closed like the test plugin closes them, so a key the view
answers without the schema naming it fails."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from jsonschema import Draft202012Validator
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.output_schemas import closed

ENV = {"GRAPH_CLI_GRAPH": "example_graph", "GRAPH_CLI_SPILL_BYTES": "0"}
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
# an entity view whose limit is the smallest there is, so the byte guard trims its answer
TIGHT = """\
schema: graph_cli.view/1
name: tight_location
summary: A location with its items, in the least room a view may have.
use_when: A view that is trimmed by its byte limit.
takes: inv:Location
max_bytes: 1024
sections:
  - kind: fields
    fields: all
  - kind: neighbors
    id: items
    relation: inv:located_in
    direction: in
    order: [name]
    fields: [name, quantity, status]
    max: 40
  - kind: relations
  - kind: rows
    id: contents
    function: items_in_location
    args: {location: $entity}
  - kind: affordances
"""


def documents() -> dict[str, Any]:
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "inv:Location", "local": place, "attrs": attrs}
        for place, attrs in (
            ("shelf", {"name": "Shelf room", "code": "SH"}),
            ("cellar", {"name": "Cellar", "code": "CE"}),
        )
    ]
    for index in range(30):
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
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    views = tmp_path / "views"
    views.mkdir()
    for source in VIEWS.glob("*.yaml"):
        (views / source.name).write_text(source.read_text())
    (views / "tight_location.yaml").write_text(TIGHT)
    for kind, folder in (("functions", FUNCTIONS), ("views", views)):
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    write_documents(root, "example_graph", [documents()])
    return root


def j(value: Any) -> str:
    return json.dumps(value)


def ok(run_cli: CliRunner, *args: str) -> Outcome:
    outcome = app(run_cli, *args, env=ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def call(run_cli: CliRunner, tool: str, **given: Any) -> Outcome:
    inputs = [x for key, value in given.items() for x in ("--input", f"{key}={j(value)}")]
    return app(run_cli, "call", tool, *inputs, env=ENV)


def manifest(run_cli: CliRunner) -> dict[str, Any]:
    return ok(run_cli, "describe", "tools").data


def schema_of(run_cli: CliRunner, view: str) -> Draft202012Validator:
    tools = {t["name"]: t for t in manifest(run_cli)["tools"]}
    return Draft202012Validator(closed(tools[f"view_{view}"]["output_schema"]))


def wrong(validator: Draft202012Validator, document: Any) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message[:300]}"
        for e in validator.iter_errors(document)
    ]


def location(run_cli: CliRunner, code: str) -> str:
    found = ok(run_cli, "find", "--type", "inv:Location").data["items"]
    return str(next(i["alias"] for i in found if i["attrs"]["code"] == code))


def test_an_entity_view_answers_what_its_tool_schema_says(run_cli: CliRunner, root: Path) -> None:
    check = schema_of(run_cli, "location_overview")
    for code in ("SH", "CE"):
        shelf = location(run_cli, code)
        called = call(run_cli, "view_location_overview", _entity=shelf)
        assert called.code == 0, called.envelope
        assert wrong(check, called.data) == []
        assert called.data == ok(run_cli, "view", shelf, "--name", "location_overview").data
    kinds = [s["kind"] for s in called.data["sections"]]
    assert kinds == ["fields", "neighbors", "rows"]
    contents = next(s for s in called.data["sections"] if s["kind"] == "rows")
    assert contents["function"]["name"] == "items_in_location" and contents["items"]


def test_the_rows_of_a_section_are_typed_like_the_function_of_the_same_tool(
    run_cli: CliRunner, root: Path
) -> None:
    tools = {t["name"]: t for t in manifest(run_cli)["tools"]}
    rows = next(
        v
        for v in tools["view_location_overview"]["output_schema"]["properties"]["sections"][
            "items"
        ]["anyOf"]
        if v["properties"]["kind"] == {"const": "rows"} and "columns" in v["properties"]
    )
    cells = rows["properties"]["items"]["items"]["properties"]
    function = tools["run_items_in_location"]["output_schema"]["properties"]["items"]["items"]
    assert set(cells) == set(function["properties"]) == {"item", "quantity"}
    # a function and the view over it are two tools of the same graph; their cells agree but for
    # the text that an entity view cuts (no patterns, formats or enum values there)
    for name, cell in cells.items():
        assert (
            cell.get("anyOf", [cell])[0]["type"] == function["properties"][name]["anyOf"][0]["type"]
        )


def test_a_trimmed_entity_view_matches_its_schema(run_cli: CliRunner, root: Path) -> None:
    check = schema_of(run_cli, "tight_location")
    shelf = location(run_cli, "SH")
    called = call(run_cli, "view_tight_location", _entity=shelf)
    assert called.code == 0, called.envelope
    assert called.data["cut"] is True and called.data["bytes"] <= 1024
    assert wrong(check, called.data) == []


def test_every_page_of_a_set_view_matches_its_schema_and_follows_the_contract(
    run_cli: CliRunner, root: Path
) -> None:
    check = schema_of(run_cli, "item_catalog")
    asked: dict[str, Any] = {"_page_bytes": 1500}
    seen: list[dict[str, Any]] = []
    while True:
        called = call(run_cli, "view_item_catalog", **asked)
        assert called.code == 0, called.envelope
        assert wrong(check, called.data) == []
        seen.append(called.data)
        if not called.data.get("more"):
            break
        assert called.data["more"] is True and called.data["next"]
        asked = {"_page_bytes": 1500, "_cursor": called.data["next"]}
    assert len(seen) >= 3 and "next" not in seen[-1] and "more" not in seen[-1]
    assert all("entity" not in page for page in seen)
    assert any("from" in s for page in seen for s in page["sections"])
    # the pages put together hold every row once
    rows = [i for page in seen for s in page["sections"] if s["kind"] == "list" for i in s["items"]]
    assert len(rows) == 32


def test_a_write_between_two_pages_restarts_the_paging(run_cli: CliRunner, root: Path) -> None:
    errors = manifest(run_cli)["errors"]
    assert errors["view.cursor_stale"]["on_error"] == "restart"
    first = call(run_cli, "view_item_catalog", _page_bytes=1500)
    assert first.data["more"] is True
    write_documents(
        root,
        "example_graph",
        [{"ops": [{"op": "create", "type": "inv:Category", "attrs": {"name": "Late addition"}}]}],
        seed=99,
    )
    stale = call(run_cli, "view_item_catalog", _page_bytes=1500, _cursor=first.data["next"])
    assert (stale.code, stale.error["code"]) == (1, "view.cursor_stale")
    again = call(run_cli, "view_item_catalog", _page_bytes=1500)
    assert again.code == 0 and wrong(schema_of(run_cli, "item_catalog"), again.data) == []


def test_a_set_view_with_a_parameter_matches_its_schema(run_cli: CliRunner, root: Path) -> None:
    check = schema_of(run_cli, "items_in_location")
    called = call(run_cli, "view_items_in_location", location=location(run_cli, "SH"))
    assert called.code == 0, called.envelope
    assert wrong(check, called.data) == []
    (section,) = called.data["sections"]
    assert section["kind"] == "rows" and section["count"] == 15


def test_a_rows_section_that_gave_an_error_matches_the_schema(
    run_cli: CliRunner, root: Path
) -> None:
    """Under ``--as-of`` the functions of ``rows`` sections do not run: the section is its error
    form, in an entity view as in a page of a set view."""
    seq = ok(run_cli, "get", location(run_cli, "SH")).envelope["graph"]["seq"]
    pinned = ["--as-of", f"tx:{seq}"]
    entity = ok(run_cli, "view", location(run_cli, "SH"), "--name", "location_overview", *pinned)
    failed = [s for s in entity.data["sections"] if s["kind"] == "rows"]
    assert [s["error"]["code"] for s in failed] == ["view.section_unavailable"]
    assert wrong(schema_of(run_cli, "location_overview"), entity.data) == []
    page = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "262144", *pinned)
    assert [s["error"]["code"] for s in page.data["sections"] if s["kind"] == "rows"] == [
        "view.section_unavailable"
    ]
    assert wrong(schema_of(run_cli, "item_catalog"), page.data) == []


def test_the_schema_refuses_what_the_view_does_not_declare(run_cli: CliRunner, root: Path) -> None:
    check = schema_of(run_cli, "location_overview")
    shelf = location(run_cli, "SH")
    answer = call(run_cli, "view_location_overview", _entity=shelf).data
    assert wrong(check, answer) == []
    extra = {**answer, "sections": [*answer["sections"], {"kind": "files", "id": "files"}]}
    assert wrong(check, extra) != []
    paged = {**answer, "next": "v1.1.0.0.00000000", "more": True}
    assert wrong(check, paged) != []  # an entity view has no pages
    other = {**answer, "view": {**answer["view"], "name": "item_catalog"}}
    assert wrong(check, other) != []
    broken = json.loads(json.dumps(answer))
    broken["sections"][1]["items"][0]["ref"] = 7
    assert wrong(check, broken) != []
