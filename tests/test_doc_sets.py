"""Lists in the documentation that mirror a closed set of the code name exactly that set.

- `SETS`: the code spans between two phrases of a document equal a code set (a value set,
  a status list, the parameter types ...). A member added to or removed from the code fails
  here until the document follows.
- The module tables of docs/architecture.md, docs/audit.md and docs/storage.md name only
  modules that exist (a bare name in a cell after a dotted one is a sibling of it), and
  every module of a package in `COMPLETE_PACKAGES` has a row.
- The extractor table of docs/files.md names every registered extractor with its module,
  its record and part classes and the attributes it records.
- The phases of the access log in docs/audit.md are the phases the code times (every
  `phase("...")` and `until_now("...")` literal, plus `other`); the keys of its access-log
  table are the keys `access_record` writes.
- `FIELDS`: the field names between two phrases of a document equal the keys the output
  schema of a command declares at a path (`changes` items, `start` graphs, the short
  envelope ...), so a key added to or removed from an answer fails here.
- The commands a project admin may run (docs/server.md), the meta commands that are never
  skill-checked (docs/skills.md) and the parts push uploads (docs/server.md) equal their
  sets in code; crash points named anywhere exist.

Lists a scanner cannot find stay a review matter (CONTRIBUTING.md "Documentation"). Each
parser has a positive control.
"""

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest
import yaml
from harness import REPO_ROOT

from graph_cli.access_control.compiler import WRITE_VERBS
from graph_cli.app_cli.commands import all_specs as app_specs
from graph_cli.app_cli.output_schemas import DEFS as APP_DEFS
from graph_cli.app_cli.output_schemas import RECONCILE
from graph_cli.catalog import access_log
from graph_cli.catalog.config import Config, FileStoreKind, OutputMode
from graph_cli.catalog.tokens import SCOPES
from graph_cli.cli_kit.output_schemas import DEFS as KIT_DEFS
from graph_cli.cli_kit.spec import Pairs, PairValues
from graph_cli.extractors.registry import REGISTRY
from graph_cli.kernel.actions import SCOPE_LEVELS
from graph_cli.kinetic.params import PARAM_TYPES
from graph_cli.ontology_layer.migration import STEP_NAMES
from graph_cli.services import describe_tools
from graph_cli.services.files import CHECK_STATUSES
from graph_cli.services.tool_errors import ON_ERROR_VALUES
from graph_cli.skills.install import STATUSES
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.durable import Durability
from graph_cli.storage.s3.settings import Addressing
from graph_cli_admin.maintenance_cli.commands import all_specs as admin_specs
from graph_cli_admin.maintenance_cli.output_schemas import DEFS as ADMIN_DEFS
from graph_cli_admin.services_admin.sources import PARTS, SOURCE_FILE
from graph_cli_server.admin_api import PROJECT_COMMANDS

DOCS = REPO_ROOT / "docs"
SOURCES = {
    "graph_cli": REPO_ROOT / "packages/graph_cli/src/graph_cli",
    "graph_cli_admin": REPO_ROOT / "packages/graph_cli_admin/src/graph_cli_admin",
    "graph_cli_server": REPO_ROOT / "packages/graph_cli_server/src/graph_cli_server",
}
MODULES = REPO_ROOT / "packages/graph_cli/src/graph_cli/ontology_layer/modules"

FENCED_BLOCK = re.compile(r"^( *)```.*?^\1```", re.MULTILINE | re.DOTALL)
CODE_SPAN_RUN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
CELL_SEPARATOR = re.compile(r"(?<!\\)\|")
TABLE_RULE = re.compile(r"^\|[ :|-]+\|$")
CRASH_POINT = re.compile(r"^[a-z_]+\.(?:before|after)_[a-z_]+$")
CURIE = re.compile(r"^[a-z_]+:[A-Z]\w*$")
NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _prose(text: str) -> str:
    return re.sub(r"\s+", " ", FENCED_BLOCK.sub(" ", text))


def _spans(text: str) -> list[str]:
    return [span.strip() for _, span in CODE_SPAN_RUN.findall(text)]


