"""The comparison walk: which fields a query sets against a parameter or a constant."""

import pytest

from graph_cli.query.cypher.ast import Const, ListExpr, Parameter
from graph_cli.query.cypher.compare import Comparison, comparisons
from graph_cli.query.cypher.parser import parse


def found(text: str, *, literals: bool = False) -> list[Comparison]:
    return list(comparisons(parse(text), literals=literals))


def summary(text: str, *, literals: bool = False) -> list[tuple[str | None, str, str, str]]:
    return [(c.variable, c.field, c.shape, c.op) for c in found(text, literals=literals)]


@pytest.mark.parametrize("op", ["=", "<>", "<", "<=", ">", ">="])
def test_a_value_comparison_names_the_field_and_the_operator(op: str) -> None:
    assert summary(f"MATCH (a) WHERE a.x {op} $p RETURN a") == [("a", "x", "value", op)]


def test_the_parameter_may_stand_on_either_side() -> None:
    [use] = found("MATCH (a) WHERE $p = a.x RETURN a")
    assert (use.variable, use.field, use.shape, use.op) == ("a", "x", "value", "=")
    assert isinstance(use.value, Parameter) and use.value.name == "p"


def test_in_with_a_list_parameter_and_in_with_a_list_field() -> None:
    assert summary("MATCH (a) WHERE a.x IN $p RETURN a") == [("a", "x", "in_param", "in")]
    assert summary("MATCH (a) WHERE $p IN a.xs RETURN a") == [("a", "xs", "in_field", "in")]


@pytest.mark.parametrize(
    ("keyword", "op"),
    [("STARTS WITH", "starts_with"), ("ENDS WITH", "ends_with"), ("CONTAINS", "contains")],
)
def test_text_matching_is_its_own_shape(keyword: str, op: str) -> None:
    assert summary(f"MATCH (a) WHERE a.x {keyword} $p RETURN a") == [("a", "x", "text", op)]


def test_text_matching_with_the_field_on_the_right_is_not_a_comparison() -> None:
    assert summary("MATCH (a) WHERE $p CONTAINS a.x RETURN a") == []


def test_an_inline_map_of_a_named_node_is_a_value_comparison_with_its_node() -> None:
    [use] = found("MATCH (a:T {x: $p}) RETURN a")
    assert (use.variable, use.field, use.shape, use.op) == ("a", "x", "value", "=")
    assert use.node is not None and use.node.variable == "a"


def test_an_inline_map_of_an_anonymous_node_keeps_the_node_for_the_caller() -> None:
    [use] = found("MATCH (a)-[:R]->(:T {x: $p}) RETURN a")
    assert use.variable is None
    assert use.node is not None and use.node.labels == (("T",),)


def test_an_inline_map_of_a_relationship_has_no_node() -> None:
    [use] = found("MATCH (a)-[r:R {w: $p}]->(b) RETURN a")
    assert (use.variable, use.field) == ("r", "w")
    assert use.node is None


def test_patterns_used_as_predicates_are_walked() -> None:
    text = "MATCH (a) WHERE (a)-[:R]->(:T {x: $p}) RETURN a"
    assert [(c.field, c.shape) for c in found(text)] == [("x", "value")]


def test_comparisons_come_in_clause_order_expressions_before_patterns() -> None:
    text = "MATCH (a:T {x: $one}) WHERE a.y = $two WITH a WHERE a.z IN $three RETURN a"
    assert [c.field for c in found(text)] == ["y", "x", "z"]


def test_other_uses_of_a_field_or_a_parameter_are_not_comparisons() -> None:
    text = (
        "MATCH (a) WHERE a.x + 1 = $p AND a.y = $q + 1 AND a.z = toString($r) "
        "AND size(a.w) > 0 AND a.v = a.u RETURN a.x AS k, $s AS t"
    )
    assert found(text) == []


def test_constants_are_left_out_unless_asked_for() -> None:
    text = "MATCH (a:T {k: 'v'}) WHERE a.x = 2.1 AND a.y IN [1, 2] AND 'z' IN a.zs RETURN a"
    assert found(text) == []


def test_constants_are_comparisons_with_the_flag() -> None:
    text = "MATCH (a:T {k: 'v'}) WHERE a.x = 2.1 AND a.y IN [1, 2] AND 'z' IN a.zs RETURN a"
    uses = found(text, literals=True)
    assert [(c.field, c.shape, c.op) for c in uses] == [
        ("x", "value", "="),
        ("y", "in_param", "in"),
        ("zs", "in_field", "in"),
        ("k", "value", "="),
    ]
    assert all(isinstance(c.value, Const | ListExpr) for c in uses)
    assert isinstance(uses[1].value, ListExpr)


def test_a_list_with_a_parameter_inside_is_not_a_constant_list() -> None:
    text = "MATCH (a) WHERE a.x IN [1, $p] RETURN a"
    assert found(text, literals=True) == []


def test_parameters_and_constants_come_together_with_the_flag() -> None:
    text = "MATCH (a) WHERE a.x = $p AND a.y STARTS WITH 'ab' RETURN a"
    uses = found(text, literals=True)
    assert [type(c.value) for c in uses] == [Parameter, Const]
    assert [c.op for c in uses] == ["=", "starts_with"]
