"""The documentation holds together: links resolve, the index lists every document and
the command overview of `graph` names exactly the commands it has.

Covers the tracked Markdown documentation (the root README, CONTRIBUTING.md,
docs/ and the hand-written `ontology-best-practices` skill). Links inside
code blocks are ignored. Anchors follow GitHub's heading slugs. The command
overview is compared with the inventory golden, which
`tests/cli/test_app_meta.py` keeps equal to `graph describe commands`.
Each check has a positive control, so a broken scanner cannot pass silently.
"""

import json
import re
from pathlib import Path

from harness import REPO_ROOT
from support.fixtures import GOLDENS

DOCS_DIR = REPO_ROOT / "docs"
ONTOLOGY_SKILL = REPO_ROOT / "skills" / "ontology-best-practices"
INDEX = DOCS_DIR / "README.md"

CODE_BLOCK = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
CELL_SEPARATOR = re.compile(r"(?<!\\)\|")
CODE_SPAN = re.compile(r"`([^`]+)`")
COMMAND_WORD = re.compile(r"[a-z][a-z_-]*")


def _documents() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        *sorted(DOCS_DIR.glob("*.md")),
        *sorted(ONTOLOGY_SKILL.rglob("*.md")),
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


def _overview_commands(section: str) -> list[tuple[str, ...]]:
    """The command paths named in the first column of the table: the leading words of each
    code span, up to the first placeholder or option."""
    commands: list[tuple[str, ...]] = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = CELL_SEPARATOR.split(line)[1]
        for span in CODE_SPAN.findall(first_cell):
            words: list[str] = []
            for token in span.split():
                if not COMMAND_WORD.fullmatch(token):
                    break
                words.append(token)
            commands.append(tuple(words))
    return commands


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


def test_command_overview_names_every_command() -> None:
    inventory = json.loads((GOLDENS / "app" / "inventory.json").read_text())
    expected = sorted(tuple(command["path"]) for command in inventory["commands"])
    section = _section((DOCS_DIR / "cli_graph.md").read_text(), "Commands")
    assert sorted(_overview_commands(section)) == expected


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
    )
    assert _overview_commands(table) == [
        ("get",),
        ("describe", "type"),
        ("skills", "install"),
        ("file", "check"),
    ]
    assert _section("# T\n\n## A\nx\n## B\ny\n", "A") == "x"
