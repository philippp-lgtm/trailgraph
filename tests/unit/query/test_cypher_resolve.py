"""Resolving parsed Cypher against the model (plan cypher_query.md 3.1, 3.2)."""

from dataclasses import replace
from typing import Any

import pytest
from support.fixtures import compiled_model

from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher import render
from graph_cli.query.cypher.ast import Const, Match, Query, Variable
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import ResolvedQuery, resolve

NAS = compiled_model("notes_and_sources")
INV = compiled_model("inventory")


def with_second_note(model: CompiledModel) -> CompiledModel:
    """A model where the local names Note and cites belong to two prefixes."""
    return replace(
        model,
        classes={**model.classes, "provenance:Note": model.classes["nas:Note"]},
        relations={**model.relations, "provenance:cites": model.relations["nas:cites"]},
    )


def run(text: str, model: CompiledModel = NAS, **params: Any) -> ResolvedQuery:
    return resolve(parse(text), model, params)


def failure(text: str, model: CompiledModel = NAS, **params: Any) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        run(text, model, **params)
    return caught.value


def text_of(resolved: ResolvedQuery) -> str:
    return " ".join(render.clause(c) for c in resolved.query.clauses)


# Names


def test_local_names_and_curies_resolve_to_curies() -> None:
    assert text_of(run("MATCH (n:Note)-[:cites]->(s:`nas:Source`) RETURN n")) == (
        "MATCH (n:`nas:Note`)-[:`nas:cites`]->(s:`nas:Source`) RETURN n"
    )


def test_labels_cover_subclasses() -> None:
    (variables, _) = run("MATCH (s:Source), (n:Note|Book) RETURN s").introduced
    assert variables["s"].types == ("nas:Book", "nas:Source", "nas:WebPage")
    assert variables["n"].types == ("nas:Book", "nas:Note")


def test_a_subclass_label_narrows() -> None:
    (variables, _) = run("MATCH (s:Source:Book) RETURN s").introduced
    assert variables["s"].types == ("nas:Book",)


def test_labels_no_type_has_together_are_refused() -> None:
    error = failure("MATCH (s:Book:WebPage) RETURN s")
    assert error.code == "query.unsupported"
    assert error.details["kind"] == "pattern"
    assert "nas:Book" in str(error.details["construct"])


@pytest.mark.parametrize(
    ("text", "kind", "term", "suggestion"),
    [
        ("MATCH (n:Nte) RETURN n", "type", "Nte", "Note"),
        ("MATCH (n:`nas:Nte`) RETURN n", "type", "nas:Nte", "nas:Note"),
        ("MATCH ()-[:cite]->() RETURN 1", "relation", "cite", "cites"),
        ("MATCH (n) WHERE n:Topik RETURN n", "type", "Topik", "Topic"),
    ],
)
def test_unknown_names_suggest(text: str, kind: str, term: str, suggestion: str) -> None:
    error = failure(text)
    assert error.code == "term.unknown"
    assert error.details["kind"] == kind and error.details["term"] == term
    assert suggestion in error.details["did_you_mean"]  # type: ignore[operator]
    assert error.details["line"] == 1


def test_ambiguous_local_names_list_the_candidates() -> None:
    model = with_second_note(NAS)
    error = failure("MATCH (n:Note) RETURN n", model)
    assert error.code == "query.ambiguous_term"
    assert error.details["candidates"] == ["nas:Note", "provenance:Note"]
    assert "`nas:Note`" in error.message
    assert failure("MATCH ()-[:cites]->() RETURN 1", model).details["kind"] == "relation"
    assert run("MATCH (n:`provenance:Note`) RETURN n", model).columns == ("n",)


# Fields


def test_fields_are_checked_against_the_labels() -> None:
    error = failure("MATCH (n:Note) RETURN n.titel")
    assert error.code == "term.unknown" and error.details["kind"] == "field"
    assert error.details["did_you_mean"] == ["title"]
    assert failure("MATCH (n:Note) RETURN n.url").details["term"] == "url"
    assert run("MATCH (n:Source) RETURN n.url").fields[0].datatypes == ("iri",)


