"""S: ``graph start`` in the remote mode (graph start plan S3).

The graphs and the counts come from the server (the generic command route), the program and the
skills are judged and refreshed on the client: the graphs part answers the same locally and
remotely, the program is compared with the server's build (a checkout has none, so it is
``unchecked``), stale copies are refreshed from the server's skills, and the text is rendered
here from the merged answer.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR
from support import launcher
from support.equivalence import home_env, normalized
from support.fixtures import FIXTURES, ONTOLOGIES, compiled_model
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY

pytestmark = pytest.mark.slow

GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
POINTER = 'format_version = 1\ngraphs = ["example_graph"]\n'


def run(env: dict[str, str], cwd: Path, program: str, *args: str) -> Any:
    result = launcher.run(
        [str(BIN_DIR / program), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result


def admin(env: dict[str, str], cwd: Path, *args: str) -> None:
    result = run(env, cwd, "graph-admin", *args)
    assert result.returncode == 0, result.stdout + result.stderr


def installation(env: dict[str, str], tmp_path: Path) -> None:
    policy = tmp_path / "installation.yaml"
    policy.write_text(SERVER_POLICY)
    admin(env, tmp_path, "init")
    version = compiled_model("inventory").bundle_version
    for args in (
        (
            *("graph", "create", "example_graph"),
            *("--ontology", str(ONTOLOGIES / "inventory" / version)),
            *("--title", "Example inventory"),
        ),
        ("policy", "apply", str(policy), "--reason", "start"),
        ("graph", "skill", "publish", "example_graph", str(GRAPH_SKILL)),
        (
            "graph",
            "functions",
            "publish",
            "example_graph",
            str(FIXTURES / "functions/inventory/valid"),
        ),
        ("graph", "actions", "publish", "example_graph", str(FIXTURES / "actions/inventory/valid")),
        ("graph", "views", "publish", "example_graph", str(FIXTURES / "views/inventory/valid")),
    ):
        admin(env, tmp_path, *args)


def test_the_graphs_part_is_the_same_locally_and_remotely(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    local = home_env(isolated_env, tmp_path / "local")
    installation(local, tmp_path)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".graph_cli.toml").write_text(POINTER)
    here = run(local, project, "graph", "start", "--json")
    assert here.returncode == 0, here.stdout + here.stderr
    expected = json.loads(here.stdout)["data"]

    owner = home_env(isolated_env, tmp_path / "server")
    installation(owner, tmp_path)
    created = run(owner, tmp_path, "graph-admin", "token", "create", "ci", "--json")
    token = json.loads(created.stdout)["data"]["token"]
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            tmp_path / "client",
            server.port,
            token,
        )
        there = run(client, project, "graph", "start", "--json")
        assert there.returncode == 0, there.stdout + there.stderr
        data = json.loads(there.stdout)["data"]
        assert normalized(data["graphs"]) == normalized(expected["graphs"])
        assert data["hidden_count"] == expected["hidden_count"] == 0
        assert expected["program"]["state"] == "local"
        assert data["program"]["state"] == "unchecked"  # a checkout has no build to compare
        assert data["graphs"][0]["counts"]["functions"] == 2

        text = run(client, project, "graph", "start")
        assert text.returncode == 0, text.stdout + text.stderr
        assert len(text.stdout.encode()) <= 1500
        assert '  example_graph "Example inventory" (write): modules ' in text.stdout
        assert "skills: none installed here" in text.stdout


def test_stale_copies_are_refreshed_from_the_server(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    owner = home_env(isolated_env, tmp_path / "server")
    installation(owner, tmp_path)
    created = run(owner, tmp_path, "graph-admin", "token", "create", "ci", "--json")
    token = json.loads(created.stdout)["data"]["token"]
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".graph_cli.toml").write_text(POINTER)
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            tmp_path / "client",
            server.port,
            token,
        )
        installed = run(
            client, project, "graph", "skills", "install", "--scope", "project", "--dir",
            str(project), "--project-graphs", "--json",
        )  # fmt: skip
        assert installed.returncode == 0, installed.stdout + installed.stderr
        fresh = json.loads(run(client, project, "graph", "start", "--json").stdout)["data"]
        assert {(s["skill"], s["state"]) for s in fresh["skills"]} == {
            ("graph-cli", "fresh"),
            ("graph-example-graph", "fresh"),
        }
        changed = tmp_path / "changed" / GRAPH_SKILL.name
        shutil.copytree(GRAPH_SKILL, changed)
        (changed / "SKILL.md").write_text((changed / "SKILL.md").read_text() + "\nchanged\n")
        admin(owner, tmp_path, "graph", "skill", "publish", "example_graph", str(changed))
        skills = project / ".claude" / "skills"
        before = (skills / "graph-example-graph" / "SKILL.md").read_bytes()
        checked = json.loads(run(client, project, "graph", "start", "--check", "--json").stdout)
        assert {(s["skill"], s["state"]) for s in checked["data"]["skills"]} == {
            ("graph-cli", "fresh"),
            ("graph-example-graph", "stale"),
        }
        assert (skills / "graph-example-graph" / "SKILL.md").read_bytes() == before
        done = json.loads(run(client, project, "graph", "start", "--json").stdout)
        assert {(s["skill"], s["state"]) for s in done["data"]["skills"]} == {
            ("graph-cli", "fresh"),
            ("graph-example-graph", "refreshed"),
        }
        assert "skills.stale" not in [w["code"] for w in done.get("warnings", [])]
        assert (skills / "graph-example-graph" / "SKILL.md").read_bytes() != before