def _between(prose: str, start: str, end: str) -> str:
    """The text after the one occurrence of `start` up to the next `end`."""
    start, end = re.sub(r"\s+", " ", start), re.sub(r"\s+", " ", end)
    assert prose.count(start) == 1, f"{start!r} occurs {prose.count(start)} times"
    rest = prose.split(start, 1)[1]
    assert end in rest, f"{end!r} not found after {start!r}"
    return rest.split(end, 1)[0]


@dataclass(frozen=True)
class DocSet:
    doc: str
    start: str
    end: str
    values: frozenset[str]
    quoted: bool  # the members are the quoted strings inside the spans (config values)
    ignore: frozenset[str]  # spans of the region that are no members


def doc_set(
    doc: str,
    start: str,
    end: str,
    values: tuple[object, ...] | frozenset[str],
    *,
    quoted: bool = False,
    ignore: tuple[str, ...] = (),
) -> DocSet:
    return DocSet(doc, start, end, frozenset(map(str, values)), quoted, frozenset(ignore))


def _members(item: DocSet, prose: str) -> set[str]:
    spans = _spans(_between(prose, item.start, item.end))
    if item.quoted:
        return {value for span in spans for value in re.findall(r'"([^"]+)"', span)}
    return set(spans) - item.ignore


SETS: tuple[DocSet, ...] = (
    doc_set("cli_graph.md", "the server API in `pairs` (", ")", get_args(Pairs)),
    doc_set("cli_graph.md", "and `pair_values` (", ")", get_args(PairValues)),
    doc_set("cli_graph.md", "compare origins with the stored content:", "|", CHECK_STATUSES),
    doc_set("cli_graph.md", "by the type the function declares (", ")", PARAM_TYPES),
    doc_set("cli_graph_admin.md", "report each admin skill as", "(`--dir`)", STATUSES),
    doc_set("skills.md", "`graph skills check` reports each skill as", "and names", STATUSES),
    doc_set("configuration.md", "`durability` (", ")", get_args(Durability), quoted=True),
    doc_set(
        "configuration.md", "- `[output]`: `default` (", ")", get_args(OutputMode), quoted=True
    ),
    doc_set(
        "configuration.md",
        "`client_builds`:",
        "gets a warning",
        get_args(get_type_hints(Config)["client_builds"]),
        quoted=True,
    ),
    doc_set(
        "configuration.md",
        "`[files] store` decides",
        "object store of",
        get_args(FileStoreKind),
        quoted=True,
    ),
    doc_set(
        "configuration.md",
        "Empty means AWS itself, reached by region.",
        "into the host name",
        get_args(Addressing),
        quoted=True,
    ),
    doc_set(
        "access_control.md",
        "matches the exact type).",
        "An absent or",
        WRITE_VERBS,
        ignore=("true",),
    ),
    doc_set("access_control.md", "Levels are", "for a named graph", SCOPE_LEVELS),
    doc_set(
        "server.md",
        "Every token has a scope:",
        "only the admin API",
        SCOPES,
        ignore=("graph", "token create NAME --scope admin"),
    ),
)


