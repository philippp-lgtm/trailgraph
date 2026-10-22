"""Pattern matching of the Cypher subset (plan cypher_query.md 3.1, 3.3; step 4)."""

from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.execute import execute
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.query.cypher.values import Node, Rel

NAS = compiled_model("notes_and_sources")

# Notes cite sources and are about topics; topics form a broader chain.
GRAPH = DictSnapshot(
    revision(
        "note_a",
        "nas:Note",
        {"title": "Alpha", "kind": "idea"},
        [
            edge("nas:cites", "book", locator="p. 3"),
            edge("nas:cites", "page"),
            edge("nas:about", "graphs"),
        ],
        alias="NOTE-0001",
    ),
    revision(
        "note_b",
        "nas:Note",
        {"title": "Beta"},
        [edge("nas:cites", "book"), edge("nas:about", "graphs"), edge("nas:about", "storage")],
        alias="NOTE-0002",
    ),
    revision(
        "note_old",
        "nas:Note",
        {"title": "Gamma"},
        [edge("nas:about", "graphs")],
        status="superseded",
    ),
    revision("note_alone", "nas:Note", {"title": "Delta"}),
    revision("book", "nas:Book", {"title": "A book"}),
    revision("page", "nas:WebPage", {"title": "A page", "url": "https://example.org"}),
    revision("graphs", "nas:Topic", {"label": "graphs"}, [edge("nas:broader", "storage")]),
    revision("storage", "nas:Topic", {"label": "storage"}, [edge("nas:broader", "data")]),
    revision("data", "nas:Topic", {"label": "data"}, [edge("nas:broader", "data", name="loop")]),
    revision("gone", "nas:Topic", {"label": "gone"}, status="retracted"),
)
NAMES = {
    eid(n): n
    for n in (
        "note_a",
        "note_b",
        "note_old",
        "note_alone",
        "book",
        "page",
        "graphs",
        "storage",
        "data",
        "gone",
    )
}


def shown(value: Any) -> Any:
    if isinstance(value, Node):
        return NAMES[value.id]
    if isinstance(value, Rel):
        return f"{NAMES[value.source]}-{value.edge.rel}->{NAMES[value.target]}"
    if isinstance(value, list):
        return [shown(v) for v in value]
    return value


def rows(
    text: str, *, include_inactive: bool = False, budget: int = 100_000, **params: Any
) -> list[dict[str, Any]]:
    resolved = resolve(parse(f"{text} RETURN *"), NAS, params)
    result = execute(resolved, NAS, GRAPH, include_inactive=include_inactive, budget=budget)
    return [{k: shown(v) for k, v in zip(result.columns, row, strict=True)} for row in result.rows]


def column(text: str, name: str, **kwargs: Any) -> list[Any]:
    return [row[name] for row in rows(text, **kwargs)]


def ids_order(*names: str) -> list[str]:
    return sorted(names, key=eid)


# Nodes


def test_labels_cover_subclasses_in_id_order() -> None:
    assert column("MATCH (s:Source)", "s") == ids_order("book", "page")
    assert column("MATCH (n:Note|Book)", "n") == ids_order("note_a", "note_b", "note_alone", "book")


def test_inactive_nodes_are_hidden_unless_asked() -> None:
    assert "note_old" not in column("MATCH (n:Note)", "n")
    assert "note_old" in column("MATCH (n:Note)", "n", include_inactive=True)
    assert len(column("MATCH (n)", "n")) == 8
    assert len(column("MATCH (n)", "n", include_inactive=True)) == 10


def test_inline_properties_and_where() -> None:
    assert column("MATCH (n:Note {title: 'Beta'})", "n") == ["note_b"]
    assert column("MATCH (n:Note) WHERE n.title STARTS WITH 'A' OR n.kind = 'x'", "n") == ["note_a"]
    assert column("MATCH (n:Note) WHERE n.kind IS NULL", "n") == ids_order("note_b", "note_alone")
    # NOT on a missing value is null, so the row goes (unlike find's Not).
    assert column("MATCH (n:Note) WHERE NOT n.kind = 'idea'", "n") == []


@pytest.mark.parametrize(
    ("where", "params"),
    [
        ("n.id = $x", {"x": str(eid("note_b"))}),
        ("$x = n.id", {"x": str(eid("note_b"))}),
        ("n.alias = 'NOTE-0002'", {}),
        ("n.alias = $x AND n.title = 'Beta'", {"x": "NOTE-0002"}),
    ],
)
def test_id_and_alias_lookups(where: str, params: dict[str, Any]) -> None:
    assert column(f"MATCH (n) WHERE {where}", "n", **params) == ["note_b"]


def test_lookups_of_missing_or_inactive_entities_find_nothing() -> None:
    assert rows("MATCH (n {id: 'not-an-id'})") == []
    assert rows("MATCH (n {alias: 'NOTE-9999'})") == []
    assert rows("MATCH (n) WHERE n.id = $x", x=str(eid("note_old"))) == []
    assert column(
        "MATCH (n) WHERE n.id = $x", "n", x=str(eid("note_old")), include_inactive=True
    ) == ["note_old"]


# Relationships


