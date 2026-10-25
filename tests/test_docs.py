"""The documentation holds together: links resolve, the index lists every document and
the command overviews of `graph` and `graph-admin` name exactly the commands each has,
with options those commands take.

Covers the tracked Markdown documentation (the root README, CONTRIBUTING.md,
docs/ and the hand-written admin skills that ship with graph-admin). Links inside
code blocks are ignored. Anchors follow GitHub's heading slugs. The command
overviews are compared with the inventory goldens, which `tests/cli/test_app_meta.py`
and `tests/cli/test_admin_init.py` keep equal to `describe commands` of each program.
Each check has a positive control, so a broken scanner cannot pass silently.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from harness import REPO_ROOT
from support.fixtures import GOLDENS
from support.skills import ADMIN_SKILLS_DIR

DOCS_DIR = REPO_ROOT / "docs"
INDEX = DOCS_DIR / "README.md"

CODE_BLOCK = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
CELL_SEPARATOR = re.compile(r"(?<!\\)\|")
CODE_SPAN = re.compile(r"`([^`]+)`")
COMMAND_WORD = re.compile(r"[a-z][a-z_-]*")
OPTION = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
# The command overview of each program and the golden of its inventory.
OVERVIEWS = (("cli_graph.md", "app"), ("cli_graph_admin.md", "admin"))


def _documents() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        *sorted(DOCS_DIR.glob("*.md")),
        *sorted(ADMIN_SKILLS_DIR.rglob("*.md")),
    ]


def _prose(text: str) -> str:
    return CODE_BLOCK.sub("", text)


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(m.group(1)) for m in HEADING.finditer(_prose(path.read_text()))}


def _broken_links(path: Path, text: str) -> list[str]:
    broken: list[str] = []
    for match in LINK.finditer(_prose(text)):
        target = match.group(1)
        if re.match(r"[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        checks_anchor = bool(anchor) and dest.suffix == ".md"
        if not dest.exists() or (checks_anchor and anchor not in _anchors(dest)):
            broken.append(target)
    return broken


def _section(text: str, heading: str) -> str:
    _, found, rest = text.partition(f"\n## {heading}\n")
    assert found, f"no section {heading!r}"
    return rest.split("\n## ", 1)[0]


def _overview_rows(section: str) -> list[tuple[list[tuple[str, ...]], set[str]]]:
    """Per table row: the command paths named in the first column (the leading words of
    each code span, up to the first placeholder or option; a span of options only names
    none) and the options named in code spans anywhere in the row."""
    rows: list[tuple[list[tuple[str, ...]], set[str]]] = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = CELL_SEPARATOR.split(line)
        commands: list[tuple[str, ...]] = []
        for span in CODE_SPAN.findall(cells[1]):
            words: list[str] = []
            for token in span.split():
                if not COMMAND_WORD.fullmatch(token):
                    break
                words.append(token)
            if words:
                commands.append(tuple(words))
        options = {o for span in CODE_SPAN.findall(line) for o in OPTION.findall(span)}
        rows.append((commands, options))
    return rows


def _overview_commands(section: str) -> list[tuple[str, ...]]:
    return [command for commands, _ in _overview_rows(section) for command in commands]


def _inventory(golden: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads((GOLDENS / golden / "inventory.json").read_text())
    return found


def _overview(doc: str) -> str:
    return _section((DOCS_DIR / doc).read_text(), "Commands")


def test_documents_exist() -> None:
    assert INDEX in _documents()
    assert all(path.is_file() for path in _documents())


def test_links_resolve() -> None:
    found = {
        str(path.relative_to(REPO_ROOT)): broken
        for path in _documents()
        if (broken := _broken_links(path, path.read_text()))
    }
    assert found == {}


def test_index_lists_every_document() -> None:
    linked = {
        (DOCS_DIR / m.group(1).partition("#")[0]).resolve()
        for m in LINK.finditer(_prose(INDEX.read_text()))
    }
    missing = [p.name for p in DOCS_DIR.glob("*.md") if p != INDEX and p.resolve() not in linked]
    assert missing == []


@pytest.mark.parametrize(("doc", "golden"), OVERVIEWS)
def test_command_overview_names_every_command(doc: str, golden: str) -> None:
    expected = sorted(tuple(command["path"]) for command in _inventory(golden)["commands"])
    assert sorted(_overview_commands(_overview(doc))) == expected


@pytest.mark.parametrize(("doc", "golden"), OVERVIEWS)
def test_command_overview_names_only_real_options(doc: str, golden: str) -> None:
    """Every option a row names belongs to one of the row's commands or is global."""
    inventory = _inventory(golden)
    common = {"--json", "--help", *(o["name"] for o in inventory["global_options"])}
    options = {
        tuple(c["path"]): {o["name"] for o in c["options"]} | common for c in inventory["commands"]
    }
    unknown = [
        (commands, sorted(named - set().union(*(options[c] for c in commands))))
        for commands, named in _overview_rows(_overview(doc))
        if named - set().union(*(options[c] for c in commands))
    ]
    assert unknown == []


def test_positive_controls() -> None:
    page = DOCS_DIR / "README.md"
    text = "[a](missing.md#x) [b](cli_graph.md#no-such-heading) [c](#nowhere)\n"
    assert _broken_links(page, text) == ["missing.md#x", "cli_graph.md#no-such-heading", "#nowhere"]
    fenced = "```\n[a](missing.md)\n```\n[ok](https://example.org) [ok](cli_graph.md)\n"
    assert _broken_links(page, fenced) == []
    assert _slug("`graph`: the application CLI") == "graph-the-application-cli"
    assert _slug("The `graph-cli` skill") == "the-graph-cli-skill"
    table = (
        "| Command | What it does |\n| --- | --- |\n"
        "| `get REF`, `describe type T` | one `find` |\n"
        "| `skills install --scope user\\|project` | a \\| b |\n"
        "| `file check [REF...]` | c |\n"
        "| `diff DIR --against DIR` / `--graph REF` | `--module` |\n"
    )
    assert _overview_commands(table) == [
        ("get",),
        ("describe", "type"),
        ("skills", "install"),
        ("file", "check"),
        ("diff",),
    ]
    assert _overview_rows(table)[-1] == ([("diff",)], {"--against", "--graph", "--module"})
    assert _section("# T\n\n## A\nx\n## B\ny\n", "A") == "x"
