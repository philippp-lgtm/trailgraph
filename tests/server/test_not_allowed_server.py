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
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}
  - {entrypoint: server, principal: {role: writer}, graphs: [example_graph], level: write}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
GRAPH = {"--graph": "example_graph"}
ACTIONS = ["move_item", "retire_item"]


@pytest.fixture
def served(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Any:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    actions = FIXTURES / "actions" / "inventory" / "valid"
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(actions))
    assert published.code == 0, published.envelope
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "test")
    assert applied.code == 0, applied.envelope
    tokens = {
        name: new_token(run_cli, f"app_{name}", "--project", "example_project", "--role", name)
        for name in ("reader", "writer")
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


def test_do_of_a_not_allowed_action_is_a_403_that_names_the_right(served: Any) -> None:
    server, tokens = served
    args = {**GRAPH, "name": "retire_item", "--param": ["item=x"], "--reason": "test"}
    denied = command(server, "do", tokens["reader"], **args)
    assert denied.status == 403 and denied.error["code"] == "access.denied", denied.json
    assert denied.error["details"]["action"].startswith("write.")
    unknown = command(server, "do", tokens["reader"], **{**args, "name": "retire_itemm"})
    assert unknown.status == 404 and unknown.error["code"] == "action.not_found"
