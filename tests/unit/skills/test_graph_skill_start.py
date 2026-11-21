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
from support.views import company_model as views_model
from support.views import edited, function_entries, valid_sources

from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.view_compile import compile_views
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
    assert first.startswith(
        "If you have not yet in this session: `graph start --json` (it refreshes"
    )
    assert "\n\nLoad the `graph-cli` skill as well before your first `graph` command" in first
    assert "by `graph start` or `graph graphs --json`" in first
    for rule in (
        "plain command that starts with `graph` and passes `--json`",
        "`error.hint` is the command to run next",
        "Never cut an answer short",
        "`graph <command> --help` lists the options",
        "with `--dry-run`, then run them without",
        "Say why in `--reason`, with how you learned it (in English, whatever language the"
        " values are in), never in a field value",
        "Ask the user before `retract`",
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


def view_catalog(sources: dict[str, str] | None = None) -> dict[str, Any]:
    """The compiled view catalog of the company fixtures (or of ``sources``)."""
    document, problems = compile_views(
        sources or valid_sources(), views_model(), function_entries()
    )
    assert problems == []
    return document


def test_a_graph_with_views_says_where_to_list_them() -> None:
    clause = "The graph also has published views: `graph describe views --graph "
    with_views = skill(company_model(), ACTIONS, actions=action_catalog(), views=view_catalog())
    assert clause + "example_graph`." in with_views
    assert with_views.count("published views") == 1
    plain = skill(company_model(), ACTIONS, actions=action_catalog())
    assert "published views" not in plain and "describe views" not in plain
    # the clause is the only difference: no list of views in level 0
    assert with_views.replace(f" {clause}example_graph`.", "") == plain
    legacy = edited("legacy_unit_overview.yaml", "replaced_by: unit_overview\n", "")
    only_deprecated = view_catalog(
        {"legacy_unit_overview.yaml": legacy["legacy_unit_overview.yaml"]}
    )
    deprecated = skill(company_model(), ACTIONS, actions=action_catalog(), views=only_deprecated)
    assert deprecated == plain  # a deprecated view is not advertised


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
    assert "When you create an entity of a type with names" in listed
    partial = skill(model)  # nas:Book and nas:WebPage have no row of their own
    assert "`nas:Topic`: `label` (main name), `synonyms` (other names)" in partial
    assert "When you create an entity of one of these types" in partial


def test_lookup_before_create_is_said_once_for_a_graph_with_names() -> None:
    """The start block holds the rule; the names block keeps what is specific to the types."""
    named = skill(compiled_model("notes_and_sources"))
    assert "- Before you create an entity, look its name up (`graph lookup '<name>'" in named
    assert named.count("data.semantic.possible_duplicate") == 1
    assert "search for its name" not in named
    unnamed = skill(compiled_model("inventory"))  # no type has a label role
    assert "look its name up" not in unnamed and "possible_duplicate" not in unnamed


def test_consent_names_terminal_states_only_when_a_lifecycle_has_them() -> None:
    with_terminal = skill(compiled_model("inventory"))
    assert (
        "- Ask the user before `retract` and before a `transition` into a terminal state"
        " (`graph describe type` marks them `terminal`)." in with_terminal
    )
    without = skill(compiled_model("staff"))  # lifecycles without terminal states
    assert "- Ask the user before `retract`.\n" in without
    assert "terminal" not in without


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
