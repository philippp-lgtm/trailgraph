"""S: the opt-in access log of `graph` (contracts B5.10, B12.4, B12.7): one line per command
that opened the installation, with the bytes the caller received, the time per phase and
never a value."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.app import ENV, app, create_graph
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
    assert json_get["output"] == {"format": "json", "bytes": len(got.stdout.encode())}
    assert text_get["output"] == {"format": "text", "bytes": len(text.stdout.encode())}
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
