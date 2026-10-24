"""S and P: ``graph-server serve`` as a process (contracts B14.8, B14.9, B14.14; D12).

Threads writing through the server and a ``graph-admin`` process maintaining
the same graph lose no writes: every request takes the graph's ``flock`` on
its own open file description, like a process.
"""

import signal
import subprocess
import threading
from pathlib import Path

import pytest
from harness import BIN_DIR, CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.serve import graph_server
from support.server import grant_server, new_token

pytestmark = pytest.mark.slow


@pytest.fixture
def token(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> str:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    grant_server(run_cli, tmp_path)
    return new_token(run_cli, "ci")


def test_serves_until_terminated(isolated_env: dict[str, str], tmp_path: Path, token: str) -> None:
    with graph_server(isolated_env, tmp_path) as server:
        assert server.request("GET", "/v1/health")[0] == 200
        status, envelope = server.request("POST", "/v1/commands/whoami", token=token, body={})
        assert status == 200, envelope
        assert envelope["data"]["actor"]["principal"]["id"] == "token:ci"
        server.process.send_signal(signal.SIGTERM)
        assert server.process.wait(timeout=30) == 0
    log = "".join(server.log)
    assert "POST /v1/commands/whoami 200" in log and "token=ci" in log
    assert token not in log
    assert "stopped" in log


def test_plain_http_beyond_loopback_needs_the_flag(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    result = subprocess.run(
        [str(BIN_DIR / "graph-server"), "serve", "--host", "0.0.0.0"],
        env=isolated_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "--allow-plain-http" in result.stderr


def test_a_remote_config_refuses_to_serve(isolated_env: dict[str, str], tmp_path: Path) -> None:
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "http://127.0.0.1:1"\n'
    )
    result = subprocess.run(
        [str(BIN_DIR / "graph-server"), "serve"],
        env=isolated_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 5
    assert "config.invalid" in result.stderr


def test_concurrent_writers_lose_no_writes(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, token: str
) -> None:
    threads, writes = 4, 5
    failures: list[object] = []
    with graph_server(isolated_env, tmp_path) as server:

        def write(worker: int) -> None:
            for index in range(writes):
                body = {
                    "args": {
                        "type": "inv:Location",
                        "--set": [f"name=Shelf {worker}-{index}", f"code=T{worker}-{index}"],
                        "--graph": "example_graph",
                    }
                }
                status, envelope = server.request(
                    "POST", "/v1/commands/create", token=token, body=body
                )
                if status != 200:
                    failures.append(envelope)

        def maintain() -> None:
            for _ in range(3):
                outcome = run_cli("graph-admin", "graph", "reindex", "example_graph", "--json")
                if outcome.returncode != 0:
                    failures.append(outcome.stdout)

        workers = [threading.Thread(target=write, args=(n,)) for n in range(threads)]
        workers.append(threading.Thread(target=maintain))
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=300)
        assert failures == []
        status, envelope = server.request(
            "POST",
            "/v1/commands/find",
            token=token,
            body={"args": {"--type": ["inv:Location"], "--graph": "example_graph"}},
        )
        assert status == 200
        assert envelope["page"]["total"] == threads * writes
    assert admin(run_cli, "audit", "verify").code == 0
