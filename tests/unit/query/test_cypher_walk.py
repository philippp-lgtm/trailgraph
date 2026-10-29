"""The walks over a query tree: parameters, expressions and patterns of a clause."""

from graph_cli.query.cypher import ParameterUse, parameters
from graph_cli.query.cypher.ast import HasLabels, Parameter, PatternPart
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.walk import clause_expressions, clause_patterns


def names(text: str) -> list[tuple[str, bool]]:
    return [(use.name, use.paging) for use in parameters(parse(text))]


def test_a_query_without_parameters_has_none() -> None:
    assert parameters(parse("MATCH (n) RETURN n")) == ()


def test_parameters_in_every_clause_position() -> None:
    text = (
        "MATCH (a:T {k: $in_node})-[r:R {w: $in_rel}]->(b) "
        "WHERE a.x = $in_where "
        "UNWIND $in_unwind AS u "
        "WITH a, u, $in_with_item AS w WHERE w = $in_with_where "
        "RETURN a.x, $in_return AS r ORDER BY a.x = $in_order"
    )
    assert [n for n, _ in names(text)] == [
        "in_node",
        "in_rel",
        "in_where",
        "in_unwind",
        "in_with_item",
        "in_with_where",
        "in_return",
        "in_order",
    ]


def test_parameters_inside_nested_expressions_and_predicates() -> None:
    text = (
        "MATCH (a) "
        "WHERE (a)-[:R {k: $in_predicate}]->(:T {j: $in_predicate_node}) "
        "AND any(x IN [1, $in_list] WHERE x > $in_quantifier) "
        "AND size([y IN a.l WHERE y = $in_comprehension | y + $in_projection]) > 0 "
        "AND CASE a.x WHEN $in_when THEN $in_then ELSE $in_else END = 1 "
        "AND a.l[$in_index] = 1 AND a.l[$in_start..$in_end] = [] "
        "AND coalesce(a.x, $in_call) IS NOT NULL "
        "RETURN a"
    )
    assert sorted(n for n, _ in names(text)) == sorted(
        [
            "in_predicate",
            "in_predicate_node",
            "in_list",
            "in_quantifier",
            "in_comprehension",
            "in_projection",
            "in_when",
            "in_then",
            "in_else",
            "in_index",
            "in_start",
            "in_end",
            "in_call",
        ]
    )


def test_skip_and_limit_are_marked_as_paging() -> None:
    assert names("MATCH (n) WHERE n.x = $a RETURN n SKIP $s LIMIT $l") == [
        ("a", False),
        ("s", True),
        ("l", True),
    ]
    assert names("MATCH (n) WITH n LIMIT $inner RETURN n") == [("inner", True)]


def test_a_name_used_twice_is_listed_twice_with_positions() -> None:
    uses = parameters(parse("MATCH (n)\nWHERE n.x = $p OR n.y = $p\nRETURN n"))
    assert [(u.name, u.pos.line) for u in uses] == [("p", 2), ("p", 2)]
    assert uses[0].pos.column < uses[1].pos.column
    assert uses[0] == ParameterUse("p", uses[0].pos, False)


def test_clause_expressions_reach_pattern_predicates_and_property_maps() -> None:
    (match, _) = parse("MATCH (a {k: $p}) WHERE (a)-[:R]->(:T {j: $q}) RETURN a").clauses
    found = [e.name for e in clause_expressions(match) if isinstance(e, Parameter)]
    assert found == ["p", "q"]


def test_clause_patterns_are_the_match_patterns_and_the_predicates() -> None:
    (match, ret) = parse("MATCH (a)-[:R]->(b) WHERE (b)-[:S]->(:T) RETURN a").clauses
    parts: list[PatternPart] = list(clause_patterns(match))
    assert [len(p.rels) for p in parts] == [1, 1]
    assert [r.types for p in parts for r in p.rels] == [("R",), ("S",)]
    assert list(clause_patterns(ret)) == []


def test_clause_expressions_list_labels_tests() -> None:
    (match, _) = parse("MATCH (a) WHERE a:T RETURN a").clauses
    assert any(isinstance(e, HasLabels) for e in clause_expressions(match))
