"""S: ``graph-admin usage report`` counts the sessions that began with ``graph start`` from the
access log of a server: the client's session and agent ids reach the server line of every command
route, ``start`` included, so the report reads the same as it does locally."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR
from support import launcher
from support.equivalence import home_env
from support.fixtures import ONTOLOGIES, compiled_model
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY

pytestmark = pytest.mark.slow

QUERY = "MATCH (l:Location) WHERE l.name = 'y' RETURN l.code AS code"


def run(binary: str, env: dict[str, str], cwd: Path, *args: str) -> Any:
    done = launcher.run(
        [str(BIN_DIR / binary), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)["data"] if "--json" in args else done.stdout


def server_with_log(isolated_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    owner = home_env(isolated_env, tmp_path / "server")
    version = compiled_model("inventory").bundle_version
    policy = tmp_path / "installation.yaml"
    policy.write_text(SERVER_POLICY)
    run("graph-admin", owner, tmp_path, "init")
    run(
        "graph-admin",
        owner,
        tmp_path,
        *(
            "graph",
            "create",
            "example_graph",
            "--ontology",
            str(ONTOLOGIES / "inventory" / version),
        ),
    )
    run("graph-admin", owner, tmp_path, "policy", "apply", str(policy), "--reason", "sessions")
    config = Path(owner["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    return owner


def calls(
    isolated_env: dict[str, str],
    tmp_path: Path,
    owner: dict[str, str],
    plan: list[tuple[str, str | None, str]],
) -> dict[str, Any]:
    """Run ``plan`` (session id, agent id, command) as clients of one server, then the report."""
    token = run("graph-admin", owner, tmp_path, "token", "create", "ci", "--json")["token"]
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            tmp_path / "client",
            server.port,
            token,
            extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n',
        )
        for session, agent, command in plan:
            env = {**client, "GRAPH_CLI_SESSION_ID": session}
            if agent is not None:
                env["GRAPH_CLI_AGENT_ID"] = agent
            args = ("start", "--check") if command == "start" else ("query", QUERY)
            run("graph", env, tmp_path, *args, "--json")
    log = Path(owner["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(ln["command"], ln["actor"]["session"]["id"]) for ln in lines] == [
        (command, session) for session, _, command in plan
    ]  # the client's session id is in the server's line, start included
    report: dict[str, Any] = run("graph-admin", owner, tmp_path, "usage", "report", "--json")
    return report


def test_the_server_log_tells_which_session_began_with_start(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    owner = server_with_log(isolated_env, tmp_path)
    report = calls(
        isolated_env,
        tmp_path,
        owner,
        [
            ("session-one", None, "start"),
            ("session-one", None, "query"),
            ("session-two", None, "query"),
            ("session-two", None, "start"),
        ],
    )
    assert report["sessions"] == {"count": 2, "first_start": 1, "first_start_share": 50}


def test_the_server_log_keeps_the_agents_of_one_session_apart(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    owner = server_with_log(isolated_env, tmp_path)
    report = calls(
        isolated_env,
        tmp_path,
        owner,
        [
            ("session-one", "agent-main", "start"),
            ("session-one", "agent-sub", "query"),  # a subagent that skipped start
            ("session-one", "agent-sub", "start"),
        ],
    )
    assert report["sessions"] == {"count": 2, "first_start": 1, "first_start_share": 50}
