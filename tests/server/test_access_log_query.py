"""S: the access log line of a ``query`` is the same locally and through graph-server
(usage shapes plan U3): its ``query`` value depends on the query and the catalog only."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR
from support.equivalence import home_env
from support.fixtures import FIXTURES, ONTOLOGIES, compiled_model
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY

pytestmark = pytest.mark.slow

QUERIES = (
    "MATCH (i:Item)-[:located_in]->(l:Location) WHERE l.alias = 'x1' RETURN i.name AS item",
    "MATCH (l:Location) WHERE l.name = 'y' RETURN l.code AS code",
)


def prepare(env: dict[str, str], cwd: Path) -> None:
    version = compiled_model("inventory").bundle_version
    policy = cwd / "installation.yaml"
    policy.write_text(SERVER_POLICY)
    functions = FIXTURES / "functions" / "inventory" / "valid"
    for args in (
        ("init",),
        ("graph", "create", "example_graph", "--ontology", str(ONTOLOGIES / "inventory" / version)),
        ("policy", "apply", str(policy), "--reason", "access log"),
        ("graph", "functions", "publish", "example_graph", str(functions)),
    ):
        done = subprocess.run(
            [str(BIN_DIR / "graph-admin"), *args], env=env, cwd=cwd, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stdout + done.stderr
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")


def queried(env: dict[str, str], cwd: Path) -> list[Any]:
    for text in QUERIES:
        done = subprocess.run(
            [str(BIN_DIR / "graph"), "query", text, "--json"],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr
    log = Path(env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    return [json.loads(line).get("query") for line in log.read_text().splitlines()]


def test_the_query_value_is_equal_locally_and_through_the_server(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    local = home_env(isolated_env, tmp_path / "local")
    prepare(local, tmp_path)
    expected = queried(local, tmp_path)
    assert expected[0]["covered_by"] and expected[1]["covered_by"] == []

    owner = home_env(isolated_env, tmp_path / "server")
    prepare(owner, tmp_path)
    created = subprocess.run(
        [str(BIN_DIR / "graph-admin"), "token", "create", "ci", "--json"],
        env=owner,
        capture_output=True,
        text=True,
    )
    token = json.loads(created.stdout)["data"]["token"]
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            tmp_path / "client",
            server.port,
            token,
            extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n',
        )
        for text in QUERIES:
            done = subprocess.run(
                [str(BIN_DIR / "graph"), "query", text, "--json"],
                env=client,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert done.returncode == 0, done.stdout + done.stderr
    log = Path(owner["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    actual = [json.loads(line).get("query") for line in log.read_text().splitlines()]
    assert actual == expected