def _schema_keys(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """The properties of a schema by name (`[]` for the items of an array), through
    `$ref`, `oneOf`, `anyOf` and `allOf`."""
    if "$ref" in schema:
        return _schema_keys(defs[schema["$ref"].rsplit("/", 1)[1]], defs)
    keys: dict[str, Any] = {}
    for combinator in ("oneOf", "anyOf", "allOf"):
        for branch in schema.get(combinator, []):
            keys |= _schema_keys(branch, defs)
    if isinstance(schema.get("items"), dict):
        keys["[]"] = schema["items"]
    return keys | schema.get("properties", {})


def _answer_keys(program: str, command: str, *path: str) -> frozenset[str]:
    """The keys the output schema of a command declares at a path below `data`."""
    specs, defs = (app_specs(), APP_DEFS) if program == "graph" else (admin_specs(), ADMIN_DEFS)
    schema = next(s.output for s in specs if " ".join(s.path) == command)
    assert schema is not None, command
    for key in path:
        schema = _schema_keys(schema, defs)[key]
    return frozenset(_schema_keys(schema, defs)) - {"[]"}


def _enum(program: str, command: str, *path: str) -> frozenset[str]:
    specs, defs = (app_specs(), APP_DEFS) if program == "graph" else (admin_specs(), ADMIN_DEFS)
    schema = next(s.output for s in specs if " ".join(s.path) == command)
    assert schema is not None, command
    for key in path:
        schema = _schema_keys(schema, defs)[key]
    return frozenset(schema["enum"])


def _names(item: DocSet, prose: str) -> set[str]:
    """The field names of a region: its spans that look like a name, less `ignore`."""
    return {s for s in _spans(_between(prose, item.start, item.end)) if NAME.match(s)} - item.ignore


def _class_attributes(curie: str) -> set[str]:
    module, name = curie.split(":")
    document = yaml.safe_load((MODULES / f"{module}.yaml").read_text())
    return set(document["classes"][name].get("attributes", {}))


def _relation_qualifiers(curie: str) -> set[str]:
    module, name = curie.split(":")
    document = yaml.safe_load((MODULES / f"{module}.yaml").read_text())
    return set(document["relations"][name].get("qualifiers", {}))


_SHORT = _schema_keys(KIT_DEFS["short_envelope"], KIT_DEFS)
_RECONCILE_SCOPE = _schema_keys(_schema_keys(RECONCILE, {})["scopes"], {})["[]"]

FIELDS: tuple[DocSet, ...] = (
    doc_set(
        "cli_graph.md",
        "since every answer goes into an agent's context:",
        "read a missing key",
        frozenset(_SHORT) | frozenset(_schema_keys(_SHORT["graph"], KIT_DEFS)),
        ignore=("null",),
    ),
    doc_set(
        "cli_graph.md",
        "**The answer.** `data` has these keys, in this order:",
        "An entry has",
        _answer_keys("graph", "changes"),
        ignore=("null", "true"),
    ),
    doc_set(
        "cli_graph.md",
        "An entry has",
        "Each item of `entities` has",
        _answer_keys("graph", "changes", "items", "[]"),
        ignore=("history", "name", "hash", "do"),
    ),
    doc_set(
        "cli_graph.md",
        "Each item of `entities` has",
        "The text form prints",
        _answer_keys("graph", "changes", "items", "[]", "entities", "[]"),
        ignore=("history", "null"),
    ),
    doc_set(
        "cli_graph.md",
        "- **Graphs.** One item per graph the project may use",
        "`hidden_count` is the",
        _answer_keys("graph", "start", "graphs", "[]")
        | _answer_keys("graph", "start", "graphs", "[]", "counts"),
        ignore=("graphs", "write", "read", "none", "whoami"),
    ),
    doc_set(
        "cli_graph.md",
        "The write result has `reconcile`:",
        "(item keys;",
        frozenset(_schema_keys(RECONCILE, {})) - {"scopes"}
        | frozenset(_schema_keys(_RECONCILE_SCOPE, {})),
    ),
    doc_set(
        "files.md",
        "- `files:File` holds",
        "A graph module may subclass",
        frozenset(_class_attributes("files:File")),
        ignore=("blob", "local_path"),
    ),
    doc_set(
        "files.md",
        "- `files:from_file` points from any entity to a file, with the qualifiers",
        "number).",
        frozenset(_relation_qualifiers("files:from_file")),
    ),
    doc_set(
        "cli_graph.md",
        "document with these keys, in this order:",
        "The options narrow the list",
        _answer_keys("graph", "describe tools") - {"schema", "graph", "ontology"},
    ),
    doc_set(
        "cli_graph.md",
        "- `get --rev`, the revisions behind",
        "stored, for example",
        STEP_NAMES,
        ignore=("history", "changes", "changed"),
    ),
)

# Packages whose every module has a row in the module tables of the internals documents.
COMPLETE_PACKAGES = (
    "graph_cli.access_control",
    "graph_cli.audit",
    "graph_cli.catalog",
    "graph_cli.extractors",
    "graph_cli.kinetic",
    "graph_cli.services.read",
    "graph_cli.sources",
)
MODULE_DOCS = ("architecture.md", "audit.md", "storage.md")
# A table whose cells hold bare module names, found by a phrase of the text before it.
TABLE_PACKAGES = {"`services.read` is a package": "services.read"}


def _tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """Each Markdown table of a text: the text before it and its rows as cell lists."""
    lines = text.splitlines()
    tables: list[tuple[str, list[list[str]]]] = []
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith("|") and TABLE_RULE.match(lines[i + 1]):
            j = i + 2
            rows = []
            while j < len(lines) and lines[j].startswith("|"):
                rows.append([cell.strip() for cell in CELL_SEPARATOR.split(lines[j])[1:-1]])
                j += 1
            before = " ".join(lines[max(0, i - 4) : i])
            tables.append((f"{before}\n{lines[i]}", rows))
            i = j
        else:
            i += 1
    return tables


def _module_names(text: str) -> list[str]:
    """The modules the first column of every "Module" or "Package" table names, dotted
    from the package root (`services.read.state`); a bare name after a dotted one in the
    same cell is its sibling."""
    names: list[str] = []
    for before, rows in _tables(text):
        header = before.rsplit("\n", 1)[1]
        if not re.match(r"^\| (Module|Package) \|", header):
            continue
        package = next((pkg for phrase, pkg in TABLE_PACKAGES.items() if phrase in before), None)
        for row in rows:
            parent = package
            for span in _spans(row[0]):
                if "." in span:
                    parent = span.rsplit(".", 1)[0]
                    names.append(span)
                else:
                    names.append(f"{parent}.{span}" if parent else span)
    return names


def _module_path(name: str) -> Path | None:
    """The file or package directory of a dotted name; names without a top-level package
    are in `graph_cli`."""
    parts = name.split(".")
    top = parts[0] if parts[0] in SOURCES else "graph_cli"
    rest = parts[1:] if parts[0] in SOURCES else parts
    path = SOURCES[top].joinpath(*rest)
    if path.with_suffix(".py").is_file():
        return path.with_suffix(".py")
    return path if (path / "__init__.py").is_file() else None


def _package_modules(package: str) -> set[str]:
    directory = _module_path(package)
    assert directory is not None and directory.is_dir(), package
    short = package.removeprefix("graph_cli.")
    return {f"{short}.{p.stem}" for p in directory.glob("*.py") if p.stem != "__init__"} | {
        f"{short}.{p.name}" for p in directory.iterdir() if (p / "__init__.py").is_file()
    }


def _extractor_rows(text: str) -> dict[str, list[str]]:
    """The extractor table of docs/files.md: extractor name -> [module, record cell]."""
    for before, rows in _tables(text):
        if before.endswith("| Module | Extractor | Records | Applies to |"):
            return {_spans(row[1])[0]: [_spans(row[0])[0], row[2]] for row in rows}
    raise AssertionError("no extractor table")


def _record_problems(name: str, module: str, cell: str) -> list[str]:
    """The record cell names the extractor's record and part classes and only attribute
    names of those classes."""
    extractor = REGISTRY[name]
    problems = [] if module == extractor.module.id else [f"{name}: module {module}"]
    classes = {extractor.record_class, *([extractor.part_class] if extractor.part_class else [])}
    spans = _spans(cell)
    named = {span for span in spans if CURIE.match(span)}
    if named != classes:
        problems.append(f"{name}: classes {sorted(named)}, the code {sorted(classes)}")
    attributes = set().union(*(_class_attributes(c) for c in classes))
    problems += [
        f"{name}: {span} is no attribute of {', '.join(sorted(classes))}"
        for span in spans
        if NAME.match(span) and span not in attributes
    ]
    return problems


def _timed_phases(root: Path) -> set[str]:
    """Every phase name the sources time: the literal of `phase("...")` and
    `until_now("...")`."""
    phases: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("phase", "until_now")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                phases.add(node.args[0].value)
    return phases


def _access_log_keys() -> set[str]:
    """The keys `access_record` writes: those of its `record` literal and those assigned
    to `record[...]` later."""
    tree = ast.parse(Path(access_log.__file__).read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign) and isinstance(node.value, ast.Dict):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "record" for t in targets):
                keys |= {str(k.value) for k in node.value.keys if isinstance(k, ast.Constant)}
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "record"
            and isinstance(node.targets[0].slice, ast.Constant)
        ):
            keys.add(str(node.targets[0].slice.value))
    return keys


