"""C: structured arguments over the API answer like their CLI form (contracts B14.22 G5, G6).

A client that is not ``graph`` names parameters by their canonical names and
passes objects, numbers and filter objects; the server turns them into the
argv a person would type, so the answer is the CLI form's. The request body
keeps float literals and refuses duplicate keys.
"""

import io
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.app import create_graph
from support.equivalence import normalized
from support.server import Reply, call, grant_server, new_token

from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

GRAPH = {"graph": "example_graph"}


@pytest.fixture
def served(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> tuple[ServerApp, str]:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", version="2.1.0")  # the version with a decimal
    grant_server(run_cli, tmp_path)
    token = new_token(run_cli, "ci")
    return ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO()), token


def run(app: ServerApp, token: str, path: str, args: dict[str, Any]) -> Reply:
    return call(app, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


def raw(app: ServerApp, token: str, path: str, body: str) -> Reply:
    """A body as written: ``json.dumps`` would turn ``1.10`` into ``1.1``."""
    return call(app, "POST", f"/v1/commands/{path}", token=token, raw=body.encode())


def same(structured: Reply, cli: Reply) -> None:
    assert structured.status == cli.status == 200, (structured.json, cli.json)
    assert normalized(structured.json) == normalized(cli.json)


def test_create_update_and_find_with_structured_args_only(
    served: tuple[ServerApp, str],
) -> None:
    app, token = served
    shelf = run(
        app,
        token,
        "create",
        {"type": "inv:Location", "set": {"name": "Shelf", "code": "S1"}, **GRAPH},
    )
    tools = run(app, token, "create", {"type": "inv:Category", "set": {"name": "Tools"}, **GRAPH})
    location = shelf.json["data"]["entities"][0]["id"]
    category = tools.json["data"]["entities"][0]["id"]
    item = (
        '{"args": {"type": "inv:Item", "set": {"label": "Drill", "quantity": 2, "weight": 1.10},'
        f' "edge": {{"inv:located_in": ["{location}"], "inv:in_category": ["{category}"]}},'
        ' "graph": "example_graph"%s}}'
    )
    cli_item = {
        "type": "inv:Item",
        "--set": ["label=Drill", "quantity=2", "weight=1.10"],
        "--edge": [f"inv:located_in={location}", f"inv:in_category={category}"],
        "--graph": "example_graph",
    }
    preview = raw(app, token, "create", item % ', "dry_run": true')
    same(preview, run(app, token, "create", {**cli_item, "--dry-run": True}))
    created = raw(app, token, "create", item % "")
    assert created.status == 200, created.json
    entity = created.json["data"]["entities"][0]["id"]
    stored = run(app, token, "get", {"ref": entity, **GRAPH}).json["data"]["entity"]["attrs"]
    assert stored["weight"] == "1.10", stored  # the literal as written, not 1.1

    update = {"ref": entity, "set_json": {"quantity": 3}, "dry_run": True, **GRAPH}
    cli_update = {"ref": entity, "--set-json": ["quantity=3"], "--dry-run": True, **GRAPH}
    same(run(app, token, "update", update), run(app, token, "update", cli_update))
    updated = run(app, token, "update", {**update, "dry_run": False})
    assert updated.status == 200, updated.json

    where = [
        {"field": "quantity", "op": "ge", "value": 3},
        {"field": "label", "op": "in", "value": ["Drill", "Saw"]},
        {"field": "note", "op": "exists", "value": False},
    ]
    cli_where = ["quantity:ge=3", "label:in=Drill,Saw", "note:exists=false"]
    found = run(app, token, "find", {"type": ["inv:Item"], "where": where, **GRAPH})
    same(found, run(app, token, "find", {"--type": ["inv:Item"], "--where": cli_where, **GRAPH}))
    assert [e["id"] for e in found.json["data"]["items"]] == [entity]


def test_the_body_is_parsed_strictly(served: tuple[ServerApp, str]) -> None:
    app, token = served
    twice = raw(app, token, "find", '{"args": {"limit": 1, "limit": 2}}')
    assert twice.status == 400 and twice.error["code"] == "usage.invalid_input"
    assert "duplicate key" in twice.error["message"]
    for body in ("[" * 200_000, '{"args": {"limit": ' + "9" * 5000 + "}}"):
        refused = raw(app, token, "find", body)  # a refusal, not the interpreter's error
        assert (refused.status, refused.error["code"]) == (400, "usage.invalid_input")
    both = run(app, token, "find", {"limit": 1, "--limit": 1, **GRAPH})
    assert both.status == 400 and both.error["code"] == "usage.invalid_argument"
    assert both.error["details"]["argument"] == "--limit"
    typed = run(app, token, "create", {"type": "inv:Location", "set": {"name": {"x": 1}}, **GRAPH})
    assert typed.status == 400 and "set_json" in typed.error["message"]
    # outside args a number with a fraction stays a number: a malformed facet, as before
    facet = raw(app, token, "whoami", '{"actor": {"agent": {"id": 1.5}}}')
    assert facet.status == 200, facet.json
    assert [w["code"] for w in facet.json["warnings"]] == ["actor.facet_malformed"]