def test_directions_and_types() -> None:
    out = rows("MATCH (n:Note {title: 'Alpha'})-[r]->(m)")
    assert [row["m"] for row in out] == ids_order("book", "page", "graphs")
    into = column("MATCH (b:Book)<-[:cites]-(n)", "n")
    assert into == ids_order("note_a", "note_b")
    either = column("MATCH (t:Topic {label: 'storage'})-[:broader]-(x)", "x")
    assert either == ids_order("graphs", "data")
    assert column("MATCH (n:Note {title: 'Beta'})-[:cites|about]->(x)", "x") == ids_order(
        "book", "graphs", "storage"
    )


def test_relationship_values_and_qualifiers() -> None:
    (row,) = rows("MATCH (:Note)-[r:cites {locator: 'p. 3'}]->(b)")
    assert row == {"r": "note_a-nas:cites->book", "b": "book"}


def test_edges_to_inactive_nodes_do_not_match() -> None:
    assert "note_old" not in column("MATCH (t:Topic {label: 'graphs'})<-[:about]-(n)", "n")
    found = column("MATCH (t:Topic {label: 'graphs'})<-[:about]-(n)", "n", include_inactive=True)
    assert "note_old" in found


def test_a_self_loop_matches_once_without_direction() -> None:
    assert column("MATCH (t:Topic {label: 'data'})-[r:broader]-(x)", "x") == ids_order(
        "storage", "data"
    )


def test_bound_variables_join_patterns() -> None:
    found = rows("MATCH (n:Note)-[:cites]->(b), (n)-[:about]->(t)")
    assert {(r["n"], r["b"], r["t"]) for r in found} == {
        ("note_a", "book", "graphs"),
        ("note_a", "page", "graphs"),
        ("note_b", "book", "graphs"),
        ("note_b", "book", "storage"),
    }
    later = rows("MATCH (n:Note {title: 'Alpha'}) MATCH (n)-[:cites]->(b)")
    assert [r["b"] for r in later] == ids_order("book", "page")


def test_a_relationship_is_used_once_per_match() -> None:
    pairs = {(r["a"], r["b"]) for r in rows("MATCH (a:Note)-[:about]->(t)<-[:about]-(b)")}
    assert pairs == {("note_a", "note_b"), ("note_b", "note_a")}
    # Across MATCH clauses the same relationship may come back.
    again = rows("MATCH (a:Note)-[:about]->(t) MATCH (t)<-[:about]-(b) WHERE a = b")
    assert {r["a"] for r in again} == {"note_a", "note_b"}


def test_the_start_node_does_not_change_the_result() -> None:
    left = rows("MATCH (n:Note)-[:about]->(t:Topic {label: 'storage'})")
    assert [r["n"] for r in left] == ["note_b"]
    chained = rows("MATCH (b:Book)<-[:cites]-(n)-[:about]->(t {label: 'storage'})")
    assert [(r["b"], r["n"]) for r in chained] == [("book", "note_b")]


# Variable length


def test_variable_length_paths() -> None:
    reach = rows("MATCH (t:Topic {label: 'graphs'})-[r:broader*1..3]->(x)")
    assert [(r["x"], r["r"]) for r in reach] == [
        ("storage", ["graphs-nas:broader->storage"]),
        ("data", ["graphs-nas:broader->storage", "storage-nas:broader->data"]),
        (
            "data",
            [
                "graphs-nas:broader->storage",
                "storage-nas:broader->data",
                "data-nas:broader->data",
            ],
        ),
    ]
    assert column("MATCH (t:Topic {label: 'graphs'})-[:broader*0..1]->(x)", "x") == [
        "graphs",
        "storage",
    ]
    assert column("MATCH (t:Topic {label: 'graphs'})-[:broader*2]->(x)", "x") == ["data"]


def test_variable_length_lists_follow_the_pattern_order() -> None:
    (row, *_) = rows("MATCH (x {label: 'data'})<-[r:broader*2]-(t:Topic {label: 'graphs'})")
    assert row["r"] == ["storage-nas:broader->data", "graphs-nas:broader->storage"]


# OPTIONAL MATCH and pattern predicates


def test_optional_match_fills_nulls() -> None:
    found = rows("MATCH (n:Note) OPTIONAL MATCH (n)-[:cites]->(b:Book)")
    assert {(r["n"], r["b"]) for r in found} == {
        ("note_a", "book"),
        ("note_b", "book"),
        ("note_alone", None),
    }


def test_optional_match_where_belongs_to_the_pattern() -> None:
    found = rows("MATCH (n:Note) OPTIONAL MATCH (n)-[:cites]->(b) WHERE b.url IS NOT NULL")
    assert {(r["n"], r["b"]) for r in found} == {
        ("note_a", "page"),
        ("note_b", None),
        ("note_alone", None),
    }


def test_pattern_predicates() -> None:
    assert column("MATCH (n:Note) WHERE NOT (n)-[:cites]->()", "n") == ["note_alone"]
    assert column("MATCH (n:Note) WHERE (n)-[:about]->(:Topic {label: 'storage'})", "n") == [
        "note_b"
    ]
    nulls = rows("OPTIONAL MATCH (x:Book {title: 'none'}) MATCH (n:Note) WHERE NOT (x)-->(n)")
    assert nulls == []


# Budget


def test_budget_ends_runaway_matches() -> None:
    with pytest.raises(GraphCliError) as caught:
        rows("MATCH (a), (b), (c)", budget=250)
    error = caught.value
    assert error.code == "query.too_large"
    assert error.details == {"clause": "MATCH", "budget": 250, "line": 1, "column": 1}
    assert "add labels" in error.message
    assert len(rows("MATCH (a), (b)", budget=250)) == 64