def _first_column(text: str, header: str) -> set[str]:
    for before, rows in _tables(text):
        if before.endswith(header):
            return {_spans(row[0])[0] for row in rows}
    raise AssertionError(f"no table {header!r}")


def _meta_commands() -> set[str]:
    inventory = json.loads((REPO_ROOT / "tests/goldens/app/inventory.json").read_text())
    return {" ".join(c["path"]) for c in inventory["commands"] if c["kind"] == "meta"}


def test_between_and_members_read_the_region() -> None:
    prose = _prose('Levels are `none`, `read` and\n`write`; `true` here. Store `"a" = 1`.')
    item = doc_set("x.md", "Levels are", ";", ("none", "read", "write"))
    assert _members(item, prose) == {"none", "read", "write"}
    assert _members(doc_set("x.md", "Store", ".", (), quoted=True), prose) == {"a"}
    with pytest.raises(AssertionError, match="occurs 0 times"):
        _between(prose, "Missing", ".")


def test_tables_and_module_names_resolve_siblings() -> None:
    text = (
        "`services.read` is a package:\n\n| Module | Holds |\n| --- | --- |\n"
        "| `state` | x |\n\n| Module | Content |\n| --- | --- |\n"
        "| `extractors.format`, `pdf` | y |\n| Key | z |\n"
    )
    assert _module_names(text) == ["services.read.state", "extractors.format", "extractors.pdf"]
    assert _module_path("services.read.state") is not None
    assert _module_path("services.read.nothing") is None
    assert _module_path("graph_cli_server.http_status") is not None


