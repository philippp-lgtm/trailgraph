"""K/S: the container owns the data and recovers from a kill mid-write (B14, D11).

Opt-in: needs Docker and ``GRAPH_CLI_DOCKER_TESTS=1`` (it builds the image).
A compose project of its own with its own volumes and port is torn down
afterwards. Writes go through the remote ``graph``; the container is killed
while they run; after a restart ``audit verify`` in the container is clean
and a write whose answer was lost is repeated with its key exactly once.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import BIN_DIR, REPO_ROOT
from support.fixtures import ONTOLOGIES
from support.serve import client_env

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GRAPH_CLI_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
        reason="set GRAPH_CLI_DOCKER_TESTS=1 with Docker available",
    ),
]

PROJECT = "graph_cli_container_test"
PORT = 18471
POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: server, graphs: "*", level: write}
scopes:
  example_project: {"*": write}
no_project: {"*": read}
"""


def compose(env: dict[str, str], *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert not check or result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def admin(env: dict[str, str], *args: str) -> dict:  # type: ignore[type-arg]
    document: dict = json.loads(compose(env, "exec", "-T", "graph", "graph-admin", *args, "--json"))  # type: ignore[type-arg]
    return document


def wait_healthy() -> None:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v1/health", timeout=2)
            return
        except OSError:
            time.sleep(1)
    raise AssertionError("the container did not answer")


@pytest.fixture
def container(tmp_path: Path) -> Iterator[dict[str, str]]:
    workspace = tmp_path / "workspace"
    shutil.copytree(ONTOLOGIES, workspace / "ontologies")
    (workspace / "policy.yaml").write_text(POLICY)
    env = {**os.environ, "GRAPH_PORT": str(PORT), "GRAPH_WORKSPACE": str(workspace)}
    compose(env, "up", "-d", "--build")
    try:
        wait_healthy()
        yield env
    finally:
        compose(env, "down", "-v", check=False)


def test_kill_mid_write_recovers(
    container: dict[str, str], isolated_env: dict[str, str], tmp_path: Path
) -> None:
    created = admin(
        container,
        "graph",
        "create",
        "example_graph",
        "--ontology",
        "/workspace/ontologies/inventory/1.0.0",
    )
    assert created["ok"], created
    assert admin(container, "policy", "apply", "/workspace/policy.yaml", "--reason", "test")["ok"]
    token = admin(container, "token", "create", "writer", "--project", "example_project")["data"][
        "token"
    ]
    client = client_env(isolated_env, tmp_path / "client", PORT, token)
    client["GRAPH_CLI_GRAPH"] = "example_graph"
    workdir = tmp_path / "client"
    outcomes: list[dict] = []  # type: ignore[type-arg]

    def write() -> None:
        for n in range(200):
            result = subprocess.run(
                [
                    str(BIN_DIR / "graph"),
                    "create",
                    "inv:Location",
                    "--set",
                    f"name=Shelf {n}",
                    "--set",
                    f"code=K{n}",
                    "--json",
                ],
                env=client,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            outcomes.append(json.loads(result.stdout))
            if not outcomes[-1]["ok"] and outcomes[-1]["error"]["code"] == "remote.unreachable":
                return

    writer = threading.Thread(target=write)
    writer.start()
    while len(outcomes) < 20:
        time.sleep(0.05)
    subprocess.run(
        ["docker", "kill", "-s", "KILL", f"{PROJECT}-graph-1"], check=True, capture_output=True
    )
    writer.join(timeout=120)
    compose(container, "up", "-d")
    wait_healthy()

    verified = admin(container, "audit", "verify")
    assert verified["ok"], verified
    assert verified["data"]["counts"]["error"] == 0
    written = sum(1 for o in outcomes if o["ok"])
    lost = [o for o in outcomes if not o["ok"] and o["error"]["code"] == "remote.outcome_unknown"]
    for outcome in lost:  # repeating with the reported key is safe
        argv = outcome["error"]["details"]["argv"]
        again = subprocess.run(
            [str(BIN_DIR / "graph"), *argv, "--json"],
            env=client,
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        assert json.loads(again.stdout)["ok"]
    found = subprocess.run(
        [str(BIN_DIR / "graph"), "find", "--type", "inv:Location", "--limit", "1", "--json"],
        env=client,
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    assert json.loads(found.stdout)["page"]["total"] == written + len(lost)
