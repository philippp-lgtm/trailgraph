"""The documentation holds together: links resolve, the index lists every document, the
command overviews of `graph` and `graph-admin` name exactly the commands each has, with
options those commands take, the layer diagram of docs/architecture.md matches the
layers contract of import-linter, every screenshot marker has its image, and every terminal
transcript names only real `graph` commands and options.

Covers the tracked Markdown documentation (the root README, CONTRIBUTING.md, the
example's README, docs/ and the hand-written admin skills that ship with graph-admin).
Links inside code blocks are ignored. Anchors follow GitHub's heading slugs. The command
overviews are compared with the inventory goldens, which `tests/cli/test_app_meta.py`
and `tests/cli/test_admin_init.py` keep equal to `describe commands` of each program.
Screenshots and transcripts are produced by `scripts/docs_screenshots.py`; this module only
checks that they are in place. Each check has a positive control, so a broken scanner cannot
pass silently.
"""

import json
import re
import shlex
import tomllib
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
MERMAID_BLOCK = re.compile(r"^```mermaid\n(.*?)^```", re.MULTILINE | re.DOTALL)
LAYER_NODE = re.compile(r'^\s*L(\d+)\["([^"]*)"\]\s*$', re.MULTILINE)
LAYER_EDGE_LINE = re.compile(r"^\s*L\d+(?:\s*-->\s*L\d+)+\s*$", re.MULTILINE)
LAYERS_CONTRACT = "graph_cli layers"
IMAGES_DIR = DOCS_DIR / "images"
SCREENSHOT = re.compile(r"^<!-- screenshot: ([a-z0-9_]+) -->\n(.*)$", re.MULTILINE)
PLACEHOLDER = "Screenshot placeholder"
TRANSCRIPT_START = re.compile(r"^<!-- transcript: ([a-z0-9_]+) -->$", re.MULTILINE)
TRANSCRIPT = re.compile(
    r"^<!-- transcript: ([a-z0-9_]+) -->\n```text\n(.*?)^```\n<!-- /transcript -->$",
    re.MULTILINE | re.DOTALL,
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 400_000


def _documents() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "examples" / "company" / "README.md",
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


def _diagram_layers(section: str) -> list[list[str]]:
    """The layers of the one Mermaid block in a section, top down: per node `L<n>` the
    subpackages after the label's `<br>`. Nodes must be numbered 1..n and joined top down
    by one chain of edges, else the diagram does not say what the contract says."""
    blocks = MERMAID_BLOCK.findall(section)
    assert len(blocks) == 1, "expected one mermaid block"
    nodes = {int(n): label for n, label in LAYER_NODE.findall(blocks[0])}
    order = sorted(nodes)
    assert order == list(range(1, len(order) + 1)), f"layer nodes not numbered 1..n: {order}"
    chain = [
        int(n) for line in LAYER_EDGE_LINE.findall(blocks[0]) for n in re.findall(r"L(\d+)", line)
    ]
    assert chain == order, f"edges do not chain the layers top down: {chain}"
    return [sorted(p.strip() for p in nodes[n].rpartition("<br>")[2].split(",")) for n in order]


def _contract_layers() -> list[list[str]]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    (contract,) = (
        c for c in config["tool"]["importlinter"]["contracts"] if c["name"] == LAYERS_CONTRACT
    )
    return [
        sorted(p.strip().removeprefix("graph_cli.") for p in layer.split("|"))
        for layer in contract["layers"]
    ]


def _screenshot_problems(text: str, images: set[str]) -> list[str]:
    """Each `<!-- screenshot: name -->` is directly followed by an image link to
    `images/<name>.png`, which is one of `images`; no placeholder text is left."""
    problems = [
        f"{name}: no image link" if f"](images/{name}.png)" not in nxt or not nxt.startswith("![")
        else f"{name}: no file"
        for name, nxt in SCREENSHOT.findall(text)
        if f"](images/{name}.png)" not in nxt or not nxt.startswith("![") or name not in images
    ]  # fmt: skip
    return problems + ([PLACEHOLDER] if PLACEHOLDER in text else [])


def _transcript_problems(text: str, inventory: dict[str, Any]) -> list[str]:
    """Every transcript block is complete, holds commands, and each `$ ` line runs a
    command of `graph` with options that command (or every command) takes."""
    common = {"--json", "--help", *(o["name"] for o in inventory["global_options"])}
    options = {
        tuple(c["path"]): {o["name"] for o in c["options"]} | common for c in inventory["commands"]
    }
    blocks = TRANSCRIPT.findall(text)
    problems = [
        f"{name}: unterminated"
        for name in TRANSCRIPT_START.findall(text)
        if name not in {found for found, _ in blocks}
    ]
    for name, body in blocks:
        lines = [line[2:] for line in body.splitlines() if line.startswith("$ ")]
        if not lines:
            problems.append(f"{name}: no command")
        for line in lines:
            words = shlex.split(line)
            path = next(
                (tuple(words[1:n]) for n in range(len(words), 1, -1)
                 if tuple(words[1:n]) in options),
                None,
            )  # fmt: skip
            if words[0] != "graph" or path is None:
                problems.append(f"{name}: unknown command {line!r}")
                continue
            named = {w.partition("=")[0] for w in words if w.startswith("--")}
            problems += [f"{name}: {path} has no {o}" for o in sorted(named - options[path])]
    return problems


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


def test_layer_diagram_matches_import_contract() -> None:
    section = _section((DOCS_DIR / "architecture.md").read_text(), "Layers")
    assert _diagram_layers(section) == _contract_layers()


def test_screenshot_markers_have_images() -> None:
    images = {p.stem for p in IMAGES_DIR.glob("*.png")}
    found = {
        str(path.relative_to(REPO_ROOT)): problems
        for path in _documents()
        if (problems := _screenshot_problems(path.read_text(), images))
    }
    assert found == {}


def test_images_are_small_pngs_and_used() -> None:
    marked = {name for path in _documents() for name, _ in SCREENSHOT.findall(path.read_text())}
    files = sorted(IMAGES_DIR.iterdir())
    assert files, "docs/images/ holds no image"
    assert [p.name for p in files if p.suffix != ".png" or p.stem not in marked] == []
    assert [p.name for p in files if not p.read_bytes().startswith(PNG_SIGNATURE)] == []
    assert [p.name for p in files if p.stat().st_size > MAX_IMAGE_BYTES] == []


def test_transcripts_name_real_commands() -> None:
    inventory = _inventory("app")
    found = {
        str(path.relative_to(REPO_ROOT)): problems
        for path in _documents()
        if (problems := _transcript_problems(path.read_text(), inventory))
    }
    assert found == {}
    assert any(TRANSCRIPT.search(path.read_text()) for path in _documents())


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
    diagram = (
        "```mermaid\nflowchart TB\n"
        '    L1["Top<br>a"]\n    L2["Middle<br>b, c"]\n    L3["Bottom<br>d"]\n'
        "    L1 --> L2 --> L3\n```\n"
    )
    assert _diagram_layers(diagram) == [["a"], ["b", "c"], ["d"]]
    with pytest.raises(AssertionError, match="edges"):
        _diagram_layers(diagram.replace("L1 --> L2 --> L3", "L1 --> L3 --> L2"))
    with pytest.raises(AssertionError, match="numbered"):
        _diagram_layers(diagram.replace("L3[", "L4[").replace("--> L3", "--> L4"))
    assert _diagram_layers(diagram.replace("b, c", "c")) != [["a"], ["b", "c"], ["d"]]
    assert ["kernel"] in _contract_layers()

    marked = "x\n<!-- screenshot: a -->\n![A](images/a.png)\n<!-- screenshot: b -->\n> b\n"
    assert _screenshot_problems(marked, {"a"}) == ["b: no image link"]
    assert _screenshot_problems(marked.replace("> b", "![B](images/b.png)"), {"a"}) == [
        "b: no file"
    ]
    assert _screenshot_problems(f"> **{PLACEHOLDER}:** x\n", set()) == [PLACEHOLDER]
    inventory = _inventory("app")
    transcript = (
        "<!-- transcript: t -->\n```text\n$ graph start\nok\n"
        "$ graph run f --param p=1 --graph g\n$ graph link A r B --qualifier on=1 --nope\n"
        "$ graph frobnicate\n$ ls\n```\n<!-- /transcript -->\n<!-- transcript: u -->\n"
    )
    assert _transcript_problems(transcript, inventory) == [
        "u: unterminated",
        "t: ('link',) has no --nope",
        "t: unknown command 'graph frobnicate'",
        "t: unknown command 'ls'",
    ]
    empty = "<!-- transcript: e -->\n```text\nnothing\n```\n<!-- /transcript -->\n"
    assert _transcript_problems(empty, inventory) == ["e: no command"]
