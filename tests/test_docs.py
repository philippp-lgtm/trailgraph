"""The documentation holds together: links resolve, the index lists every document, the
command overviews of `graph` and `graph-admin` name exactly the commands each has, with
options those commands take, the layer diagram of docs/architecture.md matches the
layers contract of import-linter, every screenshot marker has its image, every terminal
transcript names only real `graph` commands and options, the reference block of
docs/configuration.md names exactly the keys of `config.toml` with their real defaults, its
tables name exactly the environment variables the code reads, the route tables of
docs/server.md name exactly the paths of the OpenAPI goldens, every error code, finding code
and policy action a document names exists, and every policy example of
docs/access_control.md compiles.

Covers the tracked Markdown documentation (the root README, CONTRIBUTING.md, the
example's README, docs/ and the hand-written admin skills that ship with graph-admin).
Links inside code blocks are ignored. Anchors follow GitHub's heading slugs. The command
overviews are compared with the inventory goldens, which `tests/cli/test_app_meta.py`
and `tests/cli/test_admin_init.py` keep equal to `describe commands` of each program.
Screenshots and transcripts are produced by `scripts/docs_screenshots.py`; this module only
checks that they are in place. The variables the code reads are the string constants of the
packages' sources that look like `GRAPH_CLI_*`, `AWS_*`, `CLAUDE_*`, `XDG_*` or `UV_*`, less
`ENV_NOT_DOCUMENTED`; a variable with another prefix is not seen. A code span is taken for
a code when it has the shape of one and starts like a registered code or action, unless it
names a file, a module (or a name in one) or is listed in `NOT_CODES`. Each check has a positive
control, so a broken scanner cannot pass silently.
"""

import ast
import json
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harness import REPO_ROOT
from support.fixtures import GOLDENS
from support.skills import ADMIN_SKILLS_DIR

from graph_cli.access_control.compiler import compile_policy
from graph_cli.catalog.config import _KEYS, _OPEN_TABLES, S3_KEYS, Config, parse_config
from graph_cli.catalog.registries import all_finding_codes
from graph_cli.kernel.actions import ACTIONS
from graph_cli.kernel.error_codes import ERROR_CODE_RE, ERROR_CODES
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli_admin.services_admin.error_codes import ADMIN_CODES

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
CONFIG_DOC = DOCS_DIR / "configuration.md"
TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.MULTILINE | re.DOTALL)
COMMENTED_KEY = re.compile(r"^# (?=[a-z][a-z0-9_]* = )", re.MULTILINE)
ENV_NAME = re.compile(r"\b(?:GRAPH_CLI|AWS|CLAUDE|XDG|UV)_[A-Z0-9_]+\b")
SOURCES = REPO_ROOT / "packages"
# Variables in the code that docs/configuration.md does not list, and why.
ENV_NOT_DOCUMENTED = {
    "CLAUDE_AGENT_ID": "read by the claude_code identity provider: harness internals",
    "CLAUDE_AGENT_TYPE": "read by the claude_code identity provider: harness internals",
    "CLAUDE_SESSION_ID": "read by the claude_code identity provider: harness internals",
    "CLAUDE_CODE_SESSION_ID": "read by the claude_code identity provider: harness internals",
    "UV_TOOL_DIR": "set for the uv child process of a self update, not read",
    "UV_TOOL_BIN_DIR": "set for the uv child process of a self update, not read",
}
SERVER_DOC = DOCS_DIR / "server.md"
ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")
COMMAND_ROUTE = re.compile(r"^(/v1/(?:admin/)?commands/).+$")
HTTP_METHODS = ("get", "post", "put", "patch", "delete")
# File name endings that make a dotted code span a file, not a code.
FILE_SUFFIXES = frozenset(
    {"css", "html", "json", "jsonl", "lock", "md", "py", "sh", "toml", "yaml"}
)
# Dotted spans shaped like codes that name fields of an answer or a record instead.
NOT_CODES = frozenset(
    {
        "actor.roles",
        "data.action",
        "data.columns",
        "data.entities",
        "data.findings",
        "data.fit",
        "data.function",
        "data.items",
        "data.plan",
        "data.program.executable",
        "data.results",
        "data.size",
        "graph.seq",
        "migration.to",
        "view.hash",
    }
)
# A code span opened and closed by the same number of backticks (CommonMark).
CODE_SPAN_RUN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
# A fenced block, also one indented in a list item.
FENCED_BLOCK = re.compile(r"^( *)```.*?^\1```", re.MULTILINE | re.DOTALL)
POLICY_DOC = DOCS_DIR / "access_control.md"
YAML_BLOCK = re.compile(r"^( *)```yaml\n(.*?)^\1```", re.MULTILINE | re.DOTALL)
POLICY_SCHEMA_LINE = "schema: graph_cli.policy/1"
# The graphs the policy examples name.
EXAMPLE_GRAPHS = {
    "example_graph": GraphId(UUID(int=1)),
    "other_graph": GraphId(UUID(int=2)),
}


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


