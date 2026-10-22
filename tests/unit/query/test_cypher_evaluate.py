"""Expressions and values of the Cypher subset (plan cypher_query.md 3.3)."""

from decimal import Decimal
from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.ast import Pos
from graph_cli.query.cypher.evaluate import Evaluator
from graph_cli.query.cypher.execute import match_rows
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.query.cypher.values import Node, Rel, equals, group_key, order_key

NAS = compiled_model("notes_and_sources")
INV = compiled_model("inventory", "2.1.0")
N = Node(revision("n", "nas:Book", {"title": "T"}))


def value(text: str, **params: Any) -> Any:
    """``text`` evaluated with ``n`` bound to a book."""
    resolved = resolve(parse(f"MATCH (n:Book) RETURN {text}"), NAS, params)
    evaluator = Evaluator(NAS, resolved.parameters, lambda part, row: True)
    return evaluator.evaluate(resolved.query.returns.projection.items[0].expr, {"n": N})


def failure(text: str) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        value(text)
    return caught.value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true AND null", None),
        ("false AND null", False),
        ("null AND false", False),
        ("true OR null", True),
        ("false OR null", None),
        ("null OR true", True),
        ("true XOR null", None),
        ("true XOR false", True),
        ("true XOR true", False),
        ("NOT null", None),
        ("NOT false", True),
        ("null = null", None),
        ("null <> 1", None),
        ("1 = 1.0", True),
        ("1 = '1'", False),
        ("1 <> '1'", True),
        ("true = 1", False),
        ("1 < 'a'", None),
        ("'a' < 'b'", True),
        ("false < true", True),
        ("2 >= 2.0", True),
        ("[1, 2] = [1, 2]", True),
        ("[1, null] = [1, 2]", None),
        ("[1, null] = [2, 2]", False),
        ("[1] = [1, 2]", False),
        ("{a: 1} = {a: 1}", True),
        ("{a: 1} = {b: 1}", False),
        ("n = n", True),
        ("n.title = 'T'", True),
        ("n.publisher IS NULL", True),
        ("n.title IS NOT NULL", True),
        ("2 IN [1, 2]", True),
        ("3 IN [1, null]", None),
        ("3 IN [1, 2]", False),
        ("null IN []", False),
        ("null IN [1]", None),
        ("1 IN null", None),
        ("'abc' STARTS WITH 'ab'", True),
        ("'abc' ENDS WITH 'bc'", True),
        ("'abc' CONTAINS 'x'", False),
        ("1 STARTS WITH 'a'", None),
        ("null CONTAINS 'a'", None),
        ("1 < 2 < 3", True),
        ("3 > 2 > 2", False),
    ],
)
def test_three_valued_logic_and_comparisons(text: str, expected: bool | None) -> None:
    assert value(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 + 2", 3),
        ("7 / 2", 3),
        ("-7 / 2", -3),
        ("7 % 3", 1),
        ("-7 % 3", -1),
        ("7 / 2.0", Decimal("3.5")),
        ("0.1 + 0.2", Decimal("0.3")),
        ("2 * 1.5", Decimal("3.0")),
        ("-(1)", -1),
        ("+1", 1),
        ("'a' + 'b'", "ab"),
        ("[1] + [2]", [1, 2]),
        ("[1] + 2", [1, 2]),
        ("0 + [1]", [0, 1]),
        ("null + 1", None),
        ("1 - null", None),
    ],
)
def test_arithmetic(text: str, expected: Any) -> None:
    result = value(text)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1 / 0", "division by zero"),
        ("1 % 0", "division by zero"),
        ("1.0 / 0", "division by zero"),
        ("9223372036854775807 + 1", "integer overflow"),
        ("'a' - 1", "- needs numbers, got a string and a number"),
        ("-'a'", "unary - needs a number, got a string"),
        ("1 AND true", "expected a boolean, got a number"),
        ("NOT 'x'", "expected a boolean, got a string"),
        ("1 IN 2", "IN needs a list, got a number"),
        ("toLower(1)", "tolower() needs a string, got a number"),
        ("size(1)", "size() needs a list or a string"),
        ("labels('x')", "labels() needs a node, got a string"),
        ("type(n)", "type() needs a relationship, got a node"),
        ("'x'.title", "a string has no property 'title'"),
    ],
)
def test_runtime_errors(text: str, message: str) -> None:
    error = failure(text)
    assert error.code == "query.invalid_value"
    assert message in error.message
    assert error.details["line"] == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("labels(n)", ["nas:Book", "nas:Source"]),
        ("size('abc')", 3),
        ("size([1, 2])", 2),
        ("size(null)", None),
        ("coalesce(null, n.publisher, 'x', 'y')", "x"),
        ("coalesce(null)", None),
        ("toLower('AbC')", "abc"),
        ("toUpper('AbC')", "ABC"),
        ("trim('  a ')", "a"),
        ("CASE WHEN false THEN 1 WHEN null THEN 2 ELSE 3 END", 3),
        ("CASE WHEN 1 = 1 THEN 'yes' END", "yes"),
        ("CASE 2 WHEN 1 THEN 'one' WHEN 2 THEN 'two' END", "two"),
        ("CASE null WHEN null THEN 'null' ELSE 'other' END", "other"),
        ("CASE 1 WHEN 2 THEN 'x' END", None),
        ("n:Book", True),
        ("n:Source", True),
        ("n:Note|Book", True),
        ("n:Note", False),
        ("{a: n.title, b: [1]}", {"a": "T", "b": [1]}),
        ("$p", {"k": [1, "x"]}),
        ("n.id", str(N.id)),
        ("n.type", "nas:Book"),
        ("n.rev", 1),
        ("n.created_at", "2026-01-01T00:00:00.000Z"),
    ],
)
def test_functions_and_values(text: str, expected: Any) -> None:
    assert value(text, p={"k": [1, "x"]}) == expected