def test_system_fields_and_untyped_variables() -> None:
    fields = run("MATCH (n) RETURN n.id, n.alias, n.rev, n.created_at, n.url").fields
    assert [(f.field, f.datatypes) for f in fields] == [
        ("id", ("string",)),
        ("alias", ("string",)),
        ("rev", ("integer",)),
        ("created_at", ("datetime",)),
        ("url", ("iri",)),
    ]
    assert failure("MATCH (n) RETURN n.nothing").code == "term.unknown"


def test_many_valued_fields_are_marked() -> None:
    (use,) = run("MATCH (t:Topic) RETURN t.synonyms").fields
    assert use.many and use.datatypes == ("string",)


def test_relationship_fields_are_qualifiers_and_edge_id() -> None:
    fields = run("MATCH ()-[c:cites]->() RETURN c.locator, c.edge_id").fields
    assert [(f.variable, f.field) for f in fields] == [("c", "locator"), ("c", "edge_id")]
    error = failure("MATCH ()-[a:about]->() RETURN a.locator")
    assert error.code == "term.unknown" and error.details["did_you_mean"] == []


def test_pattern_property_maps_are_fields_too() -> None:
    assert failure("MATCH (n:Note {titel: 'x'}) RETURN n").details["term"] == "titel"
    (use,) = run("MATCH (:Note {title: 'x'}) RETURN 1").fields
    assert use.variable is None and use.field == "title"


def test_values_take_any_key() -> None:
    assert run("UNWIND [{a: 1}] AS m RETURN m.a").columns == ("m.a",)
    assert run("MATCH (n:Note) WITH n.title AS t RETURN t").columns == ("t",)


def test_variable_length_has_no_properties() -> None:
    error = failure("MATCH ()-[r:broader*1..2]->() RETURN r.edge_id")
    assert error.code == "query.syntax" and "list of relationships" in error.message


# Parameters


def test_parameters_are_bound_and_listed() -> None:
    resolved = run("MATCH (n:Note) WHERE n.title = $t RETURN n LIMIT $k", t="x", k=2, unused=1)
    assert resolved.parameters == {"k": 2, "t": "x"}
    error = failure("MATCH (n:Note) WHERE n.title = $t RETURN n")
    assert error.code == "query.parameter_missing"
    assert error.details == {"name": "t", "line": 1, "column": 32}


# Scopes


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("MATCH (n) RETURN m", "m"),
        ("MATCH (n) WITH n.title AS t RETURN n", "n"),
        ("MATCH (n) WITH count(*) AS c RETURN c ORDER BY n.title", "n"),
        ("MATCH (n) RETURN DISTINCT n.title ORDER BY n.rev", "n"),
        ("MATCH (n) WHERE (n)-->(m) RETURN n", "m"),
        ("UNWIND x AS y RETURN y", "x"),
    ],
)
def test_unbound_variables(text: str, name: str) -> None:
    error = failure(text)
    assert error.code == "query.unbound_variable"
    assert error.details["name"] == name


def test_pattern_predicates_may_use_bound_and_anonymous_nodes() -> None:
    run("MATCH (n), (m) WHERE (n)-->(m) AND NOT (n)-[:cites]->(:Book) RETURN n")


def test_with_star_keeps_and_where_sees_the_projection() -> None:
    run("MATCH (n:Note) WITH * WHERE n.title = 'x' RETURN n")
    run("MATCH (n:Note) WITH n, n.title AS t WHERE t = 'x' RETURN n.title")


def test_order_by_sees_earlier_variables_without_aggregation() -> None:
    run("MATCH (n:Note) RETURN n.title AS t ORDER BY n.kind, t")


