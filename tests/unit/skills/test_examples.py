"""The ``graph`` command lines of a curated skill text: recipes and generic examples."""

import json
from typing import Any

from support.fixtures import GOLDENS, compiled_model
from support.skills import render_inputs

from graph_cli.ontology_layer.vocabulary import SYSTEM_FIELDS as MODEL_SYSTEM_FIELDS
from graph_cli.skills.examples import (
    SYSTEM_FIELDS,
    CommandExample,
    command_examples,
    generic_examples,
    model_terms,
    query_recipes,
)
from graph_cli.skills.template import parse_template

FRONT = '---\nname: graph-example-graph\ndescription: "Example."\n---\n\n'
INVENTORY: dict[str, Any] = json.loads((GOLDENS / "app" / "inventory.json").read_text())
DOCUMENTS, GRAPH = render_inputs(compiled_model("inventory"))
TERMS = model_terms(DOCUMENTS)


def examples(body: str, extra: dict[str, str] | None = None) -> list[CommandExample]:
    files = {"SKILL.md": (FRONT + body).encode()}
    files.update({name: text.encode() for name, text in (extra or {}).items()})
    template, broken = parse_template(files)
    assert broken == [] and template is not None
    return command_examples(template)


def lines(found: list[CommandExample]) -> list[tuple[str, int, tuple[str, ...]]]:
    return [(e.path, e.line, e.words) for e in found]


def test_the_system_fields_equal_the_ontology_vocabulary() -> None:
    assert SYSTEM_FIELDS == MODEL_SYSTEM_FIELDS


def test_a_fenced_command_with_continuations_is_one_example() -> None:
    body = "```sh\ngraph find --type inv:Item \\\n  --graph example_graph --json\n```\n"
    (found,) = examples(body)
    assert found.words == ("find", "--type", "inv:Item", "--graph", "example_graph", "--json")
    assert found.fenced and (found.path, found.line) == ("SKILL.md", 7)


def test_a_quoted_word_over_several_lines_is_one_word() -> None:
    body = "```sh\ngraph query 'MATCH (n:inv:Item)\nWHERE n.name = $name\nRETURN n' --json\n```\n"
    (found,) = examples(body)
    assert found.words[0] == "query" and "WHERE n.name = $name" in found.words[1]
    assert found.words[2:] == ("--json",)


def test_inline_code_is_an_example_but_not_fenced() -> None:
    (found,) = examples("Run `graph lookup 'Some name'` first.\n")
    assert found.words == ("lookup", "Some name") and not found.fenced and found.line == 6


def test_commands_without_their_input_in_the_line_are_skipped() -> None:
    body = (
        "```sh\ngraph apply - --json <<'JSON'\n{\"ops\": []}\nJSON\n"
        "graph query --file q.cypher --json\ngraph find --type inv:Item\n```\n"
    )
    assert [e.words[0] for e in examples(body)] == ["find"]


def test_the_word_graph_inside_a_quoted_value_is_not_a_command() -> None:
    body = "```sh\ngraph create inv:Item --reason 'learned from graph find --help' --json\n```\n"
    assert [e.words[0] for e in examples(body)] == ["create"]


def test_marker_lines_and_the_other_curated_files_are_read() -> None:
    body = "<!-- graph_cli:block types -->\n`graph get ITEM-0001`\n"
    found = examples(body, {"references/more.md": "```\ngraph describe types\n```\n"})
    assert lines(found) == [
        ("SKILL.md", 7, ("get", "ITEM-0001")),
        ("references/more.md", 2, ("describe", "types")),
    ]


def recipes(body: str) -> list[tuple[str, dict[str, str]]]:
    return [(r.text, dict(r.params)) for r in query_recipes(examples(body), INVENTORY)]


def test_a_recipe_is_the_cypher_word_and_the_param_texts() -> None:
    body = (
        "```sh\ngraph query 'MATCH (n:inv:Item) WHERE n.name = $name RETURN n' \\\n"
        "  --param name=Bolt --param tags=[1,2] --limit 5 --json\n"
        "graph query --explain 'MATCH (n:inv:Item) RETURN n' --graph=example_graph\n```\n"
    )
    assert recipes(body) == [
        ("MATCH (n:inv:Item) WHERE n.name = $name RETURN n", {"name": "Bolt", "tags": "[1,2]"}),
        ("MATCH (n:inv:Item) RETURN n", {}),
    ]


def test_a_query_example_without_a_text_is_no_recipe() -> None:
    assert recipes("```sh\ngraph query --json\n```\n") == []


def generic(body: str) -> list[tuple[str, ...]]:
    return [e.words for e in generic_examples(examples(body), TERMS, "example_graph", INVENTORY)]


def test_an_example_that_names_nothing_of_the_graph_is_generic() -> None:
    body = (
        "```sh\ngraph describe types --json\ngraph lookup '<name>' --json\n"
        "graph query 'MATCH (n:<type>) RETURN n' --json\n"
        "graph find --type <type> --graph example_graph\n"
        "graph get ITEM-0001 --graph example_graph\n```\n"
    )
    assert [w[0] for w in generic(body)] == ["describe", "lookup", "query", "find", "get"]


def test_an_example_that_names_a_term_is_specific() -> None:
    body = (
        "```sh\ngraph find --type inv:Item --json\ngraph find --type Location\n"
        "graph query 'MATCH (n:Item) WHERE n.condition = $c RETURN n.name' --param c=worn\n"
        "graph create inv:Item --set status=in_stock --reason 'x' --dry-run\n```\n"
    )
    assert generic(body) == []


def test_an_enum_value_a_state_or_a_field_makes_an_example_specific() -> None:
    assert generic("```sh\ngraph find --where condition=worn\n```\n") == []
    assert generic("```sh\ngraph find --where status=retired\n```\n") == []
    assert generic("```sh\ngraph find --where serial_number=<n>\n```\n") == []


def test_data_values_and_system_fields_are_no_terms() -> None:
    assert generic("```sh\ngraph find --where id=<id> --where alias=ITEM-0001\n```\n") != []
    assert generic("```sh\ngraph get Hammer 42 --graph example_graph\n```\n") != []


def test_inline_code_is_not_judged_as_a_generic_example() -> None:
    assert generic("Use `graph describe types` to see the types.\n") == []


def test_the_terms_hold_what_the_text_can_name() -> None:
    assert {"inv:Item", "Item", "inv:located_in", "located_in", "inventory"} <= TERMS
    assert {"condition", "worn", "status", "in_stock", "serial_number"} <= TERMS
    assert not TERMS & SYSTEM_FIELDS


def test_a_placeholder_the_tool_does_not_use_names_something_of_the_graph() -> None:
    body = (
        "```sh\ngraph file add <file> --title '<Subject> <Kind|Test> <YYYY-MM-DD>'\n"
        "graph history <installation id>\n```\n"
    )
    assert generic(body) == []
    plain = "```sh\ngraph file add <file> --title '<name>' --graph example_graph\n```\n"
    assert [w[0] for w in generic(plain)] == ["file"]
