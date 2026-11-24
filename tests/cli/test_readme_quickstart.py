"""The quickstart of the root README runs: its `sh` and `toml` blocks, in order, on a
throwaway data root.

`uv run graph-admin ...` runs the installed `graph-admin` in the repository root (the
README's relative paths point into the checkout); `graph ...` and `mkdir -p` run in a
throwaway project directory; a `toml` block whose first line is a comment naming a file
(`# .graph_cli.toml`) is written to that file there. Any other command fails the test, so
the quickstart cannot grow a step this module does not run.
"""

import re
import shlex
from pathlib import Path

import pytest
from harness import REPO_ROOT, CliRunner

README = REPO_ROOT / "README.md"
BLOCK = re.compile(r"^```(sh|toml)\n(.*?)^```", re.MULTILINE | re.DOTALL)
FILE_COMMENT = re.compile(r"^# (\S+)\n")

Step = tuple[str, tuple[str, ...]]  # (kind, words): "admin", "graph", "mkdir", "file"


def _section(text: str, heading: str) -> str:
    _, found, rest = text.partition(f"\n## {heading}\n")
    assert found, f"no section {heading!r}"
    return rest.split("\n## ", 1)[0]


def _command(words: list[str]) -> Step:
    if words[:3] == ["uv", "run", "graph-admin"]:
        return "admin", tuple(words[3:])
    if words[:1] == ["graph"]:
        return "graph", tuple(words[1:])
    if words[:2] == ["mkdir", "-p"]:
        return "mkdir", tuple(words[2:])
    raise AssertionError(f"the quickstart runs a command this test does not: {words}")


def _steps(section: str) -> list[Step]:
    """The section's blocks as steps, in order; `&&` separates commands of one line."""
    steps: list[Step] = []
    for kind, body in BLOCK.findall(section):
        if kind == "toml":
            name = FILE_COMMENT.match(body)
            assert name, "a toml block of the quickstart starts with '# <file name>'"
            steps.append(("file", (name.group(1), body)))
            continue
        for line in body.splitlines():
            command: list[str] = []
            for word in shlex.split(line, comments=True):
                if word == "&&":
                    steps.append(_command(command))
                    command = []
                else:
                    command.append(word)
            if command:
                steps.append(_command(command))
    return steps


def test_quickstart_runs(run_cli: CliRunner, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    steps = _steps(_section(README.read_text(), "Quickstart"))
    assert {kind for kind, _ in steps} >= {"admin", "graph", "file"}
    for kind, words in steps:
        if kind == "file":
            (project / words[0]).write_text(words[1])
        elif kind == "mkdir":
            for directory in words:
                (project / directory).mkdir(parents=True, exist_ok=True)
        else:
            program = "graph-admin" if kind == "admin" else "graph"
            done = run_cli(program, *words, cwd=REPO_ROOT if kind == "admin" else project)
            assert done.returncode == 0, (program, words, done.stdout, done.stderr)


def test_positive_controls() -> None:
    section = (
        "Text\n```sh\nuv run graph-admin init   # a comment\n```\n"
        "```toml\n# .pointer.toml\na = 1\n```\n"
        '```sh\ngraph create t:T --set label="A b"\nmkdir -p .x && graph skills install\n```\n'
        "```text\nnot run\n```\n"
    )
    assert _steps(section) == [
        ("admin", ("init",)),
        ("file", (".pointer.toml", "# .pointer.toml\na = 1\n")),
        ("graph", ("create", "t:T", "--set", "label=A b")),
        ("mkdir", (".x",)),
        ("graph", ("skills", "install")),
    ]
    with pytest.raises(AssertionError, match="does not"):
        _steps("```sh\nrm -rf x\n```\n")
    with pytest.raises(AssertionError, match="file name"):
        _steps("```toml\na = 1\n```\n")
    assert _section("# T\n\n## A\nx\n## B\ny\n", "A") == "x"
