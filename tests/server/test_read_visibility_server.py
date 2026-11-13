"""C: type read limits through the server in-process (type read limits plan V1, V5, leak item 10).

The role ``reporting`` reads notes and scans of ``example_graph`` and nothing else, ``auditor``
reads everything. Token A holds the first, token B both, token C the second. A hidden stored file
is ``entity.not_found`` on every route before any message about its type, and the same as an id
that is not there, including the download route and its conditional requests.
"""

import io
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init, write_documents
from support.app import app, create_graph
from support.files import write_pdf
from support.policies import write_policy
from support.server import call, new_token

from graph_cli.kernel.canonical import hash_bytes
from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

NOT_THERE = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: notes and scans only"}
  auditor: {description: "Auditors: everything"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [nas:Note, nas:Scan]
  - entrypoint: server
    principal: {role: auditor}
    graphs: [example_graph]
    level: read
scopes:
  example_project: {example_graph: read}
no_project: {"*": none}
"""
BUILD = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t", "attrs": {"label": "A topic"}},
        {
            "op": "create",
            "type": "nas:Note",
            "local": "n",
            "attrs": {"title": "A note", "body": "text", "kind": "summary"},
            "edges": [{"rel": "nas:about", "target": "@t"}],
        },
    ]
}
GRAPH = {"--graph": "example_graph"}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    write_documents(root, "example_graph", [BUILD])
    contents = {name: write_pdf(tmp_path / f"{name}.pdf", name) for name in ("plain", "scan")}
    ids: dict[str, str] = {}
    for name, extra in (("plain", ()), ("scan", ("--type", "nas:Scan"))):
        added = app(
            run_cli,
            *("file", "add", str(contents[name]), *extra),
            env={"GRAPH_CLI_GRAPH": "example_graph"},
        )
        assert added.code == 0, added.envelope
        ids[name] = added.data["entity"]["id"]
    found = app(run_cli, "find", "--limit", "100", env={"GRAPH_CLI_GRAPH": "example_graph"})
    for item in found.data["items"]:
        if item["type"] in ("nas:Topic", "nas:Note"):
            ids[item["type"].split(":")[1].lower()] = item["id"]
    applied = admin(
        run_cli,
        *("policy", "apply", str(write_policy(tmp_path / "policies", "installation.yaml", POLICY))),
        *("--reason", "read limits"),
    )
    assert applied.code == 0, applied.envelope
    tokens = {
        "a": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reporting"),
        "b": new_token(
            *(run_cli, "app_b", "--project", "example_project"),
            *("--role", "reporting", "--role", "auditor"),
        ),
        "c": new_token(run_cli, "app_c", "--project", "example_project", "--role", "auditor"),
    }
    return {
        "env": isolated_env,
        "tokens": tokens,
        "ids": ids,
        "bytes": {name: path.read_bytes() for name, path in contents.items()},
    }


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> ServerApp:
    return ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )


def command(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


def download(server: ServerApp, token: str, graph: str, ref: str, **headers: str) -> Any:
    return call(server, "GET", f"/v1/graphs/{graph}/files/{ref}", token=token, headers=headers)


def same_but_for(hidden: Any, missing: Any, swap: tuple[str, str]) -> None:
    """Two replies are the same but for the name that was asked: status, headers and body."""
    assert hidden.status == missing.status
    keep = ("Cache-Control", "Content-Type", "Graph-Cli-Exit-Code")
    assert {k: v for k, v in hidden.headers.items() if k in keep} == {
        k: v for k, v in missing.headers.items() if k in keep
    }
    assert hidden.body.decode().replace(*swap) == missing.body.decode()
    assert "ETag" not in hidden.headers


def test_a_hidden_file_cannot_be_downloaded_and_is_not_found_as_a_missing_one(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    ids, tokens, content = installation["ids"], installation["tokens"], installation["bytes"]
    plain, scan = ids["plain"], ids["scan"]
    # the visible scan comes down, with its validators
    first = download(served, tokens["a"], "example_graph", scan)
    assert first.status == 200 and first.body == content["scan"]
    tag = f'"{hash_bytes(content["scan"])}"'
    assert first.headers["ETag"] == tag
    assert download(served, tokens["a"], "example_graph", scan, If_None_Match=tag).status == 304
    # the plain file is hidden from A: not found, as an id that is not there, whatever is asked
    missing = download(served, tokens["a"], "example_graph", NOT_THERE)
    assert missing.status == 404 and missing.error["code"] == "entity.not_found"
    hidden = download(served, tokens["a"], "example_graph", plain)
    same_but_for(hidden, missing, (plain, NOT_THERE))
    sealed = f'"{hash_bytes(content["plain"])}"'  # its hash would answer 304 to a reader
    conditional = download(served, tokens["a"], "example_graph", plain, If_None_Match=sealed)
    same_but_for(conditional, missing, (plain, NOT_THERE))
    suffix = plain.replace("-", "")[-10:]
    unknown = download(served, tokens["a"], "example_graph", "0123456789")
    by_suffix = download(served, tokens["a"], "example_graph", suffix)
    same_but_for(by_suffix, unknown, (suffix, "0123456789"))
    assert content["plain"] not in hidden.body
    # a reader who sees both types, whichever role gives it, gets the bytes
    for name in ("b", "c"):
        full = download(served, tokens[name], "example_graph", plain)
        assert full.status == 200 and full.body == content["plain"], name
        assert download(served, tokens[name], "example_graph", scan).body == content["scan"]
    # other entities of hidden types are as missing on this route as on the command
    topic = download(served, tokens["a"], "example_graph", ids["topic"])
    same_but_for(topic, missing, (ids["topic"], NOT_THERE))


def test_commands_of_a_limited_token_see_the_graph_as_if_the_rest_did_not_exist(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    ids, tokens = installation["ids"], installation["tokens"]
    a, b = tokens["a"], tokens["b"]
    for path in ("get", "file/path", "history", "neighbors"):
        entity = ids["plain" if "file" in path else "topic"]
        hidden = command(served, path, a, ref=entity, **GRAPH)
        missing = command(served, path, a, ref=NOT_THERE, **GRAPH)
        assert hidden.status == missing.status == 404, path
        assert hidden.error["code"] == "entity.not_found", path
        same_but_for(hidden, missing, (entity, NOT_THERE))
    # what the note shows: no edge to the topic, for A; B (also an auditor) sees it
    shown_to_a = command(served, "get", a, ref=ids["note"], **GRAPH)
    shown_to_b = command(served, "get", b, ref=ids["note"], **GRAPH)
    assert shown_to_a.json["data"]["entity"]["edges"] == []
    assert [e["rel"] for e in shown_to_b.json["data"]["entity"]["edges"]] == ["nas:about"]
    assert ids["topic"] not in shown_to_a.body.decode()
    found = command(served, "find", a, **{"--limit": 100}, **GRAPH)
    assert sorted(i["type"] for i in found.json["data"]["items"]) == ["nas:Note", "nas:Scan"]
    assert found.json["page"]["total"] == 2
    everything = command(served, "find", b, **{"--limit": 100}, **GRAPH)
    assert everything.json["page"]["total"] == 4
    types = command(served, "describe/types", a, **GRAPH)
    assert [t["curie"] for t in types.json["data"]["types"]] == ["nas:Note", "nas:Scan"]
    query = command(served, "query", a, text="MATCH (n) RETURN count(n) AS c", **GRAPH)
    assert query.json["data"]["items"] == [{"c": 2}]
    assert command(served, "query", b, text="MATCH (n) RETURN count(n) AS c", **GRAPH).json["data"][
        "items"
    ] == [{"c": 4}]
