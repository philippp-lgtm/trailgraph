"""The clause pipeline of the Cypher subset: projection, WITH, aggregation,
DISTINCT, ORDER BY, SKIP/LIMIT and UNWIND (plan cypher_query.md 3.2, 3.3; step 5)."""

from decimal import Decimal
from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.execute import Result, execute
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.query.cypher.values import Node

INV = compiled_model("inventory", "2.1.0")

# Items in locations; one item has no location, one has no weight.
GRAPH = DictSnapshot(
    revision("shelf", "inv:Location", {"name": "Shelf", "code": "S1"}),
    revision("box", "inv:Location", {"name": "Box", "code": "B1"}, [edge("inv:within", "shelf")]),
    revision(
        "lamp",
        "inv:Item",
        {"label": "Lamp", "quantity": 2, "weight": "1.5", "condition": "good"},
        [edge("inv:located_in", "shelf")],
    ),
    revision(
        "desk",
        "inv:Item",
        {"label": "Desk", "quantity": 1, "weight": "20", "condition": "good"},
        [edge("inv:located_in", "shelf")],
    ),
    revision(
        "pen",
        "inv:Item",
        {"label": "Pen", "quantity": 10, "condition": "new"},
        [edge("inv:located_in", "box")],
    ),
    revision("cable", "inv:Item", {"label": "Cable", "quantity": 3, "weight": "0.25"}),
)


def run(text: str, **params: Any) -> Result:
    return execute(resolve(parse(text), INV, params), INV, GRAPH)


def table(text: str, **params: Any) -> list[tuple[Any, ...]]:
    return [
        tuple(v.revision.attrs.get("label") if isinstance(v, Node) else v for v in row)
        for row in run(text, **params).rows
    ]


# Projection


def test_columns_follow_the_return_items() -> None:
    result = run("MATCH (i:Item {label: 'Lamp'}) RETURN i.label, i.quantity AS q, i")
    assert result.columns == ("i.label", "q", "i")
    (row,) = result.rows
    assert row[:2] == ("Lamp", 2) and isinstance(row[2], Node)


def test_return_star_sorts_the_variables() -> None:
    result = run("MATCH (l:Location)<-[r:located_in]-(i:Item {label: 'Pen'}) RETURN *")
    assert result.columns == ("i", "l", "r")


def test_return_without_match() -> None:
    assert table("RETURN 1 + 1 AS two, 'x' AS x") == [(2, "x")]


def test_case_and_functions_in_items() -> None:
    rows = table(
        "MATCH (i:Item) RETURN i.label, "
        "CASE WHEN i.weight > 10 THEN 'heavy' WHEN i.weight IS NULL THEN '?' ELSE 'light' END "
        "AS w ORDER BY i.label"
    )
    assert rows == [("Cable", "light"), ("Desk", "heavy"), ("Lamp", "light"), ("Pen", "?")]


# Aggregation


def test_grouping_and_aggregates() -> None:
    rows = table(
        "MATCH (i:Item)-[:located_in]->(l:Location) "
        "RETURN l.name AS place, count(*) AS n, collect(i.label) AS labels, "
        "sum(i.quantity) AS total, min(i.weight) AS lightest, max(i.label) AS last "
        "ORDER BY place"
    )
    assert rows == [
        ("Box", 1, ["Pen"], 10, None, "Pen"),
        ("Shelf", 2, ["Lamp", "Desk"], 3, Decimal("1.5"), "Lamp"),
    ]


def test_aggregates_skip_nulls_and_distinct_dedupes() -> None:
    ((weights, conditions, distinct, average, total),) = table(
        "MATCH (i:Item) RETURN count(i.weight), count(DISTINCT i.condition), "
        "collect(DISTINCT i.condition), avg(i.quantity), sum(i.weight)"
    )
    assert (weights, conditions, average, total) == (3, 2, Decimal(4), Decimal("21.75"))
    assert sorted(distinct) == ["good", "new"]


