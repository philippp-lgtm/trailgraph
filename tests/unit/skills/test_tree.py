"""Skill tree validation: one positive control per rule (D15 Q6)."""

import os
from pathlib import Path

import pytest

from graph_cli.skills.tree import (
    LEAKS,
    MARKER_FILE,
    MAX_SKILL_LINES,
    MAX_TOTAL_BYTES,
    graph_skill_name,
    read_tree,
    validate,
    write_tree,
)

NAME = "graph-example-graph"


def skill_md(name: str = NAME, description: str = '"Use for the example graph."') -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Example\n".encode()


def valid() -> dict[str, bytes]:
    return {"SKILL.md": skill_md(), "references/model.md": b"# Model\n"}


def rules(files: dict[str, bytes], **kw: str) -> list[str]:
    return [p.rule for p in validate(files, name=kw.pop("name", NAME), **kw)]


def test_a_valid_tree_has_no_problems() -> None:
    assert validate(valid(), name=NAME, directory=NAME) == []


def test_graph_skill_names() -> None:
    assert graph_skill_name("example_graph") == NAME
    assert graph_skill_name("kb") == "graph-kb"


@pytest.mark.parametrize("name", ["Graph-x", "graph_x", "graph--x", "-x", "x" * 65])
def test_name_rule(name: str) -> None:
    files = {"SKILL.md": skill_md(name)}
    assert "name" in rules(files, name=name)


def test_directory_must_equal_the_name() -> None:
    assert rules(valid(), directory="graph-other") == ["name"]


@pytest.mark.parametrize(
    "path", [".hidden", "refs/.hidden", MARKER_FILE, ".graph_cli_install", "/abs", "a/../b"]
)
def test_file_names(path: str) -> None:
    assert rules({**valid(), path: b"x"}) == ["file"]


def test_utf8_only() -> None:
    assert rules({**valid(), "refs.md": b"\xff\xfe"}) == ["file"]


def test_skill_md_is_required() -> None:
    assert rules({"README.md": b"x"}) == ["file"]


@pytest.mark.parametrize(
    "text",
    [
        b"# no frontmatter\n",
        b"---\nname: graph-example-graph\n",
        skill_md(name="graph-other"),
        skill_md(description=""),
        skill_md(description='"' + "x" * 1025 + '"'),
    ],
)
def test_frontmatter(text: bytes) -> None:
    assert rules({"SKILL.md": text}) == ["frontmatter"]


@pytest.mark.parametrize(
    "description",
    ["plain text", "'single ''quoted'''", '"double \\"quoted\\""', ">\n  folded\n  block"],
)
def test_frontmatter_scalar_styles(description: str) -> None:
    assert rules({"SKILL.md": skill_md(description=description)}) == []


def test_size_budgets() -> None:
    long = skill_md() + b"line\n" * MAX_SKILL_LINES
    assert rules({"SKILL.md": long}) == ["size"]
    big = {**valid(), "references/big.md": b"x" * MAX_TOTAL_BYTES}
    assert rules(big) == ["size"]


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("maintenance program", "run graph-admin graph list"),
        ("maintenance action", "needs maint.graph_configure"),
        ("maintenance code", "fails with admin.skill_invalid"),
        ("data root option", "pass --data-root"),
        ("data root variable", "set GRAPH_CLI_ROOT"),
        ("absolute path", "see /Users/someone/x"),
        ("absolute path", "see /home/someone/x"),
        ("home path", "see ~/.claude"),
        ("home path", "see $HOME/x"),
        ("windows path", "see C:\\x"),
    ],
)
def test_leaks(kind: str, text: str) -> None:
    problems = validate({**valid(), "references/model.md": text.encode()}, name=NAME)
    assert [p.rule for p in problems] == ["leak"]
    assert kind in problems[0].message and problems[0].path == "references/model.md"


def test_every_leak_kind_has_a_control() -> None:
    assert {kind for kind, _ in LEAKS} == {
        "maintenance program",
        "maintenance action",
        "maintenance code",
        "data root option",
        "data root variable",
        "absolute path",
        "home path",
        "windows path",
    }


def test_harmless_neighbours_are_no_leaks() -> None:
    text = "the admin. a sysadmin.x role, relative data/x/skill/ paths, graph-cli"
    assert rules({**valid(), "references/model.md": text.encode()}) == []


def test_read_and_write_round_trip(tmp_path: Path) -> None:
    write_tree(tmp_path / NAME, valid())
    files, problems = read_tree(tmp_path / NAME)
    assert files == valid() and problems == []
    with pytest.raises(FileExistsError):
        write_tree(tmp_path / NAME, valid())


def test_read_tree_reports_symlinks_without_following_them(tmp_path: Path) -> None:
    write_tree(tmp_path / NAME, valid())
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    os.symlink(outside, tmp_path / NAME / "link.md")
    os.symlink(tmp_path, tmp_path / NAME / "loop")
    files, problems = read_tree(tmp_path / NAME)
    assert files == valid()
    assert sorted(p.path for p in problems) == ["link.md", "loop"]
