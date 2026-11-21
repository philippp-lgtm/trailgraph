"""The description of a generated graph skill points to ``graph start`` (graph start plan S7)."""

from support.fixtures import FIXTURES, compiled_model
from support.skills import GRAPH_SKILL_NAME, render_inputs

from graph_cli.skills.graph_skill import measure, render_checked
from graph_cli.skills.graph_skill.description import (
    START_SENTENCE,
    description_of,
    with_start_sentence,
)
from graph_cli.skills.template import parse_template
from graph_cli.skills.tree import MAX_DESCRIPTION, SKILL_FILE, read_tree

TEMPLATE = FIXTURES / "skills" / "template" / GRAPH_SKILL_NAME
BODY = ["---", "name: graph-example-graph", "{description}", "metadata:", "  a: b", "---", "", "x"]


def lines(description: str) -> list[str]:
    return [line.replace("{description}", description) for line in BODY]


def test_the_sentence_is_appended_to_every_form_of_a_description() -> None:
    for form in (
        'description: "Items and places. Needs the graph-cli skill."',
        "description: Items and places",
        "description: 'Items and places, don''t guess'",
        "description: >-\n  Items and places,\n  and their stock.",
        "description: |\n  Items and places.",
    ):
        made = with_start_sentence("\n".join(lines(form)).split("\n"))
        found = description_of(made)
        assert found is not None and found.endswith(START_SENTENCE), (form, made)
        assert made[0] == "---" and made[-2:] == ["", "x"]
        assert "name: graph-example-graph" in made and "metadata:" in made  # the rest is kept


def test_a_description_that_names_graph_start_is_left_alone() -> None:
    mine = lines('description: "Items. Run `graph start` once, then load this."')
    assert with_start_sentence(mine) == mine
    assert with_start_sentence(["no frontmatter"]) == ["no frontmatter"]


def test_a_description_without_room_stays_as_written() -> None:
    long = "x" * (MAX_DESCRIPTION - len(START_SENTENCE) - 2)  # still fits, by one character
    assert len(long) + 1 + len(START_SENTENCE) == MAX_DESCRIPTION - 1
    fits = lines(f'description: "{long}"')
    assert description_of(with_start_sentence(fits)) == f"{long} {START_SENTENCE}"
    tight = lines(f'description: "{long}xx"')
    assert with_start_sentence(tight) == tight  # the sentence would pass the limit


def template_with(description: str) -> dict[str, bytes]:
    files, problems = read_tree(TEMPLATE)
    assert problems == []
    text = files[SKILL_FILE].decode()
    old = next(line for line in text.splitlines() if line.startswith("description:"))
    files[SKILL_FILE] = text.replace(old, f"description: {description}").encode()
    return files


def test_a_render_carries_the_sentence_and_stays_within_the_limit() -> None:
    documents, graph = render_inputs(compiled_model("inventory"))
    rendered, problems = render_checked(
        template_with('"Items. Needs the graph-cli skill."'),
        documents,
        graph,
        name=GRAPH_SKILL_NAME,
    )
    assert problems == [] and rendered is not None
    found = description_of(rendered[SKILL_FILE].decode().split("\n"))
    assert found == f"Items. Needs the graph-cli skill. {START_SENTENCE}"
    # a template that already says it renders as written
    again, _ = render_checked(
        template_with('"Items. Run `graph start` first."'), documents, graph, name=GRAPH_SKILL_NAME
    )
    assert again is not None
    assert (
        description_of(again[SKILL_FILE].decode().split("\n")) == "Items. Run `graph start` first."
    )


def test_a_description_that_cannot_take_the_sentence_renders_and_is_reported() -> None:
    documents, graph = render_inputs(compiled_model("inventory"))
    long = "y" * (MAX_DESCRIPTION - 10)
    files = template_with(f'"{long}"')
    rendered, problems = render_checked(files, documents, graph, name=GRAPH_SKILL_NAME)
    assert problems == [] and rendered is not None  # never refused for this
    assert description_of(rendered[SKILL_FILE].decode().split("\n")) == long
    template, _ = parse_template(files)
    assert template is not None
    size = measure(template, documents, graph)
    assert size.description_over and size.over and size.description == len(long)
    assert size.to_json()["start_sentence"] == START_SENTENCE
    fine = parse_template(template_with('"Short."'))[0]
    assert fine is not None
    assert not measure(fine, documents, graph).description_over
