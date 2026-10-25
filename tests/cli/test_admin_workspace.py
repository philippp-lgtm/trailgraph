"""S: graph-admin workspace sync and graph-admin skills install|check.

A workspace gets the admin skills, a CLAUDE.md written once and one folder per graph,
named after the graph and the end of its id; a second sync changes nothing, a graph
created later gets its folder, a folder whose graph moved on is reported, and a folder
name taken by anything else is refused without stopping the rest. The admin skills sit
next to graph's skills without either program reporting or replacing the other's.
"""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app, create_graph
from support.fixtures import ONTOLOGIES
from support.trees import tree_hash

from graph_cli_admin.maintenance_cli.admin_skills import NAMES

V110 = ONTOLOGIES / "inventory" / "1.1.0"


@pytest.fixture
def graph_id(run_cli: CliRunner, isolated_env: dict[str, str]) -> str:
    init(run_cli, isolated_env)
    return create_graph(run_cli, "example_graph", version="1.0.0")


def sync(run_cli: CliRunner, directory: Path, *args: str) -> Outcome:
    return admin(run_cli, "workspace", "sync", str(directory), *args)


def actions(outcome: Outcome) -> dict[str, tuple[str, str]]:
    return {g["graph"]: (g["folder"], g["action"]) for g in outcome.data["graphs"]}


def test_a_workspace_gets_the_skills_and_one_folder_per_graph(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    workspace = tmp_path / "admin" / "workspace"
    folder = f"example_graph_{graph_id.replace('-', '')[-6:]}"

    planned = sync(run_cli, workspace, "--dry-run")
    assert planned.code == 0, planned.envelope
    assert actions(planned) == {"example_graph": (folder, "pull")}
    assert [s["action"] for s in planned.data["skills"]] == ["create"] * len(NAMES)
    assert not workspace.exists()

    first = sync(run_cli, workspace)
    assert first.code == 0, first.envelope
    assert actions(first) == {"example_graph": (folder, "pulled")}
    assert first.data["instructions"] == "create"
    assert sorted(p.name for p in (workspace / ".claude" / "skills").iterdir()) == list(NAMES)
    base = json.loads((workspace / folder / ".graph_cli_source.json").read_text())
    assert base["graph"] == {"id": graph_id, "name": "example_graph"}
    assert (workspace / folder / "ontology" / "1.0.0").is_dir()
    assert "maintaining-graphs" in (workspace / "CLAUDE.md").read_text()

    (workspace / "CLAUDE.md").write_text("mine\n")
    before = tree_hash(workspace)
    again = sync(run_cli, workspace)
    assert again.code == 0, again.envelope
    assert actions(again) == {"example_graph": (folder, "present")}
    assert again.data["instructions"] == "kept"
    assert {s["action"] for s in again.data["skills"]} == {"unchanged"}
    assert tree_hash(workspace) == before

    checked = admin(run_cli, "skills", "check", "--scope", "project", "--dir", str(workspace))
    assert checked.code == 0, checked.envelope
    assert checked.data["counts"] == {"fresh": len(NAMES)}


def test_a_renamed_folder_is_found_and_a_moved_graph_reported(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    assert sync(run_cli, workspace).code == 0
    (folder,) = [p for p in workspace.iterdir() if p.name.startswith("example_graph_")]
    folder.rename(workspace / "renamed")
    released = admin(run_cli, "ontology", "release", "example_graph", str(V110))
    assert released.code == 0, released.envelope
    second = create_graph(run_cli, "second", version="1.0.0")

    outcome = sync(run_cli, workspace)
    assert outcome.code == 0, outcome.envelope
    assert actions(outcome) == {
        "example_graph": ("renamed", "behind"),
        "second": (f"second_{second.replace('-', '')[-6:]}", "pulled"),
    }


def test_a_taken_folder_name_is_a_conflict_and_the_rest_is_done(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    folder = workspace / f"example_graph_{graph_id.replace('-', '')[-6:]}"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("not a pull\n")
    second = create_graph(run_cli, "second", version="1.0.0")

    outcome = sync(run_cli, workspace)
    assert outcome.code == 1, outcome.envelope
    assert outcome.error["code"] == "admin.workspace_conflict"
    assert outcome.error["details"]["folders"] == [folder.name]
    assert actions(outcome) == {
        "example_graph": (folder.name, "conflict"),
        "second": (f"second_{second.replace('-', '')[-6:]}", "pulled"),
    }
    assert sorted(p.name for p in folder.iterdir()) == ["notes.md"]
    assert (workspace / ".claude" / "skills" / "maintaining-graphs" / "SKILL.md").is_file()


def test_admin_skills_and_graph_skills_share_a_directory(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    installed = admin(run_cli, "skills", "install", "--scope", "project", "--dir", str(project))
    assert installed.code == 0, installed.envelope
    assert [s["skill"] for s in installed.data["skills"]] == list(NAMES)
    assert app(run_cli, "skills", "install", "--scope", "project", "--dir", str(project)).code == 0

    graph_check = app(run_cli, "skills", "check", "--scope", "project", "--dir", str(project))
    assert graph_check.code == 0, graph_check.envelope
    assert [s["skill"] for s in graph_check.data["skills"]] == ["graph-cli"]
    admin_check = admin(run_cli, "skills", "check", "--scope", "project", "--dir", str(project))
    assert admin_check.code == 0, admin_check.envelope
    assert [s["skill"] for s in admin_check.data["skills"]] == list(NAMES)

    edited = project / ".claude" / "skills" / "maintaining-graphs" / "SKILL.md"
    edited.write_text(edited.read_text() + "\nMy own rule.\n")
    modified = admin(run_cli, "skills", "check", "--scope", "project", "--dir", str(project))
    assert modified.code == 1 and modified.error["code"] == "skills.check_failed"
    refused = admin(run_cli, "skills", "install", "--scope", "project", "--dir", str(project))
    assert refused.code == 1 and refused.error["code"] == "skills.modified"
    assert "My own rule." in edited.read_text()
    forced = admin(
        run_cli, "skills", "install", "--scope", "project", "--dir", str(project), "--force"
    )
    assert forced.code == 0, forced.envelope
    assert "My own rule." not in edited.read_text()


def test_user_scope_needs_the_agent_directory(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    missing = admin(run_cli, "skills", "install", "--scope", "user")
    assert missing.code != 0 and missing.error["code"] == "skills.target_invalid"
    Path(isolated_env["HOME"], ".claude").mkdir(parents=True)
    installed = admin(run_cli, "skills", "install", "--scope", "user")
    assert installed.code == 0, installed.envelope
    assert installed.data["target"]["path"] == str(Path(isolated_env["HOME"], ".claude", "skills"))