def _config_reference(section: str) -> tuple[str, str]:
    """The first TOML block of a section: as written (the keys with a default) and with
    every commented key line (`# key = value`) uncommented."""
    blocks = TOML_BLOCK.findall(section)
    assert blocks, "no toml block"
    return blocks[0], COMMENTED_KEY.sub("", blocks[0])


def _toml_keys(document: dict[str, Any], table: str = "") -> dict[str, set[str]]:
    """Per dotted table name its keys; a nested table is a key of its parent too."""
    found = {table: set(document)}
    for key, value in document.items():
        if isinstance(value, dict):
            found |= _toml_keys(value, f"{table}.{key}".lstrip("."))
    return found


def _loader_keys() -> dict[str, set[str]]:
    return {**{t: set(k) for t, k in _KEYS.items()}, "files.s3": set(S3_KEYS)}


def _key_differences(documented: dict[str, set[str]]) -> dict[str, tuple[list[str], list[str]]]:
    """Per table the keys the loader has and the document lacks, and the other way round;
    the keys of an open table are names of the user's choice."""
    loader = _loader_keys()
    return {
        table: (sorted(loader.get(table, set()) - keys), sorted(keys - loader.get(table, set())))
        for table in sorted(set(loader) | set(documented))
        if table not in _OPEN_TABLES
        and (keys := documented.get(table, set())) != loader.get(table, set())
    }