def test_record_problems_find_a_wrong_attribute() -> None:
    cell = "`files_table:TableMetadata`: `delimiter`, `rows_total`"
    assert _record_problems("table", "files_table", cell) == [
        "table: rows_total is no attribute of files_table:TableMetadata"
    ]
    assert _record_problems("table", "files", "`files:Other`") == [
        "table: module files",
        "table: classes ['files:Other'], the code ['files_table:TableMetadata']",
    ]


def test_scans_find_phases_and_access_log_keys() -> None:
    assert {"startup", "authorize", "commit"} <= _timed_phases(SOURCES["graph_cli"])
    assert {"schema", "ts", "command", "query"} <= _access_log_keys()


@pytest.mark.parametrize("item", SETS, ids=lambda s: f"{s.doc}:{s.start[:30]}")
def test_list_names_the_code_set(item: DocSet) -> None:
    members = _members(item, _prose((DOCS / item.doc).read_text()))
    assert members == item.values, (sorted(members - item.values), sorted(item.values - members))


def test_schema_keys_follow_refs_and_branches() -> None:
    defs = {"x": {"type": "object", "properties": {"a": {}, "b": {"type": "array", "items": {}}}}}
    schema = {"oneOf": [{"$ref": "#/$defs/x"}, {"properties": {"c": {}}}]}
    assert set(_schema_keys(schema, defs)) == {"a", "b", "c"}
    assert "[]" in _schema_keys(defs["x"]["properties"]["b"], defs)
    assert {"since", "items"} <= _answer_keys("graph", "changes")


@pytest.mark.parametrize("item", FIELDS, ids=lambda s: f"{s.doc}:{s.start[:30]}")
def test_field_list_names_the_answer_keys(item: DocSet) -> None:
    names = _names(item, _prose((DOCS / item.doc).read_text()))
    assert names == item.values, (sorted(names - item.values), sorted(item.values - names))


def test_module_tables_name_existing_modules() -> None:
    missing = [
        f"{doc}: {name}"
        for doc in MODULE_DOCS
        for name in _module_names((DOCS / doc).read_text())
        if not name.endswith(".__init__") and _module_path(name) is None
    ]
    assert not missing, missing


