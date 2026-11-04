"""S: the opt-in access log of `graph` (contracts B5.10, B12.4, B12.7): one line per command
that opened the installation, with the bytes the caller received, the time per phase and
never a value."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph
from support.fixtures import FIXTURES
from support.policies import write_policy
from support.trees import tree_hash

MARKER = "access-log-marker-7f3a"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    return root


def log_path(env: dict[str, str]) -> Path:
    return Path(env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"


def enable(env: dict[str, str]) -> None:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")


def lines(env: dict[str, str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path(env).read_text().splitlines()]


def create_location(run_cli: CliRunner) -> dict[str, Any]:
    created = app(
        run_cli,
        "create",
        "inv:Location",
        "--set",
        f"name={MARKER}",
        "--set",
        "code=R1",
        "--reason",
        MARKER,
        env=ENV,
    )
    assert created.code == 0, created.envelope
    entity: dict[str, Any] = created.data["entities"][0]
    return entity


def test_off_by_default(run_cli: CliRunner, root: Path, isolated_env: dict[str, str]) -> None:
    entity = create_location(run_cli)
    assert app(run_cli, "get", entity["id"], env=ENV).code == 0
    assert not log_path(isolated_env).exists()


def test_every_command_records_what_the_caller_received(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    entity = create_location(run_cli)
    got = run_cli("graph", "get", entity["id"], "--json", env=ENV)
    text = run_cli("graph", "get", entity["id"], env=ENV)
    missing = app(run_cli, "get", "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9", env=ENV)
    assert MARKER in got.stdout and MARKER in text.stdout  # the reads did return the value
    create, json_get, text_get, failed = lines(isolated_env)

    assert [line["command"] for line in (create, json_get, text_get, failed)] == ["create"] + [
        "get"
    ] * 3
    envelope = json.loads(got.stdout)
    assert json_get["graph"] == {k: envelope["graph"][k] for k in ("id", "name", "seq")}
    size = len(got.stdout.encode())
    assert json_get["output"] == {"format": "json", "bytes": size, "spilled_bytes": None}
    size = len(text.stdout.encode())
    assert text_get["output"] == {"format": "text", "bytes": size, "spilled_bytes": None}
    assert json_get["result"] == {"status": "ok"}
    assert failed["result"] == {"status": "error", "code": "entity.not_found"}
    assert missing.error["code"] == "entity.not_found"
    assert json_get["schema"] == "graph_cli.access/1"
    assert json_get["actor"]["client"]["name"] == "graph"
    assert create["graph"]["seq"] == json_get["graph"]["seq"] == 1

    log = log_path(isolated_env)
    assert MARKER not in log.read_text()  # no values, no reason text (B5.8)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_every_line_says_where_the_time_went(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    entity = create_location(run_cli)
    assert app(run_cli, "find", "--type", "inv:Location", env=ENV).code == 0
    assert app(run_cli, "get", entity["id"], env=ENV).code == 0
    create, find, get = (line["timing_us"] for line in lines(isolated_env))

    write_phases = {"authorize", "audit", "commit", "lock", "normalize", "validate"}
    assert write_phases <= create["phases"].keys()
    assert "query" not in create["phases"]
    for read in (find, get):
        assert {"authorize", "model", "snapshot", "query"} <= read["phases"].keys()
        assert not write_phases - {"authorize"} & read["phases"].keys()
    for timing in (create, find, get):
        phases = timing["phases"]
        assert {"startup", "context", "render", "other"} <= phases.keys()
        assert all(isinstance(us, int) and us >= 0 for us in phases.values())
        assert phases["startup"] > 0
        assert sum(phases.values()) <= timing["total"] <= sum(phases.values()) + len(phases)


def test_a_cypher_query_logs_its_phases_not_its_text(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    create_location(run_cli)
    text = f"MATCH (l:Location) WHERE l.name = '{MARKER}' RETURN l.code"
    assert app(run_cli, "query", text, env=ENV).code == 0
    *_, line = lines(isolated_env)
    assert line["command"] == "query"
    assert {"parse", "resolve", "execute", "authorize", "model", "snapshot"} <= (
        line["timing_us"]["phases"].keys()
    )
    assert "query" not in line["timing_us"]["phases"]
    assert MARKER not in log_path(isolated_env).read_text()


def test_reads_leave_the_data_root_unchanged(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    """B12.4: the access log lives in the state dir, never in the data root."""
    entity = create_location(run_cli)
    enable(isolated_env)
    before = tree_hash(root)
    assert app(run_cli, "get", entity["id"], env=ENV).code == 0
    assert app(run_cli, "find", "--type", "inv:Location", env=ENV).code == 0
    assert tree_hash(root) == before
    assert [line["command"] for line in lines(isolated_env)] == ["get", "find"]


def test_a_failing_log_never_fails_the_command(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    entity = create_location(run_cli)
    enable(isolated_env)
    log_path(isolated_env).parent.parent.mkdir(parents=True, exist_ok=True)
    log_path(isolated_env).parent.write_text("not a directory")
    assert app(run_cli, "get", entity["id"], env=ENV).code == 0


# ---- query shapes (usage shapes plan U3, U5) --------------------------------------

INVENTORY_FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
NO_EXPLORE_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
"""
COVERED = (
    "MATCH (i:Item)-[:located_in]->(l:Location) WHERE l.alias = '{alias}' RETURN i.name AS {column}"
)
UNCOVERED = "MATCH (l:Location) WHERE l.name = '{marker}' RETURN l.code AS code"