def _code_env_vars(root: Path) -> set[str]:
    return {
        node.value
        for path in sorted(root.glob("*/src/**/*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ENV_NAME.fullmatch(node.value)
    }


def _doc_env_vars(text: str) -> set[str]:
    """The variables named in the tables of "Locations" and "Environment variables"."""
    return {
        name
        for heading in ("Locations", "Environment variables")
        for line in _section(text, heading).splitlines()
        if line.startswith("|")
        for name in ENV_NAME.findall(line)
    }


def _doc_routes(section: str) -> set[tuple[str, str]]:
    """The `METHOD /path` spans in the first column of the section's tables."""
    return {
        (match.group(1), match.group(2))
        for line in section.splitlines()
        if line.startswith("| `")
        for span in CODE_SPAN.findall(CELL_SEPARATOR.split(line)[1])
        if (match := ROUTE.match(span))
    }


def _openapi_routes(document: dict[str, Any]) -> set[tuple[str, str]]:
    """The routes of an OpenAPI document; the command paths as one `{path}` route."""
    return {
        (method.upper(), COMMAND_ROUTE.sub(r"\1{path}", path))
        for path, item in document["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }


def _openapi_golden(name: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads((GOLDENS / "server" / f"openapi_{name}.json").read_text())
    return found


def _spans(text: str) -> list[str]:
    """The code spans outside fenced blocks."""
    return [span.strip() for _, span in CODE_SPAN_RUN.findall(FENCED_BLOCK.sub("", text))]


def _known_names() -> set[str]:
    """Every error and finding code of both programs and every policy action."""
    return (
        set(ERROR_CODES)
        | {spec.code for spec in ADMIN_CODES}
        | set(all_finding_codes())
        | set(ACTIONS)
    )


def _is_module(span: str, root: Path) -> bool:
    """The span, or a dotted prefix of it with two parts or more, names a module or a
    package of the sources (`query.cypher.parameters` is a name in `query.cypher`)."""
    parts = span.split(".")
    tops = [p for p in root.glob("*/src/*") if p.is_dir()]
    return any(
        (top / Path(*parts[:n])).is_dir() or (top / Path(*parts[:n])).with_suffix(".py").is_file()
        for n in range(len(parts), 1, -1)
        for top in tops
    )


def _unknown_codes(text: str, known: set[str], root: Path) -> list[str]:
    """The code spans of a text that are no known code or action, in order."""
    prefixes = {name.split(".")[0] for name in known}
    return [
        span
        for span in _spans(text)
        if ERROR_CODE_RE.fullmatch(span)
        and span.split(".")[0] in prefixes
        and span not in known
        and span not in NOT_CODES
        and span.rpartition(".")[2] not in FILE_SUFFIXES
        and not _is_module(span, root)
    ]


def _policy_examples(text: str) -> list[str]:
    """The YAML blocks that are a complete policy document (they start with its schema),
    dedented when they sit in a list item."""
    return [
        "\n".join(line.removeprefix(indent) for line in body.splitlines()) + "\n"
        for indent, body in YAML_BLOCK.findall(text)
        if body.lstrip().startswith(POLICY_SCHEMA_LINE)
    ]


def _compile_example(source: str) -> str:
    """The layer of a compiled policy example; a graph policy for `example_graph`."""
    compiled = compile_policy(
        source.encode(),
        file="example.yaml",
        graphs=EXAMPLE_GRAPHS,
        graph=EXAMPLE_GRAPHS["example_graph"],
    )
    return compiled.layer


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


def test_config_reference_names_every_key() -> None:
    _, everything = _config_reference(_section(CONFIG_DOC.read_text(), "`config.toml`"))
    assert _key_differences(_toml_keys(tomllib.loads(everything))) == {}


def test_config_reference_gives_the_defaults() -> None:
    """The keys written with a value are the defaults, the commented ones valid examples."""
    written, everything = _config_reference(_section(CONFIG_DOC.read_text(), "`config.toml`"))
    path = Path("config.toml")
    assert parse_config(written.encode(), path=path) == (Config(path, exists=True), ())
    _, warnings = parse_config(everything.encode(), path=path)
    assert warnings == ()


def test_env_table_names_every_variable_the_code_reads() -> None:
    in_code = _code_env_vars(SOURCES)
    assert set(ENV_NOT_DOCUMENTED) <= in_code, "an exclusion no longer occurs in the code"
    assert _doc_env_vars(CONFIG_DOC.read_text()) == in_code - set(ENV_NOT_DOCUMENTED)


def test_route_tables_match_openapi() -> None:
    text = SERVER_DOC.read_text()
    app = _openapi_routes(_openapi_golden("app"))
    admin = _openapi_routes(_openapi_golden("admin"))
    assert _doc_routes(_section(text, "API")) == app
    assert _doc_routes(_section(text, "Admin API")) == admin - app


def test_documents_name_only_known_codes() -> None:
    known = _known_names()
    found = {
        str(path.relative_to(REPO_ROOT)): unknown
        for path in _documents()
        if (unknown := _unknown_codes(path.read_text(), known, SOURCES))
    }
    assert found == {}


def test_not_codes_are_still_used() -> None:
    used = {span for path in _documents() for span in _spans(path.read_text())}
    assert sorted(NOT_CODES - used) == []
    assert sorted(NOT_CODES & _known_names()) == []


def test_policy_examples_compile() -> None:
    examples = _policy_examples(POLICY_DOC.read_text())
    assert len(examples) >= 2
    assert {_compile_example(source) for source in examples} == {"installation", "graph"}


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

    reference = (
        '```toml\nformat_version = 1\n[files]\nstore = "local"\n# max_bytes = 1\n'
        '[files.s3]\n# bucket = "b"  # x\n# a comment = no key\n[roots]\n# n = "~/n"\n```\n'
    )
    written, everything = _config_reference(f"x\n{reference}```toml\nother = 1\n```\n")
    assert "max_bytes" not in tomllib.loads(written)["files"]
    keys = _toml_keys(tomllib.loads(everything))
    assert keys == {
        "": {"format_version", "files", "roots"},
        "files": {"store", "max_bytes", "s3"},
        "files.s3": {"bucket"},
        "roots": {"n"},
    }
    differences = _key_differences(keys)
    assert "files" not in differences
    assert differences["files.s3"][0] == sorted(set(S3_KEYS) - {"bucket"})
    assert "roots" not in differences
    assert _key_differences({**keys, "files": {"store", "s3", "nope"}})["files"] == (
        ["max_bytes"],
        ["nope"],
    )
    assert _key_differences(_loader_keys()) == {}

    assert ENV_NAME.findall("`$XDG_DATA_HOME/x`, `GRAPH_CLI_ROOT`, AWS_X1, `HTTP_GRAPH_CLI_X`") == [
        "XDG_DATA_HOME",
        "GRAPH_CLI_ROOT",
        "AWS_X1",
    ]
    assert {"GRAPH_CLI_ROOT", "AWS_REGION", "XDG_DATA_HOME"} <= _code_env_vars(SOURCES)
    env_doc = (
        "# C\n\n## Locations\n\n| a | `$XDG_DATA_HOME/x` |\nGRAPH_CLI_PROSE\n"
        "## Environment variables\n\n| `GRAPH_CLI_A`, `AWS_B` | `[x] y` |\n"
    )
    assert _doc_env_vars(env_doc) == {"XDG_DATA_HOME", "GRAPH_CLI_A", "AWS_B"}

    routes = (
        "| Endpoint | Purpose |\n| --- | --- |\n"
        "| `GET /v1/a`, `GET /v1/a/{b}` | `POST /v1/not_first_column` |\n"
        "| `POST /v1/commands/{path}` | x |\n| `set`, `where` | not a route |\n"
    )
    assert _doc_routes(routes) == {
        ("GET", "/v1/a"),
        ("GET", "/v1/a/{b}"),
        ("POST", "/v1/commands/{path}"),
    }
    openapi = {
        "paths": {
            "/v1/a": {"get": {}, "parameters": []},
            "/v1/commands/describe/types": {"post": {}},
            "/v1/admin/commands/graph/list": {"post": {}},
        }
    }
    assert _openapi_routes(openapi) == {
        ("GET", "/v1/a"),
        ("POST", "/v1/commands/{path}"),
        ("POST", "/v1/admin/commands/{path}"),
    }

    known = _known_names()
    assert {"config.invalid", "integrity.audit.chain_broken", "write.create"} <= known
    codes = (
        "`config.invalid` `config.invalidd` `write.creat` `config.toml` `storage.files.manifest`"
        " `query.cypher.parameters` `graph.seq` `kernel.unknown_thing` `pdf.encrypted` `a.b c`"
    )
    assert _unknown_codes(codes, known, SOURCES) == ["config.invalidd", "write.creat"]
    assert _spans("``(:`a`)`` `b.c`\n```\n`x.y`\n```\n  ```\n  `z.w`\n  ```\n") == ["(:`a`)", "b.c"]

    policies = (
        "```yaml\nschema: graph_cli.policy/1\nid: graph\ndeny:\n"
        '  - {id: d, role: [r], action: ["write.*"]}\n```\n'
        "- item:\n\n  ```yaml\n  schema: graph_cli.policy/1\n  id: installation\n"
        "  grants: []\n  ```\n"
        "```yaml\nid: graph\n```\n"
    )
    examples = _policy_examples(policies)
    assert len(examples) == 2
    assert examples[1].startswith("schema: graph_cli.policy/1\nid: installation\n")
    assert _compile_example(examples[0]) == "graph"
    with pytest.raises(GraphCliError):
        _compile_example(examples[0].replace("deny:", "denyy:"))