def test_parameters_take_json() -> None:
    assert value("$p.k", p={"k": 3}) == 3


def test_decimal_attributes_compare_as_numbers() -> None:
    snapshot = DictSnapshot(
        revision("light", "inv:Item", {"label": "light", "weight": "2.5"}),
        revision("heavy", "inv:Item", {"label": "heavy", "weight": "10"}),
    )
    resolved = resolve(parse("MATCH (i:Item) WHERE i.weight > 3 RETURN 1"), INV, {})
    (row,) = match_rows(resolved, INV, snapshot)
    assert isinstance(row["i"], Node) and row["i"].revision.attrs["label"] == "heavy"
    resolved = resolve(parse("MATCH (i:Item) WHERE i.weight = 2.50 RETURN 1"), INV, {})
    assert len(match_rows(resolved, INV, snapshot)) == 1


def test_relationship_fields() -> None:
    source = revision("note", "nas:Note", {}, [edge("nas:cites", "book", locator="p. 1")])
    rel = Rel(source.id, source.edges[0])
    evaluator = Evaluator(NAS, {}, lambda part, row: True)
    assert evaluator.rel_field(rel, "locator") == "p. 1"
    assert evaluator.rel_field(rel, "edge_id") == str(source.edges[0].edge_id)
    assert evaluator.rel_field(rel, "nothing") is None
    assert evaluator.function("type", [rel], Pos(1, 1, 0)) == "nas:cites"


def test_order_and_group_keys() -> None:
    values = [None, 2, "b", True, [1], {"a": 1}, N, Decimal("1.5"), "a", False]
    ordered = sorted(values, key=order_key)
    assert ordered == [{"a": 1}, N, [1], "a", "b", False, True, Decimal("1.5"), 2, None]
    assert group_key(1) == group_key(Decimal("1.0"))
    assert group_key(1) != group_key(True)
    assert group_key(None) == group_key(None)
    assert equals(N, Node(N.revision)) is True