def test_aggregates_over_no_rows() -> None:
    assert table(
        "MATCH (i:Item {label: 'none'}) RETURN count(*), count(i), collect(i), sum(i.quantity), "
        "avg(i.quantity), min(i.label)"
    ) == [(0, 0, [], 0, None, None)]
    assert table("MATCH (i:Item {label: 'none'}) RETURN i.label, count(*)") == []


def test_null_is_a_grouping_key() -> None:
    rows = table("MATCH (i:Item) RETURN i.condition AS c, count(*) AS n ORDER BY n DESC, c")
    assert rows == [("good", 2), ("new", 1), (None, 1)]


def test_grouping_by_node() -> None:
    rows = table(
        "MATCH (l:Location) OPTIONAL MATCH (l)<-[:located_in]-(i) "
        "RETURN l, count(i) AS n ORDER BY n"
    )
    assert [(row[1]) for row in rows] == [1, 2]


def test_sum_of_strings_is_a_value_error() -> None:
    with pytest.raises(GraphCliError) as caught:
        run("MATCH (i:Item) RETURN sum(i.label)")
    assert caught.value.code == "query.invalid_value"
    assert "sum() needs numbers, got a string" in caught.value.message


def test_aggregate_inside_an_expression() -> None:
    assert table("MATCH (i:Item) RETURN count(*) * 10 + 1 AS x, size(collect(i)) AS s") == [(41, 4)]


# WITH


def test_with_pipes_and_filters_after_aggregation() -> None:
    rows = table(
        "MATCH (i:Item)-[:located_in]->(l) WITH l, count(i) AS n WHERE n > 1 RETURN l.name, n"
    )
    assert rows == [("Shelf", 2)]


def test_with_order_and_limit_then_match_again() -> None:
    rows = table(
        "MATCH (i:Item) WITH i ORDER BY i.quantity DESC LIMIT 2 "
        "MATCH (i)-[:located_in]->(l) RETURN i.label, l.name"
    )
    assert rows == [("Pen", "Box")]


def test_with_where_runs_after_skip_and_limit() -> None:
    rows = table("MATCH (i:Item) WITH i ORDER BY i.label LIMIT 2 WHERE i.quantity > 2 RETURN i")
    assert rows == [("Cable",)]


def test_with_where_sees_the_variables_before_the_projection() -> None:
    assert table("MATCH (i:Item) WITH i.label AS l WHERE i.quantity > 2 RETURN l ORDER BY l") == [
        ("Cable",),
        ("Pen",),
    ]


def test_grouping_keys_next_to_aggregates_and_in_order_by() -> None:
    rows = table(
        "MATCH (i:Item)-[:located_in]->(l) "
        "RETURN l.name AS place, count(*) AS n, size(l.name) + count(*) AS score "
        "ORDER BY -1 * count(*), place"
    )
    assert rows == [("Shelf", 2, 7), ("Box", 1, 4)]


def test_with_star_and_new_columns() -> None:
    assert table(
        "MATCH (i:Item {label: 'Pen'}) WITH *, i.quantity * 2 AS double RETURN i.label, double"
    ) == [("Pen", 20)]


# DISTINCT, ORDER BY, SKIP, LIMIT


def test_distinct_rows() -> None:
    assert table("MATCH (i:Item) RETURN DISTINCT i.condition AS c ORDER BY c") == [
        ("good",),
        ("new",),
        (None,),
    ]


def test_order_by_puts_null_last_ascending_and_first_descending() -> None:
    assert table("MATCH (i:Item) RETURN i.weight AS w ORDER BY w") == [
        (Decimal("0.25"),),
        (Decimal("1.5"),),
        (Decimal("20"),),
        (None,),
    ]
    assert table("MATCH (i:Item) RETURN i.weight AS w ORDER BY w DESC")[0] == (None,)


def test_order_by_a_variable_that_is_not_returned() -> None:
    assert table("MATCH (i:Item) RETURN i.label ORDER BY i.quantity") == [
        ("Desk",),
        ("Lamp",),
        ("Cable",),
        ("Pen",),
    ]


