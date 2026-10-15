"""S: graph skills emit|install|check (D15 Q1-Q5, Q8; contracts B9.1, B12 write invariant).

Every run gets a throwaway HOME (conftest); projects live under ``tmp_path``.
Tests that write prove with tree snapshots that nothing outside the named
target changed (positive control: ``test_snapshots_see_a_write``).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.skills import SKILL_DIR, committed_files
from support.trees import tree_hash

from graph_cli.skills.tree import read_tree

GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
POINTER = 'format_version = 1\ngraphs = ["{graphs}"]\n'


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def home(isolated_env: dict[str, str]) -> Path:
    return Path(isolated_env["HOME"])


@pytest.fixture
def published(run_cli: CliRunner, root: Path) -> str:
    """``example_graph`` with the fixture skill published."""
    graph_id = create_graph(run_cli, "example_graph")
    outcome = admin(run_cli, "graph", "skill", "publish", "example_graph", str(GRAPH_SKILL))
    assert outcome.code == 0, outcome.envelope
    return graph_id


def skill_files(directory: Path) -> dict[str, bytes]:
    files, problems = read_tree(directory)
    assert problems == []
    return files


def committed_skill() -> dict[str, bytes]:
    return {name: text.encode() for name, text in committed_files(SKILL_DIR).items()}


def snapshot(*roots: Path) -> list[str]:
    return [tree_hash(r) if r.exists() else "absent" for r in roots]


def test_snapshots_see_a_write(home: Path) -> None:
    """Positive control for every snapshot assertion in this module."""
    before = snapshot(home)
    (home / ".claude").mkdir()
    assert snapshot(home) != before


# ---- emit ----------------------------------------------------------------------------


def test_emit_writes_skill_one_without_marker(
    run_cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    before = snapshot(home)
    outcome = app(run_cli, "skills", "emit", "--out", str(out))  # no data root needed
    assert outcome.code == 0, outcome.envelope
    assert [s["skill"] for s in outcome.data["skills"]] == ["graph-cli"]
    assert outcome.data["skills"][0]["action"] == "create"
    assert outcome.data["program"]["name"] == "graph"
    assert skill_files(out / "graph-cli") == committed_skill()
    assert sorted(p.name for p in out.iterdir()) == ["graph-cli"]
    assert snapshot(home) == before


def test_emit_with_a_graph_skill(
    run_cli: CliRunner, published: str, home: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    before = snapshot(home)
    outcome = app(run_cli, "skills", "emit", "--out", str(out), "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    assert [s["skill"] for s in outcome.data["skills"]] == ["graph-cli", "graph-example-graph"]
    assert skill_files(out / "graph-example-graph") == skill_files(GRAPH_SKILL)
    assert snapshot(home) == before  # the data root is read, never written


def test_emit_refuses_existing_directories_unless_forced(
    run_cli: CliRunner, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    (out / "graph-cli").mkdir(parents=True)
    (out / "graph-cli" / "mine.md").write_text("mine")
    refused = app(run_cli, "skills", "emit", "--out", str(out))
    assert (refused.code, refused.error["code"]) == (1, "skills.not_owned")
    assert refused.error["hint"] == f"graph skills emit --out {out} --force"
    assert (out / "graph-cli" / "mine.md").exists()
    forced = app(run_cli, "skills", "emit", "--out", str(out), "--force")
    assert forced.code == 0 and forced.data["skills"][0]["action"] == "replace"
    assert skill_files(out / "graph-cli") == committed_skill()


def test_emit_needs_an_existing_out_directory(run_cli: CliRunner, tmp_path: Path) -> None:
    outcome = app(run_cli, "skills", "emit", "--out", str(tmp_path / "missing"))
    assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument")
    assert not (tmp_path / "missing").exists()
    missing = app(run_cli, "skills", "emit")
    assert (missing.code, missing.error["code"]) == (2, "usage.missing_argument")


def test_emit_a_graph_without_published_skill(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    out = tmp_path / "out"
    out.mkdir()
    outcome = app(run_cli, "skills", "emit", "--out", str(out), "--graph", "example_graph")
    assert (outcome.code, outcome.error["code"]) == (1, "skills.source_missing")
    assert outcome.error["hint"] == "graph-admin graph skill publish example_graph <dir>"
    assert list(out.iterdir()) == []  # refusals come before any write


def test_emit_respects_the_pointer_allowlist(
    run_cli: CliRunner, published: str, tmp_path: Path
) -> None:
    create_graph(run_cli, "other_graph")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".graph_cli.toml").write_text(POINTER.format(graphs="other_graph"))
    outcome = app(run_cli, "skills", "emit", "--out", ".", "--graph", "example_graph", cwd=project)
    assert (outcome.code, outcome.error["code"]) == (1, "graph.not_in_project")
    assert sorted(p.name for p in project.iterdir()) == [".graph_cli.toml"]


def test_emit_text_mode(run_cli: CliRunner, tmp_path: Path) -> None:
    result = run_cli("graph", "skills", "emit", "--out", str(tmp_path))
    assert result.returncode == 0 and result.stderr == ""
    assert "graph-cli" in result.stdout


def emit_all(run_cli: CliRunner, tmp_path: Path) -> dict[str, Any]:
    out = tmp_path / "emitted"
    out.mkdir()
    outcome = app(run_cli, "skills", "emit", "--out", str(out), "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    return {p.name: skill_files(p) for p in sorted(out.iterdir())}


def test_emitted_graph_skill_copy_is_independent_of_the_source(
    run_cli: CliRunner, published: str, tmp_path: Path
) -> None:
    """The app reads the published slot, not the maintainer's directory."""
    moved = tmp_path / "moved"
    shutil.copytree(GRAPH_SKILL, moved)
    emitted = emit_all(run_cli, tmp_path)
    assert emitted["graph-example-graph"] == skill_files(moved)


