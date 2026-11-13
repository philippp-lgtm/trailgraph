"""C: ``whoami`` and ``policy explain`` of tokens whose roles carry a read allow list, through the
server in-process (type read limits plan V8).

The role ``reporting`` reads notes and scans of ``example_graph`` and may create notes, the role
``auditor`` reads everything. Token A holds the first, token B both, token C the second. ``whoami``
lists the types A sees and they are the types its reads reach; neither it nor the lists of limits
and writes name another type. B and C keep the answer they had without any read limit.
``policy explain`` says the same, from the roles alone, and reflects a graph layer that takes a
type away.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init, write_documents
from support.app import create_graph
from support.policies import write_policy
from support.server import call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

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
    writes:
      nas:Note: {create: [title, body, kind]}
  - entrypoint: server
    principal: {role: auditor}
    graphs: [example_graph]
    level: read
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
# The graph layer takes scans away from everyone, and limits the rest to what the roles say
GRAPH_LAYER = """\
schema: graph_cli.policy/1
id: graph
types:
  nas:Scan: none
  nas:Book: read
  "provenance:*": read
"""
CONCRETE = (
    "files:File",
    "nas:Book",
    "nas:Note",
    "nas:Scan",
    "nas:Topic",
    "nas:WebPage",
    "provenance:Claim",
)
EVERYTHING = list(CONCRETE)
BUILD = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t", "attrs": {"label": "A topic"}},
        {"op": "create", "type": "nas:Book", "local": "b", "attrs": {"title": "A book"}},
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
    return {"env": isolated_env, "tokens": tokens, "work": tmp_path / "policies"}


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> ServerApp:
    return ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )


def command(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


def entry(server: ServerApp, token: str) -> dict[str, Any]:
    seen = command(server, "whoami", token)
    assert seen.status == 200, seen.json
    (graph,) = seen.json["data"]["permissions"]["graphs"]
    assert isinstance(graph, dict)
    return graph


def reached(server: ServerApp, token: str) -> list[str]:
    """The concrete types the token's reads reach: ``describe type`` and ``find --type`` agree."""
    found = []
    for type_curie in CONCRETE:
        described = command(server, "describe/type", token, type=type_curie, **GRAPH)
        listed = command(server, "find", token, **{"--type": [type_curie]}, **GRAPH)
        assert described.status == listed.status, (type_curie, described.json, listed.json)
        if described.status == 200:
            found.append(type_curie)
        else:
            assert described.error["code"] == listed.error["code"] == "term.unknown"
    return found


def explain(*args: str, run_cli: CliRunner) -> dict[str, Any]:
    out = admin(run_cli, "policy", "explain", "--graph", "example_graph", *args)
    assert out.code == 0, out.envelope
    return out.data  # type: ignore[no-any-return]


def test_whoami_over_the_server_lists_the_types_of_a_limited_token(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    tokens = installation["tokens"]
    limited = entry(served, tokens["a"])
    assert limited["reads"] == ["nas:Note", "nas:Scan"]
    assert reached(served, tokens["a"]) == limited["reads"]
    assert limited["writes"] == {"nas:Note": {"create": ["body", "kind", "title"]}}
    # nothing in the answer names a type the token does not see
    text = json.dumps(limited)
    assert [t for t in CONCRETE if t not in limited["reads"] and t in text] == []
    assert "nas:about" not in text
    # tokens that see every type have no reads key: B keeps the writes of its role, C is
    # answered as before
    both = entry(served, tokens["b"])
    assert "reads" not in both and both["writes"] == limited["writes"]
    assert sorted(entry(served, tokens["c"])) == [
        "graph",
        "level",
        "obligations",
        "types",
        "withheld",
    ]
    for name in ("b", "c"):
        assert reached(served, tokens[name]) == EVERYTHING, name


def test_whoami_follows_a_graph_layer_that_takes_a_type_away(
    served: ServerApp, installation: dict[str, Any], run_cli: CliRunner
) -> None:
    applied = admin(
        run_cli,
        *("policy", "apply", str(write_policy(installation["work"], "graph.yaml", GRAPH_LAYER))),
        *("--graph", "example_graph", "--reason", "limits"),
    )
    assert applied.code == 0, applied.envelope
    tokens = installation["tokens"]
    # A: scans are gone for everyone, so it sees notes only; none of the limits is its to see
    limited = entry(served, tokens["a"])
    assert limited["reads"] == ["nas:Note"]
    assert limited["types"] == {}
    assert reached(served, tokens["a"]) == ["nas:Note"]
    # B and C: scans are gone too, so they are limited now; the limits they see are of types
    # they see
    expected = [t for t in CONCRETE if t != "nas:Scan"]
    for name in ("b", "c"):
        graph = entry(served, tokens[name])
        assert graph["reads"] == expected, name
        assert graph["types"] == {"nas:Book": "read", "provenance:*": "read"}, name
        assert reached(served, tokens[name]) == expected, name
        assert "nas:Scan" not in json.dumps(graph), name


def test_policy_explain_for_roles_shows_the_same_types(
    installation: dict[str, Any], run_cli: CliRunner
) -> None:
    project = ("--project", "example_project")
    reporting = explain(*project, "--role", "reporting", run_cli=run_cli)
    assert reporting["reads"] == ["nas:Note", "nas:Scan"]
    assert reporting["writes"] == {"nas:Note": {"create": ["body", "kind", "title"]}}
    both = explain(*project, "--role", "reporting", "--role", "auditor", run_cli=run_cli)
    auditor = explain(*project, "--role", "auditor", run_cli=run_cli)
    assert "reads" not in both and "reads" not in auditor
    # the token explains as its roles do
    by_token = explain("--token", "app_a", run_cli=run_cli)
    assert by_token["reads"] == reporting["reads"]
    # one type: the decision the reads of the token meet
    for type_curie, effect in (("nas:Note", "allow"), ("nas:Topic", "deny")):
        one = explain(
            *project,
            *("--role", "reporting", "--action", "read.get", "--type", type_curie),
            run_cli=run_cli,
        )
        (decision,) = one["decisions"]
        assert decision["effect"] == effect, (type_curie, decision)
        assert "reads" not in one
    assert admin(run_cli, "audit", "verify").code == 0