def test_order_by_repeating_a_projected_expression_uses_the_column() -> None:
    resolved = run("MATCH (n:Note) RETURN n.kind, count(*) ORDER BY count(*) DESC, n.kind")
    order = resolved.query.returns.projection.order
    assert [s.expr for s in order] == [Variable("count(*)"), Variable("n.kind")]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("MATCH (n)-[n]->() RETURN n", "variable 'n' is a node, used here as a relationship"),
        ("MATCH ()-[r]->(n) WITH n MATCH (n)-[n]->() RETURN 1", "'n' is a node, used here"),
        ("UNWIND [1] AS x MATCH (x) RETURN x", "variable 'x' is a value, used here as a node"),
        ("MATCH (n) WITH [n] AS l MATCH (l) RETURN l", "variable 'l' is a list, used here as a"),
        ("MATCH (n) WITH n.rev AS r MATCH ()-[r]->() RETURN r", "variable 'r' is a value"),
        ("MATCH (a)-[r]->(b)-[r]->(c) RETURN 1", "relationship 'r' appears twice in one MATCH"),
        ("MATCH (a)-[r]->(b), (c)-[r]->(d) RETURN 1", "relationship 'r' appears twice"),
        ("MATCH (n) UNWIND [1] AS n RETURN n", "variable 'n' is already defined"),
        ("MATCH ()-[r]->() WHERE r:Note RETURN r", "'r' is not a node"),
        ("MATCH (n) RETURN n, n", "column 'n' appears twice"),
        ("MATCH (n) RETURN n.title AS x, n.rev AS x", "column 'x' appears twice"),
        ("MATCH (n) RETURN *, n", "column 'n' appears twice"),
        ("RETURN *", "RETURN * needs at least one variable"),
    ],
)
def test_scope_mistakes_are_syntax_errors(text: str, message: str) -> None:
    error = failure(text)
    assert error.code == "query.syntax", error.message
    assert message in error.message


def test_reusing_a_variable_length_variable_is_refused() -> None:
    error = failure("MATCH ()-[r*1..2]->() MATCH ()-[r*1..2]->() RETURN 1")
    assert error.code == "query.unsupported" and error.details["kind"] == "pattern"


# Functions and aggregates


def test_function_names_are_case_insensitive() -> None:
    run("MATCH (n) RETURN toLower(n.title), TOUPPER(n.title), Count(n), coalesce(n.title, 'x')")


@pytest.mark.parametrize(
    ("text", "construct", "alternative"),
    [
        ("MATCH (n) RETURN id(n)", "id()", "use n.id, the stable entity id"),
        ("MATCH (n) RETURN elementId(n)", "elementid()", "use n.id, the stable entity id"),
        (
            "MATCH (n) WHERE exists(n.title) RETURN n",
            "exists()",
            "use n.key IS NOT NULL, or a pattern in WHERE",
        ),
    ],
)
def test_refused_functions_point_to_the_alternative(
    text: str, construct: str, alternative: str
) -> None:
    error = failure(text)
    assert error.code == "query.unsupported" and error.details["kind"] == "function"
    assert error.details["construct"] == construct
    assert error.details["alternative"] == alternative


def test_unknown_functions_list_the_subset() -> None:
    error = failure("MATCH (n) RETURN tolowr(n.title)")
    assert error.code == "query.unsupported"
    assert error.details["did_you_mean"][0] == "tolower"  # type: ignore[index]
    assert "count()" in str(error.details["alternative"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("MATCH (n) WHERE count(n) > 1 RETURN n", "aggregate count() is allowed only"),
        ("MATCH (n) RETURN count(count(n))", "aggregate count() is allowed only"),
        ("MATCH (n) RETURN size(count(collect(n)))", "aggregate collect() is allowed only"),
        ("MATCH (n {title: count(*)}) RETURN n", "aggregate count(*) is allowed only"),
        ("UNWIND collect(1) AS x RETURN x", "aggregate collect() is allowed only"),
        ("MATCH (n) RETURN count(n, n)", "count() takes 1 argument, got 2"),
        ("MATCH (n) RETURN coalesce()", "coalesce() takes at least 1 argument, got 0"),
        ("MATCH (n) RETURN toLower(DISTINCT n.title)", "DISTINCT works only in aggregates"),
    ],
)
def test_misused_functions(text: str, message: str) -> None:
    error = failure(text)
    assert error.code == "query.syntax", error.message
    assert message in error.message