# ---- install -------------------------------------------------------------------------

MARKER = ".graph_cli_skill.json"


def new_project(tmp_path: Path, *, claude: bool = True, pointer: str | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if claude:
        (project / ".claude").mkdir()
    if pointer is not None:
        (project / ".graph_cli.toml").write_text(POINTER.format(graphs=pointer))
    return project


def install(run_cli: CliRunner, *args: str, cwd: Path | None = None, **kw: Any) -> Any:
    return app(run_cli, "skills", "install", *args, cwd=cwd, **kw)


def installed(skills: Path) -> dict[str, dict[str, bytes]]:
    return {p.name: skill_files(p) for p in sorted(skills.iterdir())}


def without_marker(files: dict[str, bytes]) -> dict[str, bytes]:
    return {k: v for k, v in files.items() if k != MARKER}


def test_install_into_a_project(run_cli: CliRunner, home: Path, tmp_path: Path) -> None:
    project = new_project(tmp_path)
    before = snapshot(home)
    outcome = install(run_cli, "--scope", "project", "--dir", str(project))
    assert outcome.code == 0, outcome.envelope
    skills = project / ".claude" / "skills"
    assert outcome.data["target"] == {
        "scope": "project",
        "dir": str(project.resolve()),
        "path": str(skills.resolve()),
    }
    assert [(s["skill"], s["action"]) for s in outcome.data["skills"]] == [("graph-cli", "create")]
    assert outcome.data["dry_run"] is False and outcome.data["unavailable"] == []
    files = installed(skills)
    assert list(files) == ["graph-cli"]
    assert without_marker(files["graph-cli"]) == committed_skill()
    assert MARKER in files["graph-cli"]
    assert sorted(p.name for p in (project / ".claude").iterdir()) == ["skills"]  # no staging
    assert snapshot(home) == before

    tree = snapshot(project)
    again = install(run_cli, "--scope", "project", "--dir", str(project))
    assert [s["action"] for s in again.data["skills"]] == ["unchanged"]
    assert snapshot(project) == tree  # byte-identical


def test_install_defaults_to_the_working_directory(run_cli: CliRunner, tmp_path: Path) -> None:
    project = new_project(tmp_path)
    outcome = install(run_cli, "--scope", "project", cwd=project)
    assert outcome.code == 0, outcome.envelope
    assert (project / ".claude" / "skills" / "graph-cli" / "SKILL.md").is_file()


def test_install_needs_the_agent_directory(run_cli: CliRunner, home: Path, tmp_path: Path) -> None:
    project = new_project(tmp_path, claude=False)
    before = snapshot(home, project)
    outcome = install(run_cli, "--scope", "project", "--dir", str(project))
    assert (outcome.code, outcome.error["code"]) == (1, "skills.target_invalid")
    assert outcome.error["hint"] == f"mkdir {project.resolve() / '.claude'}"
    user = install(run_cli, "--scope", "user")
    assert (user.code, user.error["code"]) == (1, "skills.target_invalid")
    assert user.error["details"]["path"] == str(home.resolve() / ".claude")
    assert snapshot(home, project) == before


def test_install_without_scope_writes_nothing(
    run_cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    project = new_project(tmp_path)
    (home / ".claude").mkdir()
    before = snapshot(home, project)
    outcome = install(run_cli, cwd=project)
    assert (outcome.code, outcome.error["code"]) == (2, "usage.missing_argument")
    wrong = install(run_cli, "--scope", "user", "--dir", str(project), cwd=project)
    assert (wrong.code, wrong.error["code"]) == (2, "usage.invalid_argument")
    assert snapshot(home, project) == before


def test_install_for_the_user(run_cli: CliRunner, home: Path, tmp_path: Path) -> None:
    (home / ".claude").mkdir()
    project = new_project(tmp_path)
    outside = snapshot(project)
    outcome = install(run_cli, "--scope", "user", cwd=project)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["target"] == {
        "scope": "user",
        "path": str(home.resolve() / ".claude" / "skills"),
    }
    assert [p.relative_to(home).as_posix() for p in sorted((home / ".claude").rglob("*"))][:2] == [
        ".claude/skills",
        ".claude/skills/graph-cli",
    ]
    assert snapshot(project) == outside


def test_install_for_the_user_follows_claude_config_dir(
    run_cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    config = tmp_path / "agent_config"
    config.mkdir()
    (home / ".claude").mkdir()
    before = snapshot(home)
    outcome = install(run_cli, "--scope", "user", env={"CLAUDE_CONFIG_DIR": str(config)})
    assert outcome.code == 0, outcome.envelope
    assert (config / "skills" / "graph-cli" / "SKILL.md").is_file()
    assert snapshot(home) == before


def test_foreign_and_edited_directories_need_force(
    run_cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    project = new_project(tmp_path)
    skills = project / ".claude" / "skills"
    (skills / "graph-cli").mkdir(parents=True)
    (skills / "graph-cli" / "SKILL.md").write_text("mine")
    (skills / "pdf").mkdir()  # unrelated skills are never touched
    (skills / "pdf" / "SKILL.md").write_text("other")
    args = ("--scope", "project", "--dir", str(project))
    before = snapshot(project)
    refused = install(run_cli, *args)
    assert (refused.code, refused.error["code"]) == (1, "skills.not_owned")
    assert (
        refused.error["hint"]
        == f"graph skills install --scope project --dir {project.resolve()} --force"
    )
    assert refused.data is None
    assert snapshot(project) == before
    forced = install(run_cli, *args, "--force")
    assert forced.code == 0 and forced.data["skills"][0]["action"] == "replace"
    assert without_marker(installed(skills)["graph-cli"]) == committed_skill()
    assert installed(skills)["pdf"] == {"SKILL.md": b"other"}

    (skills / "graph-cli" / "SKILL.md").write_text("edited by hand")
    edited = install(run_cli, *args)
    assert (edited.code, edited.error["code"]) == (1, "skills.modified")
    assert edited.error["details"]["refused"] == [{"skill": "graph-cli", "installed": "modified"}]
    assert install(run_cli, *args, "--force").code == 0


def test_dry_run_writes_nothing(run_cli: CliRunner, home: Path, tmp_path: Path) -> None:
    project = new_project(tmp_path)
    before = snapshot(home, project)
    outcome = install(run_cli, "--scope", "project", "--dir", str(project), "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["dry_run"] is True
    assert [s["action"] for s in outcome.data["skills"]] == ["create"]
    assert snapshot(home, project) == before


def test_legacy_marker_is_replaced(run_cli: CliRunner, tmp_path: Path) -> None:
    project = new_project(tmp_path)
    old = project / ".claude" / "skills" / "graph-cli"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("old copy")
    (old / ".graph_cli_install").write_text("installed by the former script\n")
    outcome = install(run_cli, "--scope", "project", "--dir", str(project))
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["skills"][0] == {
        "skill": "graph-cli",
        "kind": "app",
        "action": "replace",
        "installed": "legacy",
    }
    assert set(skill_files(old)) == {*committed_skill(), MARKER}


def test_graph_skills_install_and_refresh(
    run_cli: CliRunner, published: str, home: Path, tmp_path: Path
) -> None:
    project = new_project(tmp_path, pointer="example_graph")
    args = ("--scope", "project", "--dir", str(project))
    before = snapshot(home)
    outcome = install(run_cli, *args, "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    assert [s["skill"] for s in outcome.data["skills"]] == ["graph-cli", "graph-example-graph"]
    skills = project / ".claude" / "skills"
    graph_skill = installed(skills)["graph-example-graph"]
    assert without_marker(graph_skill) == skill_files(GRAPH_SKILL)
    assert snapshot(home) == before  # reading the data root writes nothing

    changed = tmp_path / "changed" / "graph-example-graph"
    shutil.copytree(GRAPH_SKILL, changed)
    (changed / "references" / "model.md").write_text("# Model\n\nRepublished.\n")
    republish = admin(run_cli, "graph", "skill", "publish", "example_graph", str(changed))
    assert republish.code == 0, republish.envelope
    refreshed = install(run_cli, *args)  # no --graph: what is installed is refreshed
    assert refreshed.code == 0, refreshed.envelope
    assert [(s["skill"], s["action"]) for s in refreshed.data["skills"]] == [
        ("graph-cli", "unchanged"),
        ("graph-example-graph", "replace"),
    ]
    assert installed(skills)["graph-example-graph"]["references/model.md"] == (
        b"# Model\n\nRepublished.\n"
    )


def test_graph_skills_follow_the_project_pointer(
    run_cli: CliRunner, published: str, tmp_path: Path
) -> None:
    create_graph(run_cli, "other_graph")
    project = new_project(tmp_path, pointer="example_graph")
    args = ("--scope", "project", "--dir", str(project))
    assert install(run_cli, *args, "--graph", "example_graph").code == 0
    # Run from elsewhere: the project's pointer decides, not the working directory's.
    (project / ".graph_cli.toml").write_text(POINTER.format(graphs="other_graph"))
    refused = install(run_cli, *args, "--graph", "example_graph", cwd=tmp_path)
    assert (refused.code, refused.error["code"]) == (1, "graph.not_in_project")
    left = install(run_cli, *args, cwd=tmp_path)
    assert left.code == 0, left.envelope
    assert left.data["unavailable"] == [
        {"skill": "graph-example-graph", "graph": published, "reason": "graph.not_in_project"}
    ]
    assert [s["skill"] for s in left.data["skills"]] == ["graph-cli"]
    assert (project / ".claude" / "skills" / "graph-example-graph" / "SKILL.md").is_file()


def test_install_a_graph_without_published_skill(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    project = new_project(tmp_path)
    before = snapshot(project)
    outcome = install(
        run_cli, "--scope", "project", "--dir", str(project), "--graph", "example_graph"
    )
    assert (outcome.code, outcome.error["code"]) == (1, "skills.source_missing")
    assert snapshot(project) == before


def test_source_behind_ontology_warns(run_cli: CliRunner, published: str, tmp_path: Path) -> None:
    from support.fixtures import ONTOLOGIES

    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.0.1")
    )
    assert released.code == 0, released.envelope
    project = new_project(tmp_path)
    outcome = install(
        run_cli, "--scope", "project", "--dir", str(project), "--graph", "example_graph"
    )
    assert outcome.code == 0, outcome.envelope
    assert outcome.warning_codes == ["skills.source_behind_ontology"]


# ---- check ---------------------------------------------------------------------------


def check(run_cli: CliRunner, project: Path, **kw: Any) -> Any:
    return app(run_cli, "skills", "check", "--scope", "project", "--dir", str(project), **kw)


def statuses(outcome: Any) -> dict[str, str]:
    return {s["skill"]: s["status"] for s in outcome.data["skills"]}


def test_check_fresh_and_program_block(run_cli: CliRunner, home: Path, tmp_path: Path) -> None:
    from harness import BIN_DIR

    project = new_project(tmp_path)
    assert install(run_cli, "--scope", "project", "--dir", str(project)).code == 0
    before = snapshot(home, project)
    outcome = check(run_cli, project)
    assert outcome.code == 0, outcome.envelope
    assert statuses(outcome) == {"graph-cli": "fresh"}
    assert outcome.data["ok_count"] == 1 and outcome.data["counts"] == {"fresh": 1}
    program = outcome.data["program"]
    assert program["name"] == "graph"
    assert program["executable"] == str((BIN_DIR / "graph").resolve())  # this program (F6)
    assert snapshot(home, project) == before  # check only reads


def test_check_reports_every_status(run_cli: CliRunner, published: str, tmp_path: Path) -> None:
    project = new_project(tmp_path, pointer="example_graph")
    skills = project / ".claude" / "skills"
    missing = check(run_cli, project)
    assert (missing.code, missing.error["code"]) == (1, "skills.check_failed")
    assert statuses(missing) == {"graph-cli": "missing"}
    assert missing.error["details"]["counts"] == {"missing": 1}
    assert missing.error["hint"] == (
        f"graph skills install --scope project --dir {project.resolve()}"
    )

    args = ("--scope", "project", "--dir", str(project), "--graph", "example_graph")
    assert install(run_cli, *args).code == 0
    assert statuses(check(run_cli, project)) == {
        "graph-cli": "fresh",
        "graph-example-graph": "fresh",
    }

    changed = tmp_path / "changed" / "graph-example-graph"
    shutil.copytree(GRAPH_SKILL, changed)
    (changed / "references" / "model.md").write_text("# Model\n\nRepublished.\n")
    assert admin(run_cli, "graph", "skill", "publish", "example_graph", str(changed)).code == 0
    stale = check(run_cli, project)
    assert stale.code == 1
    assert statuses(stale) == {"graph-cli": "fresh", "graph-example-graph": "stale"}
    assert stale.data["ok_count"] == 1  # the partial result keeps the list (B9.2)

    (skills / "graph-example-graph" / "SKILL.md").write_text("edited")
    (skills / "graph-cli" / "notes.md").write_text("extra")
    (skills / "graph-other").mkdir()
    (skills / "graph-other" / "SKILL.md").write_text("someone else's")
    outcome = check(run_cli, project)
    assert statuses(outcome) == {
        "graph-cli": "unexpected",
        "graph-example-graph": "modified",
        "graph-other": "foreign",
    }
    assert outcome.error["details"]["counts"] == {"modified": 1, "unexpected": 1, "foreign": 1}


def test_check_unavailable_graph_skill(run_cli: CliRunner, published: str, tmp_path: Path) -> None:
    project = new_project(tmp_path, pointer="example_graph")
    args = ("--scope", "project", "--dir", str(project), "--graph", "example_graph")
    assert install(run_cli, *args).code == 0
    create_graph(run_cli, "other_graph")
    (project / ".graph_cli.toml").write_text(POINTER.format(graphs="other_graph"))
    outcome = check(run_cli, project, cwd=tmp_path)
    assert outcome.code == 1
    (skill,) = [s for s in outcome.data["skills"] if s["skill"] == "graph-example-graph"]
    assert skill == {
        "skill": "graph-example-graph",
        "kind": "graph",
        "status": "unavailable",
        "graph": published,
        "reason": "graph.not_in_project",
    }


def test_check_warns_when_the_source_is_behind_the_ontology(
    run_cli: CliRunner, published: str, tmp_path: Path
) -> None:
    from support.fixtures import ONTOLOGIES

    project = new_project(tmp_path)
    args = ("--scope", "project", "--dir", str(project), "--graph", "example_graph")
    assert install(run_cli, *args).code == 0
    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.0.1")
    )
    assert released.code == 0, released.envelope
    outcome = check(run_cli, project)
    assert outcome.code == 0, outcome.envelope  # warnings never change the exit code
    assert outcome.warning_codes == ["skills.source_behind_ontology"]


def test_check_text_mode_prints_the_list_and_the_error(run_cli: CliRunner, tmp_path: Path) -> None:
    project = new_project(tmp_path)
    result = run_cli("graph", "skills", "check", "--scope", "project", "--dir", str(project))
    assert result.returncode == 1
    assert "graph-cli" in result.stdout and "missing" in result.stdout
    assert "skills.check_failed" in result.stderr
