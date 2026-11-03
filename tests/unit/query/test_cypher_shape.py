"""Canonical query shapes: structure kept, data stripped (usage shapes plan U1)."""

import time
from typing import Any

import pytest
from support.fixtures import compiled_model

from graph_cli.query.cypher import render
from graph_cli.query.cypher.ast import Parameter
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.query.cypher.shape import SHAPE_MAX_BYTES, Shape, shape

NAS = compiled_model("notes_and_sources")


def shaped(text: str, **params: Any) -> Shape:
    return shape(resolve(parse(text), NAS, params))


def text_of(text: str, **params: Any) -> str:
    result = shaped(text, **params).text
    assert result is not None
    return result


def test_variables_and_aliases_are_renamed_in_order_of_appearance() -> None:
    assert text_of("MATCH (a:Note)-[r:cites]->(b:Source) RETURN a.title AS name, b") == (
        "MATCH (v1:`nas:Note`)-[v2:`nas:cites`]->(v3:`nas:Source`)\nRETURN v1.title AS c1, v3"
    )
    renamed = shaped("MATCH (x:Note)-[q:cites]->(y:Source) RETURN x.title AS other, y")
    assert renamed == shaped("MATCH (a:Note)-[r:cites]->(b:Source) RETURN a.title AS name, b")


def test_with_unwind_and_local_variables_are_renamed_consistently() -> None:
    text = text_of(
        "MATCH (n:Note) WITH n, count(*) AS total WHERE total > 1 "
        "UNWIND [1, n.title] AS item "
        "RETURN item, any(k IN [n.title] WHERE k = 'x') AS flag, [z IN [n.title] | z] AS zs"
    )
    assert text == (
        "MATCH (v1:`nas:Note`)\n"
        "WITH v1, count(*) AS c1 WHERE (c1 > $n)\n"
        "UNWIND [$n, v1.title] AS v2\n"
        "RETURN v2, any(v3 IN [v1.title] WHERE (v3 = $s)) AS c2, [v4 IN [v1.title] | v4] AS c3"
    )


def test_order_by_may_use_an_alias_or_an_incoming_variable() -> None:
    text = text_of("MATCH (n:Note) RETURN n.title AS t ORDER BY t DESC, n.body")
    assert text == "MATCH (v1:`nas:Note`)\nRETURN v1.title AS c1 ORDER BY c1 DESC, v1.body"


def test_unaliased_columns_never_keep_their_text() -> None:
    text = text_of("MATCH (n:Note) RETURN n.title = 'Marker-X', n.title")
    assert "Marker-X" not in text
    assert text.endswith("RETURN (v1.title = $s) AS c1, v1.title AS c2")


def test_local_and_prefixed_names_give_one_shape() -> None:
    local = shaped("MATCH (n:Note)-[:cites]->(s:Source) RETURN n")
    prefixed = shaped("MATCH (n:`nas:Note`)-[:`nas:cites`]->(s:`nas:Source`) RETURN n")
    assert local == prefixed


def test_lists_collapse_whatever_their_length() -> None:
    three = shaped("MATCH (n:Note) WHERE n.title IN ['a', 'b', 'c'] RETURN n")
    eight = shaped("MATCH (n:Note) WHERE n.title IN [1, 2, 3, 4, 5, 6, 7, 8] RETURN n")
    assert three == eight
    assert "n.title IN $list" in (three.text or "").replace("v1.title", "n.title")


def test_a_list_with_computed_items_keeps_its_structure() -> None:
    text = text_of("MATCH (n:Note) WHERE n.title IN ['a', n.body] RETURN n")
    assert "IN [$s, v1.body]" in text


def test_literal_and_parameter_give_one_shape() -> None:
    literal = shaped("MATCH (n:Note {title: 'T-1'}) WHERE n.body IN ['a', 'b'] RETURN n")
    param = shaped(
        "MATCH (n:Note {title: $t}) WHERE n.body IN $ids RETURN n", t="T-1", ids=["a", "b", "c"]
    )
    assert literal == param


def test_parameter_kinds() -> None:
    query = "MATCH (n:Note) WHERE n.title = $p RETURN n"
    kinds = {
        "$s": "x",
        "$n": 4,
        "$b": True,
        "$list": ["x"],
    }
    for placeholder, value in kinds.items():
        assert placeholder in text_of(query, p=value)
    assert "(v1.title = null)" in text_of(query, p=None)