@pytest.fixture
def functions(run_cli: CliRunner, root: Path) -> Path:
    published = admin(
        run_cli, "graph", "functions", "publish", "example_graph", str(INVENTORY_FUNCTIONS)
    )
    assert published.code == 0, published.envelope
    return root


def last(env: dict[str, str]) -> dict[str, Any]:
    return lines(env)[-1]


def test_a_covered_query_logs_its_shape_and_its_functions(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    text = COVERED.format(alias=MARKER, column="item_name")
    assert app(run_cli, "query", text, env=ENV).code == 0
    query = last(isolated_env)["query"]
    assert set(query) == {"shape_hash", "shape", "covered_by"}
    assert query["covered_by"] == [{"name": "items_in_location", "list_fit": True}]
    assert query["shape_hash"].startswith("sha256:")
    assert "located_in" in query["shape"] and "Location" in query["shape"]
    assert "shape" in last(isolated_env)["timing_us"]["phases"]

    # the same question with other literals and names is the same shape
    other = COVERED.format(alias="another-alias", column="stored")
    assert app(run_cli, "query", other, "--param", "unused=1", env=ENV).code == 0
    assert last(isolated_env)["query"] == query


def test_an_uncovered_query_logs_an_empty_cover_list(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    assert app(run_cli, "query", UNCOVERED.format(marker=MARKER), env=ENV).code == 0
    query = last(isolated_env)["query"]
    assert query["covered_by"] == [] and query["shape_hash"].startswith("sha256:")


def test_a_graph_without_functions_logs_an_empty_cover_list(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    assert app(run_cli, "query", COVERED.format(alias="x", column="n"), env=ENV).code == 0
    assert last(isolated_env)["query"]["covered_by"] == []


def test_a_parse_or_resolve_error_logs_no_query(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    broken = app(run_cli, "query", "MATCH (n RETURN n", env=ENV)
    unknown = app(run_cli, "query", "MATCH (n:NoSuchClass) RETURN n", env=ENV)
    assert broken.code != 0 and unknown.code != 0
    for line in lines(isolated_env):
        assert line["command"] == "query" and line["result"]["status"] == "error"
        assert "query" not in line


def test_an_explain_is_logged_like_the_run(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    text = COVERED.format(alias=MARKER, column="item_name")
    assert app(run_cli, "query", text, env=ENV).code == 0
    run = last(isolated_env)["query"]
    assert app(run_cli, "query", text, "--explain", env=ENV).code == 0
    assert last(isolated_env)["query"] == run


def test_a_caller_without_read_function_gets_no_covers(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    enable(isolated_env)
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE_FUNCTIONS)
    assert admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy").code == 0
    text = COVERED.format(alias=MARKER, column="item_name")
    assert app(run_cli, "query", text, env=EXPLORE).code == 0
    hidden = last(isolated_env)["query"]
    assert hidden["covered_by"] == [] and hidden["shape_hash"].startswith("sha256:")
    assert app(run_cli, "query", text, env=ENV).code == 0
    allowed = last(isolated_env)["query"]
    assert [c["name"] for c in allowed["covered_by"]] == ["items_in_location"]
    assert {k: v for k, v in allowed.items() if k != "covered_by"} == {
        k: v for k, v in hidden.items() if k != "covered_by"
    }


def test_other_commands_log_no_query(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    entity = create_location(run_cli)
    assert (
        app(
            run_cli, "run", "items_in_location", "--param", f"location={entity['id']}", env=ENV
        ).code
        == 0
    )
    assert app(run_cli, "find", "--type", "inv:Location", env=ENV).code == 0
    assert all("query" not in line for line in lines(isolated_env))


def test_an_error_after_the_graph_was_open_names_the_graph(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    failed = app(run_cli, "run", "items_in_location", "--param", "location=LOC-9999", env=ENV)
    assert failed.code != 0 and failed.envelope.get("graph") is None
    line = last(isolated_env)
    assert line["result"] == {"status": "error", "code": "entity.not_found"}
    assert line["graph"] is not None and line["graph"]["name"] == "example_graph"
    assert line["function"]["name"] == "items_in_location"


def test_no_literal_or_parameter_value_reaches_the_state_dir(
    run_cli: CliRunner, functions: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    markers = ["lit_marker_3b1d", "param_marker_9e4a", "alias_marker_5c7f", "column_marker_2d8b"]
    literal, param, alias, column = markers
    key = "key_marker_6a02"  # a map key
    queries = (
        f"MATCH (l:Location) WHERE l.name = '{literal}' AND l.code IN ['{literal}', 'x'] "
        f"RETURN l.code AS {column}",
        f"MATCH (l:Location {{name: '{literal}'}}) RETURN l.code AS code SKIP 1 LIMIT 3",
        f"MATCH (l:Location) WHERE l.name = $p RETURN l.code AS {alias}, {{{key}: 1}} AS m",
        f"MATCH (i:Item)-[:located_in]->(x:Location) WHERE x.alias = $p RETURN i.name AS {alias}",
    )
    for text in queries:
        extra = ("--param", f"p={param}") if "$p" in text else ()
        assert app(run_cli, "query", text, *extra, env=ENV).code == 0
        assert app(run_cli, "query", text, *extra, "--explain", env=ENV).code == 0
    assert all("query" in line for line in lines(isolated_env))
    state = Path(isolated_env["XDG_STATE_HOME"])
    content = b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file())
    assert content  # the access log at least
    for marker in [*markers, key]:
        assert marker.encode() not in content, marker