def test_order_is_stable_and_mixed_directions() -> None:
    rows = table("MATCH (i:Item) RETURN i.condition AS c, i.label AS l ORDER BY c DESC, l")
    assert rows == [(None, "Cable"), ("new", "Pen"), ("good", "Desk"), ("good", "Lamp")]


def test_order_across_types() -> None:
    rows = table("UNWIND [1, 'a', null, true, 2.5, [1]] AS x RETURN x ORDER BY x")
    assert rows == [([1],), ("a",), (True,), (1,), (Decimal("2.5"),), (None,)]


def test_skip_and_limit() -> None:
    assert table("MATCH (i:Item) RETURN i.label ORDER BY i.label SKIP 1 LIMIT 2") == [
        ("Desk",),
        ("Lamp",),
    ]
    assert table("MATCH (i:Item) RETURN i.label ORDER BY i.label SKIP $s", s=3) == [("Pen",)]
    assert table("MATCH (i:Item) RETURN i LIMIT 0") == []


def test_rows_without_order_by_are_deterministic() -> None:
    first = run("MATCH (i:Item)-[:located_in]->(l) RETURN i.label, l.name").rows
    assert all(
        run("MATCH (i:Item)-[:located_in]->(l) RETURN i.label, l.name").rows == first
        for _ in range(3)
    )


# UNWIND


def test_unwind_lists_nulls_and_scalars() -> None:
    assert table("UNWIND [1, 2, 3] AS x RETURN x") == [(1,), (2,), (3,)]
    assert table("UNWIND [] AS x RETURN x") == []
    assert table("UNWIND null AS x RETURN x") == []
    assert table("UNWIND 5 AS x RETURN x") == [(5,)]
    assert table("UNWIND [[1, 2], [3]] AS l UNWIND l AS x RETURN sum(x)") == [(6,)]


def test_unwind_a_parameter_into_a_match() -> None:
    rows = table(
        "UNWIND $labels AS wanted MATCH (i:Item {label: wanted}) RETURN i.quantity ORDER BY wanted",
        labels=["Pen", "Lamp", "Nothing"],
    )
    assert rows == [(2,), (10,)]


def test_unwind_counts_against_the_budget() -> None:
    resolved = resolve(parse("UNWIND [1, 2, 3] AS x UNWIND [1, 2, 3] AS y RETURN x, y"), INV, {})
    with pytest.raises(GraphCliError) as caught:
        execute(resolved, INV, GRAPH, budget=10)
    assert caught.value.code == "query.too_large"
    assert caught.value.details["clause"] == "UNWIND"


# List expressions


def test_list_expressions_over_collected_nodes() -> None:
    rows = table(
        "MATCH (l:Location)<-[:located_in]-(i:Item) "
        "WITH l, collect(i) AS items ORDER BY l.name "
        "RETURN l.name, all(i IN items WHERE i.condition = 'good') AS good, "
        "[i IN items WHERE i.quantity > 1 | i.label] AS many, size(items) AS n, "
        "items[0].quantity >= 1 AS first"
    )
    assert rows == [("Box", False, ["Pen"], 1, True), ("Shelf", True, ["Lamp"], 2, True)]


def test_quantifiers_are_three_valued() -> None:
    (row,) = table(
        "UNWIND [[1, null], [2, 3], []] AS l "
        "WITH collect([any(x IN l WHERE x > 1), all(x IN l WHERE x > 1), "
        "none(x IN l WHERE x > 1), single(x IN l WHERE x > 1)]) AS r RETURN r"
    )
    # [1, null]: 1 > 1 is false, null > 1 is null
    one_null, both, empty = row[0]
    assert one_null == [None, False, None, None]
    assert both == [True, True, False, False]
    assert empty == [False, True, True, False]


def test_indexing_a_non_list_is_an_invalid_value() -> None:
    with pytest.raises(GraphCliError) as caught:
        run("MATCH (i:Item {label: 'Pen'}) RETURN i.label[0]")
    assert caught.value.code == "query.invalid_value"
