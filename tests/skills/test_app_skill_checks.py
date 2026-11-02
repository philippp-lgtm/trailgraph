"""D, L, S: independent checks of the committed skill 1 (B11.9, B11.4, B11.7, item 7 S4).

- ``TestDocsVsCode`` (B11.9): the tables and lists of the committed Markdown agree with the
  inventory, read by a Markdown table parser that does not share code with the renderer.
  They use the committed inventory golden, which ``test_app_meta.py`` proves equal to the
  live one; ``test_app_skill.py`` proves the committed skill equal to its render.
- ``TestLeaks`` (B11.4, B11.7): ``leaks`` finds fixture vocabulary, maintenance surface,
  non-placeholder tokens, paths, UUIDs and non-ASCII text; positive controls plant each class.
- ``TestStructure``: frontmatter, size budgets and links of ``skills/graph-cli/``.
"""

import json
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import FIXTURES, GOLDENS
from support.skills import SKILL_DIR, committed_files
from test_vocabulary_leak import deny_list

from graph_cli.cli_kit.envelope import envelope, error_json
from graph_cli.kernel.changeset import (
    DOCUMENT_KEYS,
    MAX_BYTES,
    MAX_OPS,
    OP_KEYS,
    parse_changeset,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.resolve import FUNCTIONS
from graph_cli.skills.app_skill import QUERY_SUBSET, render_app_skill
from graph_cli_admin.maintenance_cli.commands import all_specs as admin_specs

Table = tuple[list[str], list[list[str]]]

ALLOWED_PLACEHOLDERS = {
    "<graph>",
    "<type>",
    "<id>",
    "<attr>",
    "<value>",
    "<relation>",
    "<module>",
    "<function>",
    "<action>",
    "<param>",
    "<state>",
    "<file>",
    "<json>",
    "<seq>",
    "<dir>",
}


def golden_inventory() -> dict[str, Any]:
    inventory: dict[str, Any] = json.loads((GOLDENS / "app" / "inventory.json").read_text())
    return inventory


# --- independent Markdown reading ------------------------------------------------------


def split_row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
    return [c.strip().replace("\\|", "|").strip("`") for c in cells]


def tables(text: str) -> list[Table]:
    """Every pipe table: header cells and data rows (separator row dropped)."""
    found: list[Table] = []
    block: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|"):
            block.append(line)
            continue
        if len(block) >= 2:
            assert set(block[1]) <= set("|-"), block[1]
            found.append((split_row(block[0]), [split_row(r) for r in block[2:]]))
        block = []
    return found


def table_with(text: str, first_header: str) -> Table:
    (match,) = [t for t in tables(text) if t[0][0] == first_header]
    return match


def sections(text: str) -> dict[str, str]:
    """``{heading: body}`` of the ``## `` sections."""
    parts = re.split(r"^## (.*)$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def backticked(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def yes(value: bool) -> str:
    return "yes" if value else "no"


def described(param: dict[str, Any]) -> str:
    text = param["summary"]
    if "choices" in param:
        text += " One of " + ", ".join(f"`{c}`" for c in param["choices"]) + "."
    if "default" in param:
        text += f" Default: `{param['default']}`."
    return text


def param_rows(params: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [p["name"], p["type"], yes(p["required"]), yes(p["repeatable"]), described(p)]
        for p in params
    ]


def command_line(command: dict[str, Any]) -> str:
    return " ".join(["graph", *command["path"]])


def sorted_commands(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = sorted(inventory["commands"], key=lambda c: c["path"])
    return commands


class TestDocsVsCode:
    def test_commands_table(self) -> None:
        inventory = golden_inventory()
        _, rows = table_with(committed_files()["SKILL.md"], "Command")
        assert rows == [
            [command_line(c), c["kind"], c["summary"]] for c in sorted_commands(inventory)
        ]

    def test_global_options_table(self) -> None:
        _, rows = table_with(committed_files()["SKILL.md"], "Option")
        assert rows == [
            [o["name"], o["type"], o["summary"]] for o in golden_inventory()["global_options"]
        ]

    def test_exit_codes_point_to_the_errors_reference(self) -> None:
        exits = sections(committed_files()["SKILL.md"])["Exit codes"]
        assert "[references/errors.md](references/errors.md)" in exits
        assert "`0` is success" in exits

    def test_command_sections(self) -> None:
        inventory = golden_inventory()
        by_heading = sections(committed_files()["references/commands.md"])
        assert list(by_heading) == [command_line(c) for c in sorted_commands(inventory)]
        for command in sorted_commands(inventory):
            body = by_heading[command_line(command)]
            found = tables(body)
            expected: list[tuple[str, list[list[str]]]] = []
            if command["args"]:
                expected.append(("Argument", param_rows(command["args"])))
            if command["options"]:
                expected.append(("Option", param_rows(command["options"])))
            if command["limits"]:
                expected.append(
                    ("Limit", [[k, str(v)] for k, v in sorted(command["limits"].items())])
                )
            expected.append(
                ("Example", [[e["command"], e["summary"]] for e in command["examples"]])
            )
            assert [(h[0], rows) for h, rows in found] == expected, command_line(command)
            codes = ", ".join(str(e) for e in command["exit_codes"])
            assert f"Exit codes: {codes}." in body, command_line(command)

    def test_errors_tables(self) -> None:
        codes = golden_inventory()["error_codes"]
        text = committed_files()["references/errors.md"]
        _, rows = table_with(text, "Error code")
        assert rows == [[e["code"], e["category"], str(e["exit"])] for e in codes]
        _, todo = table_with(text, "Error category")
        exits = {e["category"]: str(e["exit"]) for e in codes}
        assert {row[0]: row[1] for row in todo} == exits
        assert len(todo) == len(exits) and all(row[2] for row in todo)

    def test_change_set_tables(self) -> None:
        text = committed_files()["references/changesets.md"]
        _, document = table_with(text, "Key")
        required, optional = DOCUMENT_KEYS
        assert [row[:2] for row in document] == [
            [k, yes(k in required)] for k in (*required, *optional)
        ]
        _, ops = table_with(text, "Op")
        assert [
            (
                row[0],
                [c.strip("`") for c in row[1].split(", ") if c],
                [c.strip("`") for c in row[2].split(", ") if c],
            )
            for row in ops
        ] == [(op, list(keys[0]), list(keys[1])) for op, keys in OP_KEYS.items()]
        assert all(row[-1] for row in document + ops)
        assert f"At most {MAX_OPS} operations and {MAX_BYTES // 2**20} MiB" in text

    def test_change_set_ops_are_the_ones_the_parser_takes(self) -> None:
        with pytest.raises(GraphCliError) as caught:
            parse_changeset({"ops": [{"op": "novel"}]})
        assert caught.value.details["expected"] == sorted(OP_KEYS)

    def test_change_set_example_parses(self) -> None:
        text = committed_files()["references/changesets.md"]
        example = text.split("```json\n")[1].split("```")[0]
        for placeholder, value in {
            "<type>": "ex:Thing",
            "<relation>": "ex:rel",
            "<attr>": "name",
            "<value>": "x",
            "<id>": "ALIAS-0001",
        }.items():
            example = example.replace(placeholder, value)
        changeset = parse_changeset(json.loads(example))
        assert [op.OP for op in changeset.ops] == ["create", "create", "link", "update"]

    def test_envelope_keys(self) -> None:
        body = committed_files()["SKILL.md"]
        (keys_line,) = [x for x in body.splitlines() if x.startswith("- Keys, always present:")]
        assert backticked(keys_line) == list(envelope(None, program="graph").keys())
        marker = "On failure `error` holds"
        (fail_line,) = [x for x in body.splitlines() if marker in x]
        listed = fail_line.partition(marker)[2].partition(". ")[0]
        failure = error_json(GraphCliError("internal.error", "x"), program="graph")
        assert backticked(listed) == list(failure.keys())

    def test_graph_mentions_resolve(self) -> None:
        paths = [tuple(c["path"]) for c in golden_inventory()["commands"]]
        for name, text in committed_files().items():
            for span in backticked(text):
                for match in re.finditer(r"(?<![-\w<])graph((?: [^\s`]+)*)", span):
                    words: list[str] = []
                    for token in match.group(1).split():
                        if token[0] in "-<['\"" or token[0].isupper() or "=" in token:
                            break
                        words.append(token)
                    assert any(p[: len(words)] == tuple(words) for p in paths), (name, span)


# --- leaks -----------------------------------------------------------------------------


@cache
def vocabulary() -> tuple[set[str], set[str]]:
    return deny_list()


def admin_only_names() -> set[str]:
    app_top = {c["path"][0] for c in golden_inventory()["commands"]}
    return {spec.path[0] for spec in admin_specs()} - app_top


def leaks(files: dict[str, str]) -> list[str]:
    """``"<class>: <file>: <match>"`` for every leak in ``files``."""
    words, prefixes = vocabulary()
    admin = admin_only_names()
    found: list[str] = []

    def scan(kind: str, pattern: str, text: str, name: str) -> None:
        found.extend(f"{kind}: {name}: {m.group(0)}" for m in re.finditer(pattern, text))

    for name, text in files.items():
        for word in sorted(words):
            scan("vocabulary", rf"\b{re.escape(word)}\b", text, name)
        for prefix in sorted(prefixes):
            scan("curie", rf"\b{re.escape(prefix)}:", text, name)
        scan("maintenance", r"maint\.|admin\.|graph-admin|--data-root|GRAPH_CLI_ROOT", text, name)
        for command in sorted(admin):
            for span in [*backticked(text), *re.findall(r"^## (.*)$", text, re.MULTILINE)]:
                scan("admin command", rf"(?<![-\w<])graph {re.escape(command)}\b", span, name)
        no_banner = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        found.extend(
            f"placeholder: {name}: {t}"
            for t in re.findall(r"<[a-z_]+>", no_banner)
            if t not in ALLOWED_PLACEHOLDERS
        )
        scan("path", r"/Users/|/home/|~/|C:\\", text, name)
        scan("uuid", r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b", text, name)
        found.extend(f"non-ascii: {name}: {ch!r}" for ch in sorted(set(text)) if ord(ch) >= 128)
    return found


class TestLeaks:
    def test_committed_skill_has_no_leaks(self) -> None:
        assert leaks(committed_files()) == []

    def test_inventory_examples_use_allowed_placeholders_only(self) -> None:
        used: set[str] = set()
        for command in golden_inventory()["commands"]:
            for example in command["examples"]:
                used.update(re.findall(r"<[a-z_]+>", example["command"]))
        assert used <= ALLOWED_PLACEHOLDERS

    def test_admin_commands_are_known(self) -> None:
        assert admin_only_names() >= {"init", "ontology", "audit"}

    @pytest.mark.parametrize(
        ("kind", "plant"),
        [
            ("vocabulary", lambda w, p: f"uses {sorted(w)[0]} here"),
            ("curie", lambda w, p: f"type {sorted(p)[0]}:Thing"),
            ("maintenance", lambda w, p: "run graph-admin"),
            ("maintenance", lambda w, p: "action maint.x"),
            ("maintenance", lambda w, p: "code admin.x"),
            ("maintenance", lambda w, p: "--data-root"),
            ("admin command", lambda w, p: "`graph init`"),
            ("placeholder", lambda w, p: "`graph get <bogus>`"),
            ("path", lambda w, p: "see /home/x"),
            ("uuid", lambda w, p: "id 123e4567-e89b-12d3-a456-426614174000"),
            ("non-ascii", lambda w, p: "caf\u00e9"),
        ],
    )
    def test_positive_control(self, kind: str, plant: Callable[[set[str], set[str]], str]) -> None:
        files = render_app_skill(golden_inventory())
        assert leaks(files) == []
        files["SKILL.md"] += "\n" + plant(*vocabulary()) + "\n"
        assert any(x.startswith(kind + ":") for x in leaks(files))

    @pytest.mark.parametrize("source", ["graph-example-graph", "template/graph-example-graph"])
    def test_emit_output_has_no_leaks(
        self, run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, source: str
    ) -> None:
        """D15: every ``graph skills emit`` output, skill 1 and a published graph skill,
        static or generated from a template. A graph skill names its graph's model on
        purpose, so vocabulary and CURIEs are allowed there; the publish refusals of
        ``graph-admin`` are the positive controls (``tests/cli/test_admin_skill.py``)."""
        init(run_cli, isolated_env)
        create_graph(run_cli, "example_graph")
        directory = FIXTURES / "skills" / source
        assert (
            admin(run_cli, "graph", "skill", "publish", "example_graph", str(directory)).code == 0
        )
        out = tmp_path / "out"
        out.mkdir()
        outcome = app(run_cli, "skills", "emit", "--out", str(out), "--graph", "example_graph")
        assert outcome.code == 0, outcome.envelope
        emitted = {skill.name: committed_files(skill) for skill in sorted(out.iterdir())}
        assert sorted(emitted) == ["graph-cli", "graph-example-graph"]
        assert leaks(emitted["graph-cli"]) == []
        graph_leaks = leaks(emitted["graph-example-graph"])
        assert [x for x in graph_leaks if not x.startswith(("vocabulary:", "curie:"))] == []
        assert graph_leaks, "positive control: the graph skill does name its model"


# --- structure -------------------------------------------------------------------------


def frontmatter() -> dict[str, Any]:
    text = committed_files()["SKILL.md"]
    assert text.startswith("---\n")
    loaded = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(loaded, dict)
    return loaded


class TestStructure:
    def test_frontmatter(self) -> None:
        meta = frontmatter()
        assert set(meta) <= {
            "name",
            "description",
            "license",
            "compatibility",
            "metadata",
            "allowed-tools",
        }
        assert meta["name"] == SKILL_DIR.name
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"]) and len(meta["name"]) <= 64
        description = meta["description"]
        assert isinstance(description, str) and 200 <= len(description) <= 1024
        assert "<" not in description and ">" not in description
        metadata = meta["metadata"]
        assert isinstance(metadata, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())
        assert {"inventory_hash", "program_version", "generator"} <= set(metadata)

    def test_size_budgets(self) -> None:
        files = committed_files()
        assert (
            len(files["SKILL.md"].splitlines()) <= 219
        )  # functions: command rows, guidance; lookup --for; actions: the do row
        for name, text in files.items():
            if name.startswith("references/"):
                assert len(text.encode()) <= 40 * 1024, name

    def test_references_are_linked_both_ways(self) -> None:
        files = committed_files()
        linked = set(re.findall(r"\]\((references/[^)]+)\)", files["SKILL.md"]))
        assert all((SKILL_DIR / link).is_file() for link in linked)
        assert linked == {n for n in files if n.startswith("references/")}


def test_query_functions_are_the_ones_the_resolver_accepts() -> None:
    (functions,) = [parts for name, parts in QUERY_SUBSET if name == "Functions"]
    listed = {re.sub(r"\(.*", "", f).lower() for f in functions}
    assert listed == set(FUNCTIONS)
