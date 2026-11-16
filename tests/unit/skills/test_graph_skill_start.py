"""The generated graph skill works without skill 1 (F42, F74): the ``start`` block carries the
rules of working with ``graph``, the graph language, and only commands and options that exist;
the blocks leave out what another block of the same template already says."""

import json
from typing import Any

import pytest
from support.actions import company_model
from support.actions import valid_sources as action_sources
from support.fixtures import FIXTURES, GOLDENS, compiled_model
from support.functions import valid_sources as function_sources
from support.skills import GRAPH_SKILL_NAME, render_inputs

from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.skills.graph_skill import render_checked
from graph_cli.skills.names import check_names
from graph_cli.skills.template import Template
from graph_cli.skills.tree import SKILL_FILE, read_tree

BLOCKS = FIXTURES / "skills" / "blocks" / GRAPH_SKILL_NAME
FUNCTIONS = FIXTURES / "skills" / "company_functions" / GRAPH_SKILL_NAME
ACTIONS = FIXTURES / "skills" / "company_actions" / GRAPH_SKILL_NAME
TEMPLATE = FIXTURES / "skills" / "template" / GRAPH_SKILL_NAME


def skill(
    model: CompiledModel,
    directory: Any = BLOCKS,
    *,
    language: str | None = "en",
    prefixes: dict[str, str] | None = None,
    **catalogs: Any,
) -> str:
    files, problems = read_tree(directory)
    assert problems == []
    documents, graph = render_inputs(model, **catalogs)
    graph["language"] = language
    graph["alias_prefixes"].update(prefixes or {})
    rendered, problems = render_checked(files, documents, graph, name=GRAPH_SKILL_NAME)
    assert problems == [] and rendered is not None
    return rendered[SKILL_FILE].decode()


def function_catalog() -> dict[str, Any]:
    document, problems = compile_catalog(function_sources(), compiled_model("company", "1.1.0"))
    assert problems == []
    return document


def action_catalog() -> dict[str, Any]:
    document, problems = compile_actions(action_sources(), company_model())
    assert problems == []
    return document


def test_the_start_block_carries_the_working_rules() -> None:
    text = skill(compiled_model("inventory"))
    first = text.split("## Is it available here?\n\n", 1)[1]
    assert first.startswith("Load the `graph-cli` skill as well before your first `graph` command")
    for rule in (
        "plain command that starts with `graph` and passes `--json`",
        "`error.hint` is the command to run next",
        "Never cut an answer short",
        "`graph <command> --help` lists the options",
        "with `--dry-run`, then run them without",
        "Say why in `--reason` (in English, whatever language the values are in)",
        "`--expect <rev>`",
        "A change set for `graph apply` goes into a file in the working directory",
        "Read `data.findings` of every write",
    ):
        assert rule in text, rule
    assert "named function" not in text and "named action" not in text  # no catalogs


def test_functions_and_actions_come_first_when_the_graph_has_them() -> None:
    with_functions = skill(
        compiled_model("company", "1.1.0"), FUNCTIONS, functions=function_catalog()
    )
    assert "run a named function when one fits" in with_functions
    assert "An empty value in a function's answer means nothing is recorded" in with_functions
    with_actions = skill(company_model(), ACTIONS, actions=action_catalog())
    assert "run a named action when one fits" in with_actions
    assert "Otherwise preview `create`" in with_actions


def test_the_module_views_are_the_exception_to_json() -> None:
    modular = skill(compiled_model("company", "1.1.0"), FUNCTIONS)
    assert "(the module views of `graph describe module` read better as text)" in modular
    assert "read better as text" not in skill(compiled_model("inventory"))


def test_the_graph_language_is_named() -> None:
    assert "Text values are written in the graph language `en`." in skill(
        compiled_model("inventory")
    )
    assert "graph language" not in skill(compiled_model("inventory"), language=None)
    tagged = skill(compiled_model("notes_and_sources", "1.2.0"), language="de")
    assert "A field of base `lang_string` or `lang_text` takes another language" in tagged
    assert "without a tag it gets `de`" in tagged
    assert "takes another language" not in skill(compiled_model("notes_and_sources"))


@pytest.mark.parametrize(
    "rendered",
    [
        pytest.param(lambda: skill(compiled_model("inventory")), id="blocks"),
        pytest.param(lambda: skill(compiled_model("notes_and_sources", "1.1.0")), id="files"),
        pytest.param(
            lambda: skill(
                compiled_model("company", "1.1.0"), FUNCTIONS, functions=function_catalog()
            ),
            id="functions",
        ),
        pytest.param(
            lambda: skill(company_model(), ACTIONS, actions=action_catalog()), id="actions"
        ),
    ],
)
def test_generated_text_names_only_real_commands_and_options(rendered: Any) -> None:
    """The names check of curated text, run over a whole render: the generated blocks, which
    publish does not check, name no command or option the program lacks."""
    inventory = json.loads((GOLDENS / "app" / "inventory.json").read_text())
    documents, graph = render_inputs(company_model(), actions=action_catalog())
    as_curated = Template({SKILL_FILE: rendered()}, ())
    problems = check_names(as_curated, documents, graph, inventory)
    assert [p for p in problems if p.rule == "command"] == []


def test_names_point_to_the_type_table_when_it_lists_every_named_type() -> None:
    model = compiled_model("notes_and_sources")
    listed = skill(model, prefixes={"nas:Book": "BOOK", "nas:WebPage": "PAGE"})
    assert "it matches the fields the type table marks main name, other names, hidden names" in (
        listed
    )
    assert "`nas:Topic`: `label` (main name)" not in listed
    assert "`label` (required, unique, main name); `synonyms` (many, other names)" in listed
    assert "Before you create an entity of a type with names" in listed
    partial = skill(model)  # nas:Book and nas:WebPage have no row of their own
    assert "`nas:Topic`: `label` (main name), `synonyms` (other names)" in partial
    assert "Before you create an entity of one of these types" in partial


def test_write_once_fields_are_named_once() -> None:
    with_types = skill(compiled_model("inventory"))
    assert "`serial_number` (unique, write-once)" in with_types
    assert "Write-once on" not in with_types
    files, _ = read_tree(BLOCKS)
    text = files[SKILL_FILE].decode().replace("<!-- graph_cli:block types -->\n", "")
    documents, graph = render_inputs(compiled_model("inventory"))
    rendered, problems = render_checked(
        {**files, SKILL_FILE: text.encode()}, documents, graph, name=GRAPH_SKILL_NAME
    )
    assert problems == [] and rendered is not None
    assert "Write-once on `inv:Item`" in rendered[SKILL_FILE].decode()


def test_relations_carry_their_query_pattern() -> None:
    text = skill(compiled_model("inventory"))
    assert "- `inv:located_in` `(:Item)-[:located_in]->(:Location)` (out 1..1): Where" in text
    assert "As patterns for `graph query`" not in text