def test_values_next_to_aggregates_must_be_grouping_keys() -> None:
    error = failure("MATCH (n) RETURN n.title, count(*) + n.rev")
    assert error.code == "query.unsupported"
    assert error.details["alternative"] == "project n as its own column"
    run("MATCH (n) RETURN n.title, count(*) + 1, collect(n.title)")
    run("MATCH (n) RETURN n.rev, n.rev + count(*)")
    run("MATCH (n) WITH n.rev AS r, n RETURN r, r * count(n)")


def test_order_by_may_combine_columns_and_constants() -> None:
    order = run(
        "MATCH (n) RETURN n.rev AS r, count(*) AS c ORDER BY $k + count(*) - n.rev, r", k=1
    ).query.returns.projection.order
    assert render.expr(order[0].expr) == "(($k + c) - r)"
    error = failure("MATCH (n) RETURN n.rev AS r, count(*) AS c ORDER BY n.title")
    assert error.code == "query.unbound_variable"


def test_values_from_with_and_unwind_may_be_matched_as_nodes() -> None:
    run("MATCH (n:Note) WITH collect(n) AS ns UNWIND ns AS m MATCH (m)-[:cites]->(b) RETURN b")


def test_with_where_sees_earlier_variables_unless_it_aggregates() -> None:
    run("MATCH (n:Note) WITH n.title AS t WHERE n.kind = 'idea' RETURN t")
    error = failure("MATCH (n:Note) WITH count(n) AS c WHERE n.kind = 'idea' RETURN c")
    assert error.code == "query.unbound_variable"


# SKIP and LIMIT


def test_skip_and_limit_become_integers() -> None:
    projection = run("MATCH (n) RETURN n SKIP $s LIMIT 3", s=2).query.returns.projection
    assert (projection.skip, projection.limit) == (Const(2), Const(3))


@pytest.mark.parametrize(
    ("text", "params", "message"),
    [
        ("MATCH (n) RETURN n LIMIT -1", {}, "LIMIT takes a non-negative integer, got -1"),
        ("MATCH (n) RETURN n SKIP 1.5", {}, "SKIP takes a non-negative integer, got 1.5"),
        ("MATCH (n) RETURN n LIMIT $k", {"k": "3"}, "LIMIT takes a non-negative integer, got '3'"),
        ("MATCH (n) RETURN n LIMIT $k", {"k": True}, "got true"),
        ("MATCH (n) RETURN n LIMIT n.rev", {}, "LIMIT takes an integer or a parameter"),
    ],
)
def test_bad_skip_and_limit(text: str, params: dict[str, Any], message: str) -> None:
    error = failure(text, **params)
    assert error.code == "query.syntax"
    assert message in error.message


# --explain