@pytest.mark.parametrize("package", COMPLETE_PACKAGES)
def test_module_tables_list_every_module(package: str) -> None:
    listed = {name for doc in MODULE_DOCS for name in _module_names((DOCS / doc).read_text())}
    unlisted = sorted(_package_modules(package) - listed)
    assert not unlisted, unlisted


def test_extractor_table_matches_the_registry() -> None:
    rows = _extractor_rows((DOCS / "files.md").read_text())
    assert set(rows) == set(REGISTRY)
    problems = [
        p for name, (module, cell) in rows.items() for p in _record_problems(name, module, cell)
    ]
    assert not problems, problems


def test_access_log_phases_are_the_timed_phases() -> None:
    documented = _first_column((DOCS / "audit.md").read_text(), "| Phase | Time spent |")
    assert documented == _timed_phases(SOURCES["graph_cli"]) | {"other"}


def test_access_log_keys_are_the_written_keys() -> None:
    documented = _first_column((DOCS / "audit.md").read_text(), "| Key | Content |")
    assert documented == _access_log_keys()


def test_workspace_sync_actions() -> None:
    documented = _first_column((DOCS / "cli_graph_admin.md").read_text(), "| Action | Meaning |")
    assert documented == _enum("graph-admin", "workspace sync", "graphs", "[]", "action")


def test_project_admin_commands() -> None:
    region = _between(
        _prose((DOCS / "server.md").read_text()),
        "It runs only the commands on one graph (",
        "and sees only",
    )
    named = {span.removeprefix("graph-admin ") for span in _spans(region)}
    assert named == set(PROJECT_COMMANDS)


def _first_cell_spans(text: str, header: str) -> set[str]:
    """Every code span of the first column of the table with ``header``."""
    for before, rows in _tables(text):
        if before.endswith(header):
            return {span for row in rows for span in _spans(row[0])}
    raise AssertionError(f"no table {header!r}")


def test_tool_error_moves_are_the_on_error_values() -> None:
    documented = _first_column((DOCS / "cli_graph.md").read_text(), "| `on_error` | Meaning |")
    assert documented == set(ON_ERROR_VALUES)


def test_tool_keys_are_the_keys_of_a_tool() -> None:
    documented = _first_cell_spans((DOCS / "cli_graph.md").read_text(), "| Tool key | Holds |")
    assert documented == _answer_keys("graph", "describe tools", "tools", "[]")


def test_tool_controls_are_the_controls_of_the_tools() -> None:
    documented = _first_cell_spans(
        (DOCS / "cli_graph.md").read_text(), "| Control | Tool | Meaning |"
    )
    assert documented == {
        describe_tools.LIMIT,
        describe_tools.OFFSET,
        describe_tools.REASON,
        describe_tools.DRY_RUN,
        describe_tools.ENTITY,
        describe_tools.CURSOR,
        describe_tools.PAGE_BYTES_ARG,
    }


def test_the_tool_kinds_are_named_in_the_tool_table() -> None:
    row = next(
        r
        for _, rows in _tables((DOCS / "cli_graph.md").read_text())
        for r in rows
        if r[0] == "`kind`"
    )
    assert set(_spans(row[1])) >= set(describe_tools.KINDS)


def test_meta_commands_are_never_checked() -> None:
    region = _between(_prose((DOCS / "skills.md").read_text()), "- Meta commands (", ")")
    assert {span.removeprefix("graph ") for span in _spans(region)} == _meta_commands()


def test_push_uploads_the_parts_push_reads() -> None:
    """The base file is named in words; every directory push reads is a span."""
    region = _between(
        _prose((DOCS / "server.md").read_text()), "uploads only what push reads of `DIR` (", ")"
    )
    assert "base file" in region
    directories = {part for part in PARTS if part != SOURCE_FILE}
    assert {span.rstrip("/") for span in _spans(region)} == directories


def test_crash_points_exist() -> None:
    values = {point.value for point in CrashPoint}
    named = {
        span
        for path in sorted(DOCS.glob("*.md"))
        for span in _spans(FENCED_BLOCK.sub(" ", path.read_text()))
        if CRASH_POINT.match(span)
    }
    assert named, "the crash point scan found nothing"
    assert named <= values, sorted(named - values)
