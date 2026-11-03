"""S: ``graph-admin usage report`` over an installation whose access log is on (usage shapes plan
U6): real ``graph query`` and ``graph run`` calls from two sessions, then the report as JSON
and as text; no log yet; one graph; a graph that does not exist."""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph
from support.fixtures import FIXTURES

MARKER = "usage-report-marker-3d9b"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
S1 = {**ENV, "GRAPH_CLI_SESSION_ID": "session-one"}
S2 = {**ENV, "GRAPH_CLI_SESSION_ID": "session-two"}
COVERED = "MATCH (i:Item)-[:located_in]->(l:Location) WHERE l.alias = '{alias}' RETURN i.name AS n"
UNCOVERED = "MATCH (l:Location) WHERE l.name = '{value}' RETURN l.code AS code"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(FUNCTIONS))
    assert published.code == 0, published.envelope
    return root


def enable(env: dict[str, str]) -> None:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")


def traffic(run_cli: CliRunner) -> None:
    created = app(
        run_cli,
        "create",
        "inv:Location",
        "--set",
        f"name={MARKER}",
        "--set",
        "code=R1",
        "--reason",
        "seed",
        env=S1,
    )
    assert created.code == 0, created.envelope
    location = created.data["entities"][0]["id"]
    for env in (S1, S2):
        assert app(run_cli, "query", COVERED.format(alias=MARKER), env=env).code == 0
        assert app(run_cli, "query", UNCOVERED.format(value=MARKER), env=env).code == 0
    assert (
        app(run_cli, "run", "items_in_location", "--param", f"location={location}", env=S1).code
        == 0
    )
    assert (
        app(
            run_cli,
            "run",
            "items_in_location",
            "--param",
            "location=0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9",
            env=S2,
        ).code
        != 0
    )


def test_the_report_reads_the_access_log_of_the_installation(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    traffic(run_cli)
    report = admin(run_cli, "usage", "report")
    assert report.code == 0, report.envelope
    assert report.data["access_log"] is True and report.data["skipped_lines"] == 0
    (graph,) = report.data["graphs"]
    assert graph["name"] == "example_graph"
    assert (graph["runs"], graph["queries"], graph["unshaped_queries"]) == (1, 4, 0)
    assert graph["function_share"] == 20
    (function,) = graph["functions"]
    assert (function["name"], function["calls"], function["sessions"]) == (
        "items_in_location",
        1,
        1,
    )
    assert report.data["without_graph"] == {"calls": 1, "errors": {"entity.not_found": 1}}
    assert graph["never_run"] == ["items_by_condition"]
    assert len(graph["shapes"]) == 2
    assert all(s["sessions"] == 2 and s["calls"] == 2 for s in graph["shapes"])
    assert [s["covered_by"][0]["name"] for s in graph["discovery_misses"]] == ["items_in_location"]
    assert len(graph["candidates"]) == 1 and graph["candidates"][0]["covered_by"] == []
    assert graph["batch_gaps"] == []
    # shapes only: no literal, no session, no actor reaches the report
    out = json.dumps(report.envelope)
    assert MARKER not in out and "session-one" not in out and "session-two" not in out


def test_the_text_report_and_since(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    enable(isolated_env)
    traffic(run_cli)
    text = run_cli("graph-admin", "usage", "report", "example_graph", "--since", "2000-01-01")
    assert text.returncode == 0, text.stderr
    assert "Graph example_graph" in text.stdout and "function share 20%" in text.stdout
    assert "Candidates" in text.stdout and "MATCH" in text.stdout
    assert MARKER not in text.stdout
    later = admin(run_cli, "usage", "report", "--since", "2999-01-01")
    assert later.data["graphs"][0]["runs"] == 0 and later.data["graphs"][0]["shapes"] == []
    assert later.data["since"] == "2999-01-01T00:00:00.000Z"
    bad = admin(run_cli, "usage", "report", "--since", "soon")
    assert bad.error["code"] == "usage.invalid_argument"


def test_without_an_access_log_the_report_is_empty_and_says_why(
    run_cli: CliRunner, root: Path
) -> None:
    report = admin(run_cli, "usage", "report")
    assert report.code == 0, report.envelope
    assert report.data["access_log"] is False
    (graph,) = report.data["graphs"]  # the published functions are still listed
    assert graph["never_run"] == ["items_by_condition", "items_in_location"]
    assert graph["function_share"] is None
    text = run_cli("graph-admin", "usage", "report")
    assert "No access log found" in text.stdout and "[audit] access_log" in text.stdout


def test_an_unknown_graph_is_not_found(run_cli: CliRunner, root: Path) -> None:
    assert admin(run_cli, "usage", "report", "nowhere").error["code"] == "graph.not_found"
