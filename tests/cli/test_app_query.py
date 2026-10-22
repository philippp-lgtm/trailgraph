"""S: graph query on seeded graphs of both fixtures (plan cypher_query.md 2, 4, 5, 6)."""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.activity import short_dir
from support.admin import admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES, TOY_ONTOLOGIES
from support.policies import write_policy
from support.roles import Roles, load_roles

from graph_cli.catalog.activity import ActivityListener
from graph_cli.kernel.changeset import read_documents


@pytest.fixture(params=TOY_ONTOLOGIES)
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], ontology: str) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", ontology)
    seeds = read_documents((FIXTURES / "seeds" / ontology / "changes.jsonl").read_bytes())
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    return root


@pytest.fixture
def roles(ontology: str) -> Roles:
    return load_roles(ontology)


ENV = {"GRAPH_CLI_GRAPH": "example_graph"}


def label(curie: str) -> str:
    return f"`{curie}`"


def test_rows_match_find(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    found = app(run_cli, "find", "--type", cls, "--limit", "500", env=ENV)
    outcome = app(run_cli, "query", f"MATCH (n:{label(cls)}) RETURN n, n.id AS id", env=ENV)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["columns"] == ["n", "id"]
    assert [i["id"] for i in outcome.data["items"]] == [i["id"] for i in found.data["items"]]
    assert outcome.data["items"][0]["n"] == found.data["items"][0]
    assert outcome.envelope["page"]["total"] == found.envelope["page"]["total"]


def test_two_hops_and_counts_in_one_call(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls, rel = roles.term("lifecycle_type"), roles.term("required_relation")
    text = (
        f"MATCH (n:{label(cls)})-[:{label(rel)}]->(t) "
        "RETURN t.id AS target, count(n) AS sources ORDER BY sources DESC, target"
    )
    outcome = app(run_cli, "query", text, env=ENV)
    assert outcome.code == 0, outcome.envelope
    items = outcome.data["items"]
    assert items and all(isinstance(i["sources"], int) and i["sources"] >= 1 for i in items)
    counts = [i["sources"] for i in items]
    assert counts == sorted(counts, reverse=True)
    first = items[0]["target"]
    via_find = app(run_cli, "find", "--type", cls, "--rel", f"{rel}={first}", env=ENV)
    assert via_find.envelope["page"]["total"] == items[0]["sources"]


def test_parameters_file_and_stdin(
    run_cli: CliRunner, root: Path, roles: Roles, tmp_path: Path
) -> None:
    cls = roles.term("lifecycle_type")
    entity = app(run_cli, "find", "--type", cls, "--limit", "1", env=ENV).data["items"][0]
    text = "MATCH (n) WHERE n.id = $id RETURN n.alias AS alias, $n + 1 AS next"
    by_param = app(
        run_cli, "query", text, "--param", f"id={entity['id']}", "--param", "n=1", env=ENV
    )
    assert by_param.code == 0, by_param.envelope
    assert by_param.data["items"] == [{"alias": entity["alias"], "next": 2}]
    query_file = tmp_path / "q.cypher"
    query_file.write_text(text + "\n")
    args = ("--param", f"id={entity['id']}", "--param", "n=41")
    from_file = app(run_cli, "query", "--file", str(query_file), *args, env=ENV)
    assert from_file.data["items"] == [{"alias": entity["alias"], "next": 42}]
    from_stdin = run_cli("graph", "query", "-", *args, "--json", env=ENV, input=text)
    assert from_stdin.returncode == 0, from_stdin.stderr
    assert json.loads(from_stdin.stdout)["data"]["items"][0]["next"] == 42
    missing = app(run_cli, "query", text, env=ENV)
    assert (missing.code, missing.error["code"]) == (2, "query.parameter_missing")
    assert missing.error["hint"] == "graph query --param id=<value>"
    both = app(run_cli, "query", text, "--file", str(query_file), env=ENV)
    assert (both.code, both.error["code"]) == (2, "usage.invalid_argument")


def test_paging_with_a_next_command(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    text = f"MATCH (n:{label(cls)}) RETURN n.id AS id"
    first = app(run_cli, "query", text, "--limit", "2", env=ENV)
    page = first.envelope["page"]
    assert page["shown"] == 2 and page["truncated"] and page["next_offset"] == 2
    (hint,) = first.envelope["hints"]
    assert hint["command"].endswith("--limit 2 --offset 2")
    nxt = app(run_cli, "query", text, "--limit", "2", "--offset", "2", env=ENV)
    assert nxt.data["items"] and nxt.data["items"][0] != first.data["items"][0]
    assert app(run_cli, "query", text, "--limit", "501", env=ENV).code == 2


def test_explain_runs_nothing(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    local = cls.partition(":")[2]
    outcome = app(run_cli, "query", f"MATCH (n:{local}) RETURN n.rev", "--explain", env=ENV)
    assert outcome.code == 0, outcome.envelope
    plan = outcome.data["plan"]
    assert plan["clauses"][0]["text"] == f"MATCH (n:{label(cls)})"
    assert plan["columns"] == ["n.rev"]
    assert plan["fields"] == [
        {"variable": "n", "field": "rev", "datatypes": ["integer"], "many": False}
    ]
    assert "page" not in outcome.envelope or outcome.envelope["page"] is None


def test_inactive_entities_only_on_request(run_cli: CliRunner, root: Path) -> None:
    text = "MATCH (n) WHERE n.record_status <> 'active' RETURN count(*) AS n"
    hidden = app(run_cli, "query", text, env=ENV)
    assert hidden.data["items"] == [{"n": 0}]
    shown = app(run_cli, "query", text, "--include-inactive", env=ENV)
    assert shown.data["items"][0]["n"] >= 1


def test_errors(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    syntax = app(run_cli, "query", "MATCH (n RETURN n", env=ENV)
    assert (syntax.code, syntax.error["code"]) == (2, "query.syntax")
    assert syntax.error["details"]["column"] == 10
    write = app(run_cli, "query", "MATCH (n) DELETE n", env=ENV)
    assert (write.code, write.error["code"]) == (1, "query.unsupported")
    assert write.error["hint"] == "graph apply -"
    unknown = app(run_cli, "query", "MATCH (n:NoSuchType) RETURN n", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    unbound = app(run_cli, "query", "MATCH (n) RETURN m", env=ENV)
    assert (unbound.code, unbound.error["code"]) == (2, "query.unbound_variable")
    value = app(run_cli, "query", "RETURN 1 / 0", env=ENV)
    assert (value.code, value.error["code"]) == (1, "query.invalid_value")
    as_of = app(run_cli, "query", "RETURN 1", "--as-of", "tx:3", env=ENV)
    assert (as_of.code, as_of.error["code"]) == (1, "temporal.as_of_unsupported")
    nothing = app(run_cli, "query", env=ENV)
    assert (nothing.code, nothing.error["code"]) == (2, "usage.invalid_argument")


def test_text_output_is_a_table(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    text = f"MATCH (n:{label(cls)}) RETURN n, n.rev AS rev LIMIT 2"
    result = run_cli("graph", "query", text, env=ENV)
    assert result.returncode == 0, result.stderr
    header, first, *_ = result.stdout.splitlines()
    assert header.split() == ["n", "rev"]
    assert cls in first and first.split()[-1].isdigit()


NO_EXPLORE_QUERY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_query, agent_type: [explore], action: ["read.query"]}
"""


def test_a_deny_on_read_query_keeps_find(
    run_cli: CliRunner, root: Path, roles: Roles, tmp_path: Path
) -> None:
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE_QUERY)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    assert app(run_cli, "query", "RETURN 1", env=ENV).code == 0
    explore = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
    denied = app(run_cli, "query", "RETURN 1", env=explore)
    assert (denied.code, denied.error["code"]) == (3, "access.denied")
    assert denied.error["details"]["action"] == "read.query"
    assert app(run_cli, "find", "--type", roles.term("lifecycle_type"), env=explore).code == 0
    who = app(run_cli, "whoami", env=explore).data["permissions"]["graphs"]
    assert [g["withheld"] for g in who] == [["read.query"]]
    unseen = app(run_cli, "query", "RETURN 1", "--graph", "no_such_graph")
    assert unseen.error["code"] == "graph.not_found"


def test_returned_entities_reach_a_listening_view(
    run_cli: CliRunner, root: Path, roles: Roles, isolated_env: dict[str, str]
) -> None:
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[activity]\nsignal_reads = true\n")
    cls = roles.term("lifecycle_type")
    with short_dir() as state:
        env = {**ENV, "XDG_STATE_HOME": str(state)}
        listener = ActivityListener.open(state / "graph_cli" / "activity")
        try:
            text = f"MATCH (n:{label(cls)}) RETURN n LIMIT 1"
            outcome = app(run_cli, "query", text, env=env)
            assert outcome.code == 0, outcome.envelope
            (event,) = listener.receive(2.0)
            assert event.ids == (outcome.data["items"][0]["n"]["id"],)
        finally:
            listener.close()


def test_describe_schema_feeds_queries(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    schema = app(run_cli, "describe", "schema", env=ENV)
    assert schema.code == 0, schema.envelope
    for item in schema.data["relationships"]:
        text = f"MATCH {item['pattern']} RETURN count(*) AS n"
        outcome = app(run_cli, "query", text, env=ENV)
        assert outcome.code == 0, (text, outcome.envelope)
    text = run_cli("graph", "describe", "schema", env=ENV)
    assert text.returncode == 0 and text.stdout.startswith("Relationships:\n")
    assert schema.data["relationships"][0]["pattern"] in text.stdout
