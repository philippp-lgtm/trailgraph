"""S: graph self update in the local mode, and skills install --project-graphs (B14.18).

Locally the program step is skipped; the skill step runs the installed
program as a new process for every scope that holds a skill of this
program. The program step itself (fetch, hash, uv tool install) is covered
by ``tests/unit/catalog/test_dist.py``.
"""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.trees import tree_hash

GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
POINTER = 'format_version = 1\ngraphs = ["example_graph", "bare_graph"]\n'


@pytest.fixture
def project(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """A project whose pointer names a graph with a published skill and one without."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "bare_graph")
    outcome = admin(run_cli, "graph", "skill", "publish", "example_graph", str(GRAPH_SKILL))
    assert outcome.code == 0, outcome.envelope
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".graph_cli.toml").write_text(POINTER)
    return project


def skills_of(project: Path) -> list[str]:
    found = project / ".claude" / "skills"
    return (
        sorted(p.name for p in found.iterdir() if not p.name.startswith("."))
        if found.exists()
        else []
    )


def test_without_installed_skills_nothing_changes(
    run_cli: CliRunner, project: Path, isolated_env: dict[str, str]
) -> None:
    home = Path(isolated_env["HOME"])
    before = tree_hash(home), tree_hash(project)
    outcome = app(run_cli, "self", "update", cwd=project)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data == {
        "check": False,
        "program": {"name": "graph", "action": "skipped", "reason": "local"},
        "skills": [],
        "changed": False,
    }
    assert app(run_cli, "self", "update", "--check", cwd=project).code == 0
    assert (tree_hash(home), tree_hash(project)) == before


def test_a_project_gets_the_skills_of_its_graphs(run_cli: CliRunner, project: Path) -> None:
    installed = app(run_cli, "skills", "install", "--scope", "project", "--dir", str(project))
    assert installed.code == 0, installed.envelope
    assert skills_of(project) == ["graph-cli"]
    before = tree_hash(project)

    check = app(run_cli, "self", "update", "--check", "--dir", str(project))
    assert check.code == 1
    assert check.error["code"] == "update.available"
    assert check.error["hint"] == f"graph self update --dir {project}"
    (scope,) = check.data["skills"]
    assert scope["scope"] == "project" and scope["dir"] == str(project)
    assert {s["skill"]: s["action"] for s in scope["skills"]} == {
        "graph-cli": "unchanged",
        "graph-example-graph": "create",
    }
    assert scope["skipped_graphs"] == [{"graph": "bare_graph", "reason": "skills.source_missing"}]
    assert tree_hash(project) == before  # --check writes nothing

    done = app(run_cli, "self", "update", "--dir", str(project))
    assert done.code == 0, done.envelope
    assert done.data["changed"] is True
    assert done.data["skills"][0]["counts"] == {"fresh": 2}
    assert skills_of(project) == ["graph-cli", "graph-example-graph"]
    again = app(run_cli, "self", "update", "--check", cwd=project / ".claude")  # walks up
    assert again.code == 0, again.envelope
    assert again.data["skills"][0]["dir"] == str(project)


def test_an_outdated_skill_is_refreshed_in_every_scope(
    run_cli: CliRunner, project: Path, isolated_env: dict[str, str]
) -> None:
    home = Path(isolated_env["HOME"])
    (home / ".claude").mkdir()
    for scope in (["--scope", "user"], ["--scope", "project", "--dir", str(project)]):
        assert app(run_cli, "skills", "install", *scope).code == 0
    marker = home / ".claude" / "skills" / "graph-cli" / ".graph_cli_skill.json"
    document = json.loads(marker.read_text())
    document["source"]["inventory_hash"] = "sha256:" + "0" * 64  # an older program's render
    marker.write_text(json.dumps(document))
    refused = app(
        run_cli, "find", "--graph", "example_graph", "--type", "inv:Location", cwd=project
    )
    assert "skills.stale" in refused.warning_codes

    done = app(run_cli, "self", "update", cwd=project)
    assert done.code == 0, done.envelope
    by_scope = {s["scope"]: s for s in done.data["skills"]}
    assert sorted(by_scope) == ["project", "user"]
    actions = {s["skill"]: s["action"] for s in by_scope["user"]["skills"]}
    assert actions == {"graph-cli": "replace"}
    assert by_scope["user"]["counts"] == {"fresh": 1}
    read = app(run_cli, "find", "--graph", "example_graph", "--type", "inv:Location", cwd=project)
    assert "skills.stale" not in read.warning_codes


def test_a_skill_edited_by_hand_stays_unless_forced(run_cli: CliRunner, project: Path) -> None:
    assert app(run_cli, "skills", "install", "--scope", "project", "--dir", str(project)).code == 0
    edited = project / ".claude" / "skills" / "graph-cli" / "SKILL.md"
    edited.write_text(edited.read_text() + "\nmy note\n")
    held = app(run_cli, "self", "update", cwd=project)
    assert held.code == 1
    assert held.error["code"] == "skills.modified"
    assert "my note" in edited.read_text()
    assert skills_of(project) == ["graph-cli", "graph-example-graph"]  # the others still go in
    forced = app(run_cli, "self", "update", "--force", cwd=project)
    assert forced.code == 0, forced.envelope
    assert "my note" not in edited.read_text()


def test_skills_install_adds_the_pointers_graphs(run_cli: CliRunner, project: Path) -> None:
    outcome = app(
        run_cli,
        "skills",
        "install",
        "--scope",
        "project",
        "--dir",
        str(project),
        "--project-graphs",
    )
    assert outcome.code == 0, outcome.envelope
    assert [s["skill"] for s in outcome.data["skills"]] == ["graph-cli", "graph-example-graph"]
    assert outcome.data["skipped_graphs"] == [
        {"graph": "bare_graph", "reason": "skills.source_missing"}
    ]
    plain = app(run_cli, "skills", "install", "--scope", "project", "--dir", str(project))
    assert plain.data["skipped_graphs"] == []


def test_graph_admin_has_no_origin_where_the_data_lives(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    init(run_cli, isolated_env)
    outcome = admin(run_cli, "self", "update")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data == {
        "check": False,
        "program": {"name": "graph-admin", "action": "skipped", "reason": "local"},
        "changed": False,
    }
