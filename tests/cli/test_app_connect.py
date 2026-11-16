"""S: ``graph connect`` against a real graph-server, and tokens per project (contracts B14.21).

A machine without a config gets from a one-time code to a working folder:
token stored per project, config in the remote mode, a pointer, the
project's graph skills; ``graph`` and ``graph-admin`` then find their token
through the pointer's project. Whatever stands in the way is refused before
the code is spent. The program step itself (fetch, hash, uv tool install,
also under uvx) is covered by ``tests/unit/catalog/test_dist.py``; here the
programs are the checkout's, which have no build id.
"""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, CliRunner
from support import launcher
from support.admin import admin, init
from support.app import create_graph
from support.fixtures import FIXTURES
from support.serve import Running, graph_server
from support.server import grant_server

pytestmark = pytest.mark.slow

GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: "*", level: write}
  - {entrypoint: server, principal: {project: p1}, graphs: "*", level: write}
  - {entrypoint: server, principal: {project: p2}, graphs: "*", level: read}
scopes:
  p1: {example_graph: maintain}
  p2: {example_graph: read}
no_project: {"*": write}
"""


@pytest.fixture
def server(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Any:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    assert admin(run_cli, "graph", "skill", "publish", "example_graph", str(GRAPH_SKILL)).code == 0
    grant_server(run_cli, tmp_path, POLICY)
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "[server]\nadmin_api = true\n")
    with graph_server(isolated_env, tmp_path) as running:
        yield running


def machine(base: dict[str, str], home: Path) -> dict[str, str]:
    """Another machine: its own home, nothing configured."""
    env = {
        **base,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
    }
    home.mkdir(parents=True, exist_ok=True)
    return env


def run(env: dict[str, str], cwd: Path, program: str, *args: str) -> tuple[int, dict[str, Any]]:
    result = launcher.run(
        [str(BIN_DIR / program), *args, "--json"],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.stdout.count("\n") == 1, (result.stdout, result.stderr)
    return result.returncode, json.loads(result.stdout)


def enrollment(run_cli: CliRunner, *args: str) -> str:
    created = admin(run_cli, "enroll", "create", *args)
    assert created.code == 0, created.envelope
    code: str = created.data["code"]
    return code


def endpoint(server: Running) -> str:
    return f"http://127.0.0.1:{server.port}"


def test_a_code_sets_up_a_machine_and_a_folder(
    run_cli: CliRunner, server: Running, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = machine(isolated_env, tmp_path / "laptop")
    folder = tmp_path / "work"
    folder.mkdir()
    code = enrollment(run_cli, "--project", "p1")
    exit_code, done = run(env, folder, "graph", "connect", endpoint(server), "--code", code)
    assert exit_code == 0, json.dumps(done)
    data = done["data"]
    assert data["project"] == "p1" and data["scope"] == "app"
    assert data["config"]["action"] == "created" and data["pointer"]["action"] == "created"
    assert data["program"] == {"name": "graph", "action": "skipped", "reason": "no_build"}
    token_file = Path(data["token"]["file"])
    tokens = tmp_path / "laptop" / ".config" / "graph_cli" / "tokens"
    assert token_file == tokens / f"127.0.0.1_{server.port}" / "p1" / "token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(tokens.stat().st_mode) == 0o700
    pointer = (folder / ".graph_cli.toml").read_text()
    assert 'project = "p1"' in pointer and 'default = "example_graph"' in pointer
    assert "gct_" not in pointer
    config = (tmp_path / "laptop" / ".config" / "graph_cli" / "config.toml").read_text()
    assert 'mode = "remote"' in config and endpoint(server) in config
    skills = sorted(p.name for p in (folder / ".claude" / "skills").iterdir())
    assert skills == ["graph-cli", "graph-example-graph"]

    exit_code, listed = run(env, folder, "graph", "graphs")
    assert exit_code == 0, listed
    assert [g["name"] for g in listed["data"]["items"]] == ["example_graph"]
    exit_code, whoami = run(env, folder, "graph", "whoami")
    assert whoami["data"]["actor"]["project"] == {
        "assurance": "verified",
        "id": "p1",
        "source": "token",
    }

    exit_code, again = run(env, folder, "graph", "connect", endpoint(server), "--code", code)
    assert exit_code == 3 and again["error"]["code"] == "remote.unauthorized"
    assert admin(run_cli, "audit", "verify").code == 0


def test_whatever_stands_in_the_way_is_refused_before_the_code_is_spent(
    run_cli: CliRunner, server: Running, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = machine(isolated_env, tmp_path / "laptop")
    folder = tmp_path / "work"
    folder.mkdir()
    first = enrollment(run_cli, "--project", "p1")
    assert run(env, folder, "graph", "connect", endpoint(server), "--code", first)[0] == 0
    code = enrollment(run_cli, "--project", "p1")

    exit_code, stored = run(env, folder, "graph", "connect", endpoint(server), "--code", code)
    assert exit_code == 1 and stored["error"]["code"] == "connect.refused"
    assert stored["error"]["details"]["reason"] == "token"
    assert stored["error"]["hint"] is None and "--force" in stored["error"]["message"]

    other = tmp_path / "other"
    other.mkdir()
    (other / ".graph_cli.toml").write_text(
        'format_version = 1\nproject = "p2"\ngraphs = ["example_graph"]\n'
    )
    exit_code, pointer = run(env, other, "graph", "connect", endpoint(server), "--code", code)
    assert pointer["error"]["details"]["reason"] == "pointer"

    elsewhere = machine(isolated_env, tmp_path / "desktop")
    config = Path(elsewhere["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('format_version = 1\n# mine\n[runtime]\nmode = "local"\n')
    exit_code, local = run(elsewhere, folder, "graph", "connect", endpoint(server), "--code", code)
    assert local["error"]["details"]["reason"] == "config"
    assert len(admin(run_cli, "enroll", "list").data["enrollments"]) == 1  # still unused

    exit_code, taken = run(
        elsewhere, folder, "graph", "connect", endpoint(server), "--code", code, "--force"
    )
    assert exit_code == 2 and taken["error"]["details"]["argument"] == "--label"  # same host
    exit_code, forced = run(
        elsewhere,
        folder,
        "graph",
        "connect",
        endpoint(server),
        "--code",
        code,
        "--force",
        "--label",
        "desktop",
    )
    assert exit_code == 0, json.dumps(forced)
    assert forced["data"]["config"]["action"] == "updated"
    assert forced["data"]["pointer"]["action"] == "kept"
    assert "# mine" in config.read_text() and 'mode = "remote"' in config.read_text()
    assert forced["data"]["token"]["name"] == "p1.desktop"
    assert admin(run_cli, "enroll", "list").data["enrollments"] == []


def test_an_admin_code_sets_up_graph_admin_for_the_project(
    run_cli: CliRunner, server: Running, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = machine(isolated_env, tmp_path / "laptop")
    folder = tmp_path / "maint"
    folder.mkdir()
    code = enrollment(run_cli, "--project", "p1", "--scope", "admin")
    exit_code, done = run(env, folder, "graph", "connect", endpoint(server), "--code", code)
    assert exit_code == 0, json.dumps(done)
    assert done["data"]["scope"] == "admin" and done["data"]["skills"] == []
    assert Path(done["data"]["token"]["file"]).name == "admin_token"
    exit_code, listed = run(env, folder, "graph-admin", "graph", "list")
    assert exit_code == 0, listed
    assert [g["name"] for g in listed["data"]["items"]] == ["example_graph"]
    exit_code, refused = run(env, folder, "graph-admin", "token", "list")
    assert refused["error"]["code"] == "access.ceiling"
    exit_code, app_side = run(env, folder, "graph", "graphs")
    assert app_side["error"]["code"] == "remote.unauthorized"  # no app token for p1 here


def test_a_bad_code_or_endpoint_never_reaches_the_server(
    server: Running, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = machine(isolated_env, tmp_path / "laptop")
    for args, argument in (
        ((endpoint(server), "--code", "not-a-code"), "--code"),
        (("http://graphs.example.org", "--code", "gce_" + "a" * 26), "endpoint"),
        (("ftp://x", "--code", "gce_" + "a" * 26), "endpoint"),
    ):
        exit_code, refused = run(env, tmp_path, "graph", "connect", *args)
        assert exit_code == 2 and refused["error"]["details"]["argument"] == argument
        assert "not-a-code" not in json.dumps(refused)
    assert not any(" /v1/enroll " in line for line in server.log)