def test_placeholders_are_typed() -> None:
    texts = {
        text_of(f"MATCH (n:Note) WHERE n.title = {literal} RETURN n")
        for literal in ("'x'", "1", "true")
    }
    assert len(texts) == 3
    assert "(v1.title = $n)" in text_of("MATCH (n:Note) WHERE n.title = 1.50 RETURN n")
    assert "(v1.title = $n)" in text_of("MATCH (n:Note) WHERE n.title = -7 RETURN n")
    assert "(v1.title = null)" in text_of("MATCH (n:Note) WHERE n.title = null RETURN n")


def test_skip_and_limit_are_dropped() -> None:
    plain = shaped("MATCH (n:Note) RETURN n ORDER BY n.title")
    paged = shaped("MATCH (n:Note) RETURN n ORDER BY n.title SKIP 20 LIMIT 5")
    assert plain == paged
    assert shaped("MATCH (n:Note) RETURN n LIMIT $k", k=3) == shaped("MATCH (n:Note) RETURN n")


def test_map_values_are_placeholders() -> None:
    text = text_of(
        "MATCH (n:Note {title: 'x', body: 'y'})-[:cites {locator: 'p. 3'}]->(s) RETURN n"
    )
    assert "{title: $s, body: $s}" in text
    assert "{locator: $s}" in text


def test_keys_of_map_values_are_renamed() -> None:
    text = text_of("MATCH (n:Note) RETURN {marker_key_7f3a: n.title, other: 1} AS m")
    assert "marker_key_7f3a" not in text
    assert "{k1: v1.title, k2: $n}" in text


def test_structure_differences_change_the_hash() -> None:
    one = shaped("MATCH (n:Note) WHERE n.title = 'x' RETURN n")
    other = shaped("MATCH (n:Note) WHERE n.body = 'x' RETURN n")
    assert one.hash != other.hash
    assert one.hash.startswith("sha256:")


def test_cap_withholds_the_text_but_keeps_the_hash() -> None:
    conditions = " AND ".join(f"n.title = 'v{i}' OR n.body = {i}" for i in range(300))
    big = shaped(f"MATCH (n:Note) WHERE {conditions} RETURN n")
    assert big.text is None
    again = shaped(f"MATCH (n:Note) WHERE {conditions} RETURN n")
    assert again.hash == big.hash
    small = shaped("MATCH (n:Note) RETURN n")
    assert small.text is not None and len(small.text.encode()) <= SHAPE_MAX_BYTES


def test_no_marker_survives() -> None:
    markers = ["zq-marker-1", "zq-marker-2", "zq-marker-3", "zq-marker-4", "zq-marker-5"]
    query = (
        f"MATCH (zq_marker_var:Note {{title: '{markers[0]}'}}) "
        f"WHERE zq_marker_var.body IN ['{markers[1]}', '{markers[2]}'] "
        f"AND zq_marker_var.title = $zq_marker_param "
        f"WITH zq_marker_var AS zq_marker_alias, count(*) AS zq_marker_count "
        f"UNWIND ['{markers[3]}'] AS zq_marker_item "
        f"RETURN zq_marker_alias.title = '{markers[4]}', zq_marker_count, zq_marker_item"
    )
    result = shaped(query, zq_marker_param="zq-marker-6")
    assert result.text is not None
    for marker in [*markers, "zq-marker-6", "zq_marker"]:
        assert marker not in result.text


def test_shape_is_fast() -> None:
    # Generous bound (the plan asks for well under 1 ms); the margin keeps CI from flaking.
    conditions = " AND ".join(f"n.title = 'v{i}'" for i in range(20))
    resolved = resolve(
        parse(
            f"MATCH (n:Note)-[:cites]->(s:Source) WHERE {conditions} AND s.title IN [1, 2, 3] "
            "WITH n, s, count(*) AS c RETURN n.title AS t, s.title, c ORDER BY t LIMIT 10"
        ),
        NAS,
        {},
    )
    runs = 200
    started = time.perf_counter()
    for _ in range(runs):
        shape(resolved)
    assert (time.perf_counter() - started) / runs < 0.005


@pytest.mark.parametrize("name", ["list", "map", "s", "n", "b"])
def test_placeholder_names_render_unquoted(name: str) -> None:
    assert render.expr(Parameter(name)) == "$" + name
