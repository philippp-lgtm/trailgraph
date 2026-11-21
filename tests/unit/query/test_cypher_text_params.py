"""Numbers meant as text: ``--param`` numbers bound as the text written, number literals warned."""

import pytest
from support.fixtures import compiled_model

from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import ResolvedQuery, resolve
from graph_cli.query.cypher.text_params import number_for_text, text_bindings
from graph_cli.query.cypher.values import is_number_text, parameter_value

INV = compiled_model("inventory", "2.1.0")
NAS = compiled_model("notes_and_sources")


def resolved_with(text: str, texts: dict[str, str], model: CompiledModel) -> ResolvedQuery:
    values = {name: parameter_value(raw) for name, raw in texts.items()}
    return resolve(parse(text), model, values)


def bound(text: str, texts: dict[str, str], model: CompiledModel = INV) -> dict[str, object]:
    return dict(text_bindings(resolved_with(text, texts, model), texts))


def warned(text: str, model: CompiledModel = INV) -> list[tuple[str | None, str, object]]:
    return number_for_text(resolve(parse(text), model, {}))


# text_bindings


@pytest.mark.parametrize(
    "where",
    [
        "i.label = $n",
        "$n = i.label",
        "i.label <> $n",
        "i.label STARTS WITH $n",
        "i.label ENDS WITH $n",
        "i.label CONTAINS $n",
        "i.condition = $n",
        "i.status = $n",
        "i.id = $n",
    ],
)
def test_a_number_compared_with_text_fields_is_bound_as_written(where: str) -> None:
    text = f"MATCH (i:Item) WHERE {where} RETURN i"
    assert bound(text, {"n": "2.10"}) == {"n": "2.10"}


def test_an_integer_is_bound_as_written() -> None:
    assert bound("MATCH (i:Item {label: $n}) RETURN i", {"n": "12"}) == {"n": "12"}


def test_surrounding_space_is_not_part_of_the_text() -> None:
    assert bound("MATCH (i:Item {label: $n}) RETURN i", {"n": " 2.1 "}) == {"n": "2.1"}


def test_a_list_of_numbers_in_a_text_field_is_bound_as_a_list_of_texts() -> None:
    text = "MATCH (i:Item) WHERE i.label IN $n RETURN i"
    assert bound(text, {"n": "[2.10, 3, 1e2]"}) == {"n": ["2.10", "3", "1e2"]}


def test_a_number_in_a_text_list_field_is_bound_as_written() -> None:
    text = "MATCH (t:Topic) WHERE $n IN t.synonyms RETURN t"
    assert bound(text, {"n": "7"}, NAS) == {"n": "7"}


def test_an_inline_map_of_a_named_node_counts() -> None:
    assert bound("MATCH (l:Location {code: $n}) RETURN l", {"n": "2.1"}) == {"n": "2.1"}


def test_a_parameter_used_twice_in_comparisons_is_bound_once_as_text() -> None:
    text = "MATCH (i:Item) WHERE i.label = $n OR i.status = $n RETURN i"
    assert bound(text, {"n": "2.1"}) == {"n": "2.1"}


@pytest.mark.parametrize(
    "where",
    [
        "i.quantity = $n",  # an integer field
        "i.weight = $n",  # a decimal field
        "i.acquired_on = $n",  # a date field
        "i.rev = $n",  # a system field that is a number
        "i.label < $n",  # ordering: not a text comparison
        "i.label >= $n",
        "i.label = $n OR i.quantity = $n",  # also compared with a number
    ],
)
def test_a_number_that_meets_a_non_text_field_stays_a_number(where: str) -> None:
    text = f"MATCH (i:Item) WHERE {where} RETURN i"
    assert bound(text, {"n": "2"}) == {}


def test_a_parameter_that_is_also_returned_stays_a_number() -> None:
    text = "MATCH (i:Item) WHERE i.label = $n RETURN i, $n AS n"
    assert bound(text, {"n": "2.1"}) == {}


def test_a_parameter_in_arithmetic_stays_a_number() -> None:
    text = "MATCH (i:Item) WHERE i.label = $n AND i.quantity > $n + 1 RETURN i"
    assert bound(text, {"n": "2"}) == {}


def test_a_parameter_in_a_list_of_the_query_stays_a_number() -> None:
    text = "MATCH (i:Item) WHERE i.label IN [$n, 'a'] RETURN i"
    assert bound(text, {"n": "2"}) == {}


def test_a_parameter_that_the_query_does_not_use_is_left_alone() -> None:
    assert bound("MATCH (i:Item) RETURN i", {"n": "2"}) == {}


def test_fields_of_anonymous_nodes_are_not_looked_up() -> None:
    text = "MATCH (i:Item)-[:located_in]->(:Location {code: $n}) RETURN i"
    assert bound(text, {"n": "2"}) == {}


def test_only_the_parameters_that_qualify_are_rebound() -> None:
    text = "MATCH (i:Item) WHERE i.label = $n AND i.quantity = $q RETURN i"
    assert bound(text, {"n": "2.1", "q": "3"}) == {"n": "2.1"}


# number_for_text


@pytest.mark.parametrize(
    ("where", "field", "literal"),
    [
        ("i.label = 2", "label", 2),
        ("2 = i.label", "label", 2),
        ("i.label = 2.10", "label", "2.10"),
        ("i.label IN [1, 2]", "label", [1, 2]),
        ("i.condition <> 3", "condition", 3),
        ("1 IN i.label", "label", 1),
    ],
)
def test_a_number_literal_compared_with_a_text_field_is_reported(
    where: str, field: str, literal: object
) -> None:
    assert warned(f"MATCH (i:Item) WHERE {where} RETURN i") == [("i", field, literal)]


def test_an_inline_map_literal_is_reported() -> None:
    assert warned("MATCH (l:Location {code: 12}) RETURN l") == [("l", "code", 12)]


def test_the_same_finding_is_reported_once() -> None:
    text = "MATCH (i:Item) WHERE i.label = 2 OR i.label = 2 RETURN i"
    assert warned(text) == [("i", "label", 2)]


def test_distinct_literals_and_fields_are_reported_separately() -> None:
    text = "MATCH (i:Item) WHERE i.label = 2 OR i.label = 3 OR i.status = 2 RETURN i"
    assert warned(text) == [("i", "label", 2), ("i", "label", 3), ("i", "status", 2)]


@pytest.mark.parametrize(
    "where",
    [
        "i.quantity = 2",
        "i.weight = 2.5",
        "i.label = '2'",
        "i.label = true",
        "i.label = null",
        "i.label IN ['1', 2]",
        "i.label IN []",
        "i.quantity = 2 AND i.label = 'a'",
    ],
)
def test_other_comparisons_are_not_reported(where: str) -> None:
    assert warned(f"MATCH (i:Item) WHERE {where} RETURN i") == []


def test_a_number_literal_is_not_reported_for_anonymous_nodes() -> None:
    assert warned("MATCH (i:Item)-[:located_in]->(:Location {code: 2}) RETURN i") == []


# is_number_text


@pytest.mark.parametrize("text", ["2", "-3", "2.10", "1e3", "[1, 2.5]", " 4 "])
def test_numbers_and_lists_of_numbers_are_number_texts(text: str) -> None:
    assert is_number_text(text)


@pytest.mark.parametrize(
    "text", ["abc", '"2.1"', "true", "null", "[]", '[1, "a"]', "[true]", "{}", "NaN", ""]
)
def test_everything_else_is_not(text: str) -> None:
    assert not is_number_text(text)
