"""C: functions, actions and views a token sees but may not use, through the server in-process
(graph start plan S4, step 2): the generic command route carries ``not_allowed``, ``allowed`` and
``missing`` unchanged, and ``do`` of such an action is a 403 ``access.denied`` that names the right.

Token ``reader`` holds a read grant without a write right, so every action is not allowed to it;
token ``writer`` holds a write grant.
"""

import io
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.fixtures import FIXTURES
from support.policies import write_policy
from support.server import call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reads example_graph"}
  writer: {description: "Writes example_graph"}
  blind: {description: "Reads example_graph without the curated catalogs"}
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}
  - {entrypoint: server, principal: {role: writer}, graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {role: blind}, graphs: [example_graph], level: read}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
deny:
  - {id: blind_no_catalogs, role: [blind], action: ["read.function", "read.find"]}
"""
GRAPH = {"--graph": "example_graph"}
ACTIONS = ["move_item", "retire_item"]


@pytest.fixture
def served(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Any:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    for kind in ("functions", "actions", "views"):
        folder = FIXTURES / kind / "inventory" / "valid"
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "test")
    assert applied.code == 0, applied.envelope
    tokens = {
        name: new_token(run_cli, f"app_{name}", "--project", "example_project", "--role", name)
        for name in ("reader", "writer", "blind")
    }
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)
    return server, tokens


def command(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


def test_a_token_that_may_not_write_gets_the_actions_marked_through_the_server(
    served: Any,
) -> None:
    server, tokens = served
    writer = command(server, "describe/actions", tokens["writer"], **GRAPH).json["data"]
    assert [a["name"] for a in writer["actions"]] == ACTIONS and "not_allowed" not in writer
    reader = command(server, "describe/actions", tokens["reader"], **GRAPH).json["data"]
    assert reader["actions"] == [] and reader["not_allowed"] == len(ACTIONS)
    both = command(
        server, "describe/actions", tokens["reader"], **{"--include-not-allowed": True}, **GRAPH
    ).json["data"]["actions"]
    assert [a["name"] for a in both] == ACTIONS
    assert all(a["allowed"] is False and a["missing"] for a in both)
    shown = command(server, "describe/action", tokens["reader"], name="retire_item", **GRAPH)
    assert shown.status == 200 and shown.json["data"]["action"]["allowed"] is False
    hits = command(server, "describe/search", tokens["reader"], text="retire", **GRAPH)
    assert [
        (h["curie"], h["allowed"]) for h in hits.json["data"]["items"] if h["kind"] == "action"
    ] == [("retire_item", False)]


WITHHELD = {"query", "returns", "ops", "preconditions", "sections", "examples", "touches", "needs"}
WITHHELD |= {"hash", "call", "featured", "max_bytes", "page_bytes"}


def test_the_definition_of_a_not_allowed_entry_stays_withheld_through_the_server(
    served: Any,
) -> None:
    """The keys that carry the maintainer's definition are absent over the server as they are
    locally (the answer is a whitelist: name, summary, use_when, signature, status and the
    marks), for an action, a function and a view alike."""
    server, tokens = served
    for path, key, name in (
        ("describe/action", "action", "retire_item"),
        ("describe/function", "function", "items_in_location"),
        ("describe/view", "view", "items_in_location"),
    ):
        token = tokens["reader"] if key == "action" else tokens["blind"]
        shown = command(server, path, token, name=name, **GRAPH)
        assert shown.status == 200, shown.json
        item = shown.json["data"][key]
        assert item["allowed"] is False and item["missing"], (key, item)
        assert not WITHHELD & set(item), (key, sorted(WITHHELD & set(item)))
        allowed = command(server, path, tokens["writer"], name=name, **GRAPH).json["data"][key]
        assert "allowed" not in allowed and {"signature", "name"} <= set(allowed)


def test_do_of_a_not_allowed_action_is_a_403_that_names_the_right(served: Any) -> None:
    server, tokens = served
    args = {**GRAPH, "name": "retire_item", "--param": ["item=x"], "--reason": "test"}
    denied = command(server, "do", tokens["reader"], **args)
    assert denied.status == 403 and denied.error["code"] == "access.denied", denied.json
    assert denied.error["details"]["action"].startswith("write.")
    unknown = command(server, "do", tokens["reader"], **{**args, "name": "retire_itemm"})
    assert unknown.status == 404 and unknown.error["code"] == "action.not_found"
