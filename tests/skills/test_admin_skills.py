"""The hand-written admin skills that ship with graph-admin hold together.

- The bundled directories are exactly the skills graph-admin installs, each a valid
  Agent Skill (name equals the directory, a description, size budget, ASCII only), and
  none is named like a skill of graph (`graph-cli`, `graph-*`), whose freshness gate
  would read it.
- They are self-contained: they are installed into maintainer workspaces, where no `docs/`
  of this repository exists, so none cites a `docs/...` path.
- Every `graph-admin` and `graph` command line in them names a real command and only
  options it takes, checked against the inventory goldens (kept equal to `describe
  commands` by `tests/cli/test_app_meta.py` and `tests/cli/test_admin_init.py`).

Links and anchors inside the skills are checked by `tests/test_docs.py`, the ontology
format reference by `tests/skills/test_ontology_skill.py`.
"""

import json
import re
from typing import Any

import pytest
import yaml
from support.fixtures import GOLDENS
from support.skills import ADMIN_SKILLS_DIR

from graph_cli.skills.tree import NAME_RE, read_tree
from graph_cli_admin.maintenance_cli import admin_skills

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
WORD = re.compile(r"[a-z][a-z_-]*")
OPTION = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
DOCS_PATH = re.compile(r"(?<![\w./-])(?:\.\./)*docs/[\w./#-]*")


def _inventory(golden: str) -> dict[tuple[str, ...], set[str]]:
    found: dict[str, Any] = json.loads((GOLDENS / golden / "inventory.json").read_text())
    common = {"--json", "--help", *(o["name"] for o in found["global_options"])}
    return {tuple(c["path"]): {o["name"] for o in c["options"]} | common for c in found["commands"]}


PROGRAMS = {"graph-admin": _inventory("admin"), "graph": _inventory("app")}


def _lines(text: str) -> list[str]:
    """Command lines: each line of a shell (or unnamed) code block, and each code span of
    the prose (a span may wrap)."""
    found: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = line.strip()[3:] if fence is None else None
        elif fence is None:
            prose.append(line)
        elif fence in ("", "sh"):
            found.append(line)
    return [*found, *CODE_SPAN.findall(" ".join(prose))]


def _problems(text: str) -> list[str]:
    """Each command line of a program that names no command, or an option its command
    does not take; ``<program> <placeholder>`` names none, a group alone (``graph
    describe``) is fine."""
    problems = []
    for line in _lines(text):
        words = line.split()
        if len(words) < 2 or words[0] not in PROGRAMS or not WORD.fullmatch(words[1]):
            continue
        commands = PROGRAMS[words[0]]
        path: tuple[str, ...] = ()
        for word in words[1:]:
            longer = (*path, word)
            if not WORD.fullmatch(word) or not any(c[: len(longer)] == longer for c in commands):
                break
            path = longer
        if not path or (path not in commands and words[len(path) + 1 :]):
            problems.append(f"{' '.join(words)}: no such command")
            continue
        if path not in commands:
            continue  # a group alone
        unknown = sorted(set(OPTION.findall(line)) - commands[path])
        if unknown:
            problems.append(f"{' '.join(words)}: {', '.join(unknown)}")
    return problems


def test_the_bundled_skills_are_the_ones_graph_admin_installs() -> None:
    bundled = sorted(p.name for p in ADMIN_SKILLS_DIR.iterdir() if p.is_dir())
    assert bundled == sorted(admin_skills.NAMES)
    assert ADMIN_SKILLS_DIR == admin_skills.BUNDLED
    assert [s.name for s in admin_skills.admin_skills()] == list(admin_skills.NAMES)


@pytest.mark.parametrize("name", admin_skills.NAMES)
def test_each_skill_is_a_valid_agent_skill(name: str) -> None:
    directory = ADMIN_SKILLS_DIR / name
    files, problems = read_tree(directory)
    assert problems == []
    text = files["SKILL.md"].decode()
    match = FRONTMATTER.match(text)
    assert match, "SKILL.md starts with frontmatter"
    meta = yaml.safe_load(match.group(1))
    assert set(meta) == {"name", "description"}
    assert meta["name"] == name and NAME_RE.match(name) and len(name) <= 64
    assert 0 < len(meta["description"]) <= 1024
    assert name != "graph-cli" and not name.startswith("graph-")
    assert len(text.splitlines()) <= 250
    assert sum(len(data) for data in files.values()) <= 256 * 1024
    for path, data in files.items():
        assert data.decode().isascii(), path


@pytest.mark.parametrize("name", admin_skills.NAMES)
def test_every_command_line_names_a_real_command(name: str) -> None:
    files, _ = read_tree(ADMIN_SKILLS_DIR / name)
    problems = [
        f"{path}: {problem}"
        for path, data in sorted(files.items())
        if path.endswith(".md")
        for problem in _problems(data.decode())
    ]
    assert problems == []


def test_positive_controls() -> None:
    text = (
        "Run `graph-admin graph push <graph> <dir> --dry-run` and `graph-admin graph\n"
        "fly`, see `graph-admin <command> --help` and `graph describe`.\n\n"
        "```sh\ngraph find --type x:Y --nope\ngraph-admin workspace sync .\n```\n\n"
        "```markdown\ngraph bogus\n```\n"
    )
    assert _problems(text) == [
        "graph find --type x:Y --nope: --nope",
        "graph-admin graph fly: no such command",
    ]


@pytest.mark.parametrize("name", admin_skills.NAMES)
def test_no_skill_file_cites_a_docs_path(name: str) -> None:
    """The documents of this repository are not in a maintainer workspace: state the fact in the
    skill, or point to another skill or to ``--help``."""
    files, _ = read_tree(ADMIN_SKILLS_DIR / name)
    cited = [
        f"{path}: {match}"
        for path, data in sorted(files.items())
        for match in DOCS_PATH.findall(data.decode())
    ]
    assert cited == []
