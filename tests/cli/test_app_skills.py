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