def test_explain_shape() -> None:
    plan = run(
        "MATCH (n:Note)-[c:cites]->(s:Source) WHERE c.locator STARTS WITH $p "
        "WITH n, count(s) AS k RETURN n.title AS title, k ORDER BY k DESC LIMIT 5",
        p="ch",
    ).explain()
    assert plan == {
        "clauses": [
            {
                "clause": "MATCH",
                "text": "MATCH (n:`nas:Note`)-[c:`nas:cites`]->(s:`nas:Source`) "
                "WHERE (c.locator STARTS WITH $p)",
                "variables": {
                    "n": {"kind": "node", "types": ["nas:Note"]},
                    "s": {"kind": "node", "types": ["nas:Book", "nas:Source", "nas:WebPage"]},
                    "c": {"kind": "rel", "types": ["nas:cites"]},
                },
            },
            {
                "clause": "WITH",
                "text": "WITH n, count(s) AS k",
                "variables": {"k": {"kind": "plain", "types": None}},
            },
            {
                "clause": "RETURN",
                "text": "RETURN n.title AS title, k ORDER BY k DESC LIMIT 5",
                "variables": {},
            },
        ],
        "columns": ["title", "k"],
        "parameters": {"p": "ch"},
        "fields": [
            {"variable": "c", "field": "locator", "datatypes": ["string"], "many": False},
            {"variable": "n", "field": "title", "datatypes": ["string"], "many": False},
        ],
    }


@pytest.mark.parametrize(
    "text",
    [
        "MATCH (n:Note)-[c:cites*1..2]->(s:Source {title: 'a\\'b'}) WHERE NOT (n)<--() "
        "RETURN n, s.title AS `odd name` ORDER BY n.title DESC SKIP 1 LIMIT 2",
        "MATCH (n) WHERE n:Note|Topic AND n.rev >= 1.50 AND n.title IN ['x', null] "
        "RETURN DISTINCT CASE n.kind WHEN 'idea' THEN -1 ELSE n.rev % 2 END AS k",
        "UNWIND [1, 2] AS x WITH x, {a: x, `b c`: true} AS m WHERE m.a > 1 OR x <> 2 "
        "RETURN *, toLower(trim('A ')) AS t",
        "OPTIONAL MATCH (n:Note)-[:about]-(t:Topic) RETURN count(DISTINCT t), "
        "collect(t.label) AS labels, n.title STARTS WITH 'a' AS s",
        "MATCH (t:Topic) WHERE any(s IN t.synonyms WHERE s STARTS WITH 'x') "
        "RETURN [s IN t.synonyms WHERE s <> '' | toUpper(s)][0] AS first, "
        "t.synonyms[1..] AS rest, t.synonyms[..-1] AS most",
    ],
)
def test_rendered_text_parses_to_the_same_query(text: str) -> None:
    rendered = text_of(run(text))
    again = text_of(resolve(parse(rendered), NAS))
    assert again == rendered


def test_list_expression_variables_are_local() -> None:
    assert run("MATCH (t:Topic) RETURN [s IN t.synonyms | s] AS x, t").columns == ("x", "t")
    unbound = failure("MATCH (t:Topic) WHERE any(s IN t.synonyms WHERE s = 'a') RETURN s")
    assert unbound.code == "query.unbound_variable"
    taken = failure("MATCH (t:Topic) RETURN [t IN [1] | t] AS x")
    assert taken.code == "query.syntax" and "already defined" in taken.message


def test_list_expressions_check_fields_and_aggregates() -> None:
    assert failure("MATCH (t:Topic) RETURN [s IN t.synonym | s]").code == "term.unknown"
    inner = failure("MATCH (t:Topic) RETURN [s IN [1] WHERE count(*) > 0] AS x")
    assert inner.code == "query.syntax" and "aggregate" in inner.message
    # an aggregate in the list is fine, and the local variable is no grouping key
    grouped = run(
        "MATCH (t:Topic) RETURN t.label, [s IN collect(t.synonyms) WHERE size(s) > 1 | s[0]] AS x"
    )
    assert grouped.columns == ("t.label", "x")


def test_size_of_a_pattern_is_refused() -> None:
    error = failure("MATCH (n:Note) RETURN size((n)-[:cites]->())")
    assert error.code == "query.syntax" and "true/false" in error.message


def test_resolved_tree_keeps_its_shape() -> None:
    query = run("MATCH (n:Note) RETURN n").query
    assert isinstance(query, Query) and isinstance(query.clauses[0], Match)
    assert query.clauses[0].patterns[0].nodes[0].labels == (("nas:Note",),)
