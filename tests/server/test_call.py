"""C: ``POST /v1/commands/call`` (tool manifest plan TM1, TM7): the typed input of a request is the
object of the tool's input schema and answers like the CLI form and like ``run``, ``do`` and
``view``; the idempotency key of the header and of ``args``; the status of each refusal."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init, write_documents
from support.app import create_graph
from support.equivalence import normalized
from support.fixtures import FIXTURES
from support.server import Reply, call, grant_server, new_token

from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

GRAPH = {"graph": "example_graph"}
PUBLISHED = {
    "functions": FIXTURES / "functions" / "inventory" / "valid",
    "actions": FIXTURES / "actions" / "inventory" / "valid",
    "views": FIXTURES / "views" / "inventory" / "valid",
}


@pytest.fixture
def served(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> tuple[ServerApp, str]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    for kind, folder in PUBLISHED.items():
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    ops: list[dict[str, Any]] = [
        {
            "op": "create",
            "type": "inv:Location",
            "local": "shelf",
            "attrs": {"name": "S", "code": "S"},
        },
        {
            "op": "create",
            "type": "inv:Location",
            "local": "cellar",
            "attrs": {"name": "C", "code": "C"},
        },
    ]
    for index in range(6):
        ops.append(
            {
                "op": "create",
                "type": "inv:Item",
                "local": f"item{index}",
                "attrs": {"name": f"Item {index}", "condition": "good" if index % 2 else "worn"},
                "edges": [{"rel": "inv:located_in", "target": "@shelf"}],
            }
        )
    write_documents(root, "example_graph", [{"ops": ops}])
    grant_server(run_cli, tmp_path)
    token = new_token(run_cli, "ci")
    return ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO()), token


def run(app: ServerApp, token: str, path: str, args: dict[str, Any], **headers: str) -> Reply:
    return call(
        app, "POST", f"/v1/commands/{path}", token=token, body={"args": args}, headers=headers
    )


def raw(app: ServerApp, token: str, body: str) -> Reply:
    """A body as written: ``json.dumps`` of a Python number would not keep ``2e0``."""
    return call(app, "POST", "/v1/commands/call", token=token, raw=body.encode())


def entity(app: ServerApp, token: str, cls: str, code: str | None = None) -> str:
    found = run(app, token, "find", {"type": [cls], **GRAPH}).json["data"]["items"]
    pick = next(i for i in found if code is None or i["attrs"].get("code") == code)
    return str(pick["alias"])


def test_the_input_object_answers_like_the_cli_form_and_like_run(
    served: tuple[ServerApp, str],
) -> None:
    app, token = served
    shelf = entity(app, token, "inv:Location", "S")
    typed = run(
        app,
        token,
        "call",
        {"tool": "run_items_in_location", "input": {"location": shelf, "_limit": 2}, **GRAPH},
    )
    assert typed.status == 200, typed.json
    cli = run(
        app,
        token,
        "call",
        {
            "tool": "run_items_in_location",
            "input": [f'location="{shelf}"', "_limit=2"],
            **GRAPH,
        },
    )
    assert normalized(typed.json) == normalized(cli.json)
    plain = run(
        app,
        token,
        "run",
        {"name": "items_in_location", "param": {"location": [shelf]}, "limit": 2, **GRAPH},
    )
    assert typed.json["data"] == {**plain.json["data"], "page": plain.json["page"]}
    assert typed.json["data"]["page"]["truncated"] is True and typed.json["page"] is None


def test_a_number_with_a_fraction_is_read_as_the_client_wrote_it(
    served: tuple[ServerApp, str],
) -> None:
    app, token = served
    shelf = entity(app, token, "inv:Location", "S")

    def asked(tool_input: str) -> Reply:
        body = '{"args": {"tool": "run_items_in_location", "graph": "example_graph", "input": %s}}'
        return raw(app, token, body % tool_input)

    page = f'{{"location": "{shelf}", "_limit": %s%s}}'
    whole = asked(page % ("2.0", ""))
    assert whole.status == 200 and whole.json["data"]["page"]["limit"] == 2
    exponent = asked(page % ("2e0", ""))
    assert exponent.status == 200 and exponent.json["data"]["page"]["limit"] == 2
    nulls = asked(page % ("null", ', "_offset": null'))
    assert nulls.status == 200 and nulls.json["data"]["page"]["limit"] == 50
    fraction = asked(page % ("2.5", ""))
    assert (fraction.status, fraction.error["code"]) == (400, "usage.invalid_argument")
    assert fraction.error["details"]["argument"] == "_limit"
    # a float is no reference either, whatever it looks like
    number = asked('{"location": 1.5}')
    assert (number.status, number.error["code"]) == (400, "function.parameter_invalid")


def test_the_input_is_checked_like_the_cli_form(served: tuple[ServerApp, str]) -> None:
    app, token = served
    for tool_input, status, code in (
        ({"condition": "good", "x": 1}, 400, "function.parameter_unknown"),
        ({"condition": "mint"}, 400, "function.parameter_invalid"),
        ({}, 400, "function.parameter_missing"),
        ({"condition": None}, 400, "function.parameter_missing"),
        ({"condition": "good", "_reason": "x"}, 400, "usage.invalid_argument"),
        ({"condition": "good", "_limit": 0}, 400, "usage.invalid_argument"),
        ({"condition": "good", "_limit": True}, 400, "usage.invalid_argument"),
        ({"condition": "good", "_limit": "2"}, 400, "usage.invalid_argument"),
    ):
        reply = run(
            app, token, "call", {"tool": "run_items_by_condition", "input": tool_input, **GRAPH}
        )
        assert (reply.status, reply.error["code"]) == (status, code), tool_input
    for bad in ("run_items_by_condition", [1], {"": 1}, {"a=b": 1}):
        reply = run(app, token, "call", {"tool": "run_items_by_condition", "input": bad, **GRAPH})
        assert reply.status == 400, bad
    missing = run(app, token, "call", {"input": {}, **GRAPH})
    assert missing.status == 400 and missing.error["code"] == "usage.missing_argument"
    absent = run(app, token, "call", {"tool": "run_items_by_conditon", "input": {}, **GRAPH})
    assert (absent.status, absent.error["code"]) == (404, "tool.not_found")
    assert absent.error["details"]["did_you_mean"][0] == "run_items_by_condition"


def test_the_key_of_the_header_and_of_args_is_the_key_of_a_do(
    served: tuple[ServerApp, str],
) -> None:
    app, token = served
    item = entity(app, token, "inv:Item")
    place = entity(app, token, "inv:Location", "C")
    move = {"tool": "do_move_item", "input": {"item": item, "location": place}, **GRAPH}
    first = run(app, token, "call", move, **{"Idempotency-Key": '"k-1"'})
    assert first.status == 200, first.json
    again = run(app, token, "call", {**move, "idempotency_key": "k-1"})
    assert again.json["data"]["idempotent_replay"] is True
    assert again.json["data"]["tx_id"] == first.json["data"]["tx_id"]
    by_do = run(
        app,
        token,
        "do",
        {"name": "move_item", "param": {"item": [item], "location": [place]}, **GRAPH},
        **{"Idempotency-Key": '"k-1"'},
    )
    assert by_do.json["data"]["idempotent_replay"] is True
    assert by_do.json["data"]["tx_id"] == first.json["data"]["tx_id"]
    items = run(app, token, "find", {"type": ["inv:Item"], **GRAPH}).json["data"]["items"]
    elsewhere = next(i["alias"] for i in items if i["alias"] != item)
    different = {**move, "input": {"item": elsewhere, "location": place}}
    reused = run(app, token, "call", different, **{"Idempotency-Key": '"k-1"'})
    assert (reused.status, reused.error["code"]) == (409, "conflict.idempotency_key_reuse")
    both = run(
        app, token, "call", {**move, "idempotency_key": "k-2"}, **{"Idempotency-Key": '"k-3"'}
    )
    assert (both.status, both.error["code"]) == (400, "usage.invalid_argument")


def test_a_read_takes_no_key_in_the_header_or_in_args(served: tuple[ServerApp, str]) -> None:
    app, token = served
    item = entity(app, token, "inv:Item")
    place = entity(app, token, "inv:Location", "C")
    for tool, tool_input in (
        ("run_items_by_condition", {"condition": "good"}),
        ("view_item_catalog", {}),
        ("do_move_item", {"item": item, "location": place, "_dry_run": True}),
    ):
        body = {"tool": tool, "input": tool_input, **GRAPH}
        header = run(app, token, "call", body, **{"Idempotency-Key": '"k-9"'})
        assert (header.status, header.error["code"]) == (400, "usage.invalid_argument"), tool
        option = run(app, token, "call", {**body, "idempotency_key": "k-9"})
        assert (option.status, option.error["code"]) == (400, "usage.invalid_argument"), tool
    # the same header on the command that takes a key anyway, and a command without one, as before
    assert (
        run(
            app,
            token,
            "run",
            {"name": "items_by_condition", "param": {"condition": ["good"]}, **GRAPH},
            **{"Idempotency-Key": '"k-9"'},
        ).status
        == 400
    )
    written = run(
        app,
        token,
        "call",
        {"tool": "do_move_item", "input": {"item": item, "location": place}, **GRAPH},
        **{"Idempotency-Key": '"k-9"'},
    )
    assert written.status == 200, written.json


def test_each_refusal_has_its_status(served: tuple[ServerApp, str]) -> None:
    app, token = served
    manifest = run(app, token, "describe/tools", GRAPH).json["data"]["tools"]
    hashes = {t["name"]: t["hash"] for t in manifest}
    shelf = entity(app, token, "inv:Location", "S")
    body = {"tool": "run_items_in_location", "input": {"location": shelf}, **GRAPH}
    current = run(app, token, "call", {**body, "expect_hash": hashes["run_items_in_location"]})
    assert current.status == 200, current.json
    stale = run(app, token, "call", {**body, "expect_hash": "sha256:" + "0" * 64})
    assert (stale.status, stale.error["code"]) == (409, "tool.hash_mismatch")
    assert stale.error["details"]["actual"] == hashes["run_items_in_location"]
    assert stale.error["retryable"] is False
    wrong = run(app, token, "call", {**body, "input": {"location": entity(app, token, "inv:Item")}})
    assert (wrong.status, wrong.error["code"]) == (
        409,
        "function.parameter_type",
    ) or wrong.status in (400, 409, 422)
    assert wrong.error["code"] == "function.parameter_type"


def test_the_call_is_in_the_inventory_and_the_api_document(served: tuple[ServerApp, str]) -> None:
    app, token = served
    inventory = call(app, "GET", "/v1/inventory", token=token).json
    (command,) = [c for c in inventory["commands"] if c["path"] == ["call"]]
    assert command["kind"] == "write" and command["skill_table"] is False
    options = {o["name"]: o for o in command["options"]}
    assert options["--input"]["pairs"] == "map" and options["--input"]["type"] == "json_assignment"
    assert {"--expect-hash", "--idempotency-key", "--graph"} <= set(options)
    assert command["args"][0]["name"] == "tool"
    assert {"tool.not_found", "tool.hash_mismatch", "remote.outcome_unknown"} <= set(
        command["errors"]
    )
    document = call(app, "GET", "/v1/openapi.json", token=token).json
    assert "/v1/commands/call" in document["paths"]
    assert json.dumps(document).count("tool.hash_mismatch") >= 1
