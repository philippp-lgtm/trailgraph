"""Judging query recipes of a graph skill against published functions (recipe lint, R3 and R5).

The functions are the company example's plus a few small ones written here, so every rule
has a recipe that is covered and one that is not for exactly that rule.
"""

from typing import Any

import pytest
from support.actions import company_model
from support.fixtures import EXAMPLES

from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.cover import (
    SignatureCache,
    covers,
    query_signature,
    recipe_covers,
    recipe_signature,
)
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.recipes import RecipeVerdict, function_pairs, judge_recipe
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve

MODEL = company_model()
EXAMPLE = {
    p.name: p.read_text() for p in sorted((EXAMPLES / "company" / "functions").glob("*.yaml"))
}
MEMBERS = "MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u:OrgUnit)"


def function(
    name: str,
    query: str,
    returns: tuple[str, ...],
    params: str = "params: {}",
    extra: str = "",
) -> dict[str, str]:
    columns = "\n".join(f"  - column: {c}\n    summary: The {c}." for c in returns)
    return {
        f"{name}.yaml": f"""schema: graph_cli.function/1
name: {name}
module: organization
summary: A test function.
use_when: Questions in the test.
{params}
query: |
  {query}
returns:
{columns}
examples:
  - question: A question in the test?
{extra}
"""
    }


EXTRA = {
    **function(
        "open_positions",
        "MATCH (q:Position) WHERE q.status = 'vacant' RETURN q.title AS title, q.status AS state",
        ("title", "state"),
    ),
    **function(
        "people_named",
        "MATCH (p:Person) WHERE p.name CONTAINS $text RETURN p.name AS person",
        ("person",),
        params="params:\n  text:\n    type: string\n    summary: Part of a name.",
    ),
    **function(
        "old_members",
        "MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u:OrgUnit) WHERE u.id = $unit"
        " RETURN p.name AS person",
        ("person",),
        params="params:\n  unit:\n    type: ref\n    class: org:OrgUnit\n    summary: The unit.",
        extra="status: deprecated\nreplaced_by: members_of_unit",
    ),
    **function(
        "position_count",
        "MATCH (q:Position) RETURN count(q) AS positions",
        ("positions",),
    ),
}


def entries(sources: dict[str, str]) -> list[FunctionEntry]:
    document, problems = compile_catalog(sources, MODEL)
    assert not problems, [p.code for p in problems]
    return list(load_entries(document))


CATALOG = entries({**EXAMPLE, **EXTRA})
PAIRS = function_pairs(CATALOG, MODEL)


def judged(text: str, **params: str) -> RecipeVerdict:
    return judge_recipe(text, params, MODEL, PAIRS)


def covering(text: str, **params: str) -> list[str]:
    verdict = judged(text, **params)
    assert verdict.error is None, verdict.error
    return [c.function for c in verdict.cover]


def test_a_recipe_anchored_on_a_name_is_covered_where_the_live_rule_refuses() -> None:
    text = f"{MEMBERS} WHERE u.name = 'Sales' RETURN p.name, q.title"
    assert covering(text)[0] == "members_of_unit"
    live = covers(
        query_signature(resolve(parse(text), MODEL)), SignatureCache().pairs(CATALOG, MODEL), MODEL
    )
    assert live == ()  # a name is no reference: a live hint must not suggest the function


def test_the_cover_names_the_parameters_it_supplies() -> None:
    (best, *_) = judged(f"{MEMBERS} WHERE u.alias = $unit RETURN p.name", unit="<alias>").cover
    assert (best.function, best.ref_params, best.value_params) == ("members_of_unit", ("unit",), ())
    (found, *_) = judged("MATCH (p:Person) WHERE p.name CONTAINS 'ann' RETURN p.name").cover
    assert (found.function, found.value_params) == ("people_named", (("text", "ann"),))


def test_a_placeholder_for_the_name_is_a_parameter() -> None:
    assert covering(f"{MEMBERS} WHERE u.name = <unit> RETURN p.name")[0] == "members_of_unit"


def test_an_anchorless_recipe_is_covered_by_a_function_without_parameters() -> None:
    assert covering("MATCH (q:Position) WHERE q.status = 'vacant' RETURN q.title, q.status") == [
        "open_positions"
    ]


def test_a_filter_the_function_does_not_show_leaves_the_recipe_uncovered() -> None:
    # open_positions shows title and status, but neither projects nor tests the alias
    shown = "MATCH (q:Position) WHERE q.status = 'vacant' AND q.title = 'Lead' RETURN q.status"
    assert covering(shown) == ["open_positions"]  # the caller narrows the rows by title itself
    hidden = "MATCH (q:Position) WHERE q.status = 'vacant' AND q.alias = 'POS-0001' RETURN q.status"
    assert covering(hidden) == []


def test_a_function_that_filters_more_than_the_recipe_answers_a_narrower_question() -> None:
    # open_positions keeps only vacant positions
    assert covering("MATCH (q:Position) RETURN q.title, q.status") == []
    # members_of_unit's sibling vacant_positions drops abolished ones and positions with a holder
    assert "vacant_positions" not in covering(
        "MATCH (q:Position)-[:in_unit]->(u:OrgUnit) WHERE u.alias = 'x' RETURN q.title, q.status"
    )
    no_holder = (
        "MATCH (q:Position)-[:in_unit]->(u:OrgUnit)-[:part_of*0..5]->(d:OrgUnit)"
        " WHERE d.alias = 'x' AND q.status <> 'abolished' AND NOT (:Person)-[:holds]->(q)"
        " RETURN q.title, q.status, u.name"
    )
    assert covering(no_holder)[0] == "vacant_positions"


def test_a_required_triple_only_the_function_has_uncovers_the_recipe() -> None:
    positions = "MATCH (q:Position)-[:in_unit]->(u:OrgUnit) WHERE u.alias = 'x' RETURN q.title"
    assert "members_of_unit" not in covering(positions)
    assert covering("MATCH (q:Position) RETURN q.title") == []


def test_a_recipe_with_an_extra_required_triple_is_not_covered() -> None:
    text = f"{MEMBERS}, (u)-[:charged_to]->(c:CostCenter) WHERE u.alias = 'x' RETURN p.name"
    assert covering(text) == []


def test_a_string_parameter_is_supplied_by_a_text_comparison_on_its_key() -> None:
    assert covering("MATCH (p:Person) WHERE p.name CONTAINS 'A' RETURN p.name") == ["people_named"]
    assert covering("MATCH (p:Person) RETURN p.name") == []  # the parameter stays open
    # another operator on the key asks another question than CONTAINS
    assert covering("MATCH (p:Person) WHERE p.name STARTS WITH 'A' RETURN p.name") == []
    assert covering("MATCH (p:Person) WHERE p.name = 'Ann' RETURN p.name") == []


def test_without_triples_the_classes_of_the_recipe_must_fit_the_function() -> None:
    assert covering("MATCH (p:Person) WHERE p.name CONTAINS 'A' RETURN p.name") == ["people_named"]
    assert covering("MATCH (r:Role) WHERE r.name CONTAINS 'A' RETURN r.name") == []
    assert covering("MATCH (p:Employee) WHERE p.name CONTAINS 'A' RETURN p.name") == [
        "people_named"
    ]


def test_a_deprecated_function_is_never_suggested() -> None:
    text = f"{MEMBERS} WHERE u.alias = 'x' RETURN p.name"
    assert "old_members" not in covering(text)
    names = [e.name for e in CATALOG if e.deprecated]
    assert names == ["old_members"]


def test_the_function_must_return_what_the_recipe_returns_and_aggregate_alike() -> None:
    assert covering(f"{MEMBERS} WHERE u.alias = 'x' RETURN p.alias") == []
    assert covering("MATCH (q:Position) RETURN count(q) AS n") == ["position_count"]
    assert covering("MATCH (q:Position) RETURN q.title") == []  # a count answers no listing


def test_a_recipe_that_returns_a_whole_entity_needs_a_function_that_does() -> None:
    assert covering("MATCH (q:Position) WHERE q.status = 'vacant' RETURN q") == []


def test_the_best_function_comes_first() -> None:
    text = f"{MEMBERS} WHERE u.alias = 'x' RETURN p.name, q.title, u.name"
    verdict = judged(text)
    assert verdict.cover[0].function == "members_of_unit"
    assert all(0 < c.share <= 1 for c in verdict.cover)


def test_a_number_param_is_bound_as_text_like_graph_query_does() -> None:
    assert covering("MATCH (p:Person) WHERE p.name CONTAINS $n RETURN p.name", n="2.1") == [
        "people_named"
    ]


def test_a_list_parameter_gets_a_list_sample() -> None:
    assert covering("MATCH (p:Person) WHERE p.name IN $names RETURN p.name") == []


def test_a_broken_recipe_carries_the_engine_error() -> None:
    verdict = judged("MATCH (n:Nope) RETURN n")
    assert verdict.error is not None and verdict.error.code == "term.unknown"
    assert verdict.cover == () and not verdict.placeholder_parse_failure
    syntax = judged("MATCH (n RETURN n")
    assert syntax.error is not None and syntax.error.code == "query.syntax"
    assert not syntax.placeholder_parse_failure


@pytest.mark.parametrize("text", ["MATCH (n:<type>) RETURN n", "MATCH (n) RETURN n.<attribute>"])
def test_a_placeholder_where_cypher_has_none_is_no_broken_recipe(text: str) -> None:
    verdict = judged(text)
    assert verdict.error is not None and verdict.placeholder_parse_failure


def test_a_placeholder_inside_a_string_is_data() -> None:
    verdict = judged("MATCH (p:Person) WHERE p.name = '<name>' RETURN p.name")
    assert verdict.error is None and not verdict.placeholder_parse_failure


def test_recipe_covers_takes_the_signature_of_a_resolved_recipe() -> None:
    signature = recipe_signature(resolve(parse(f"{MEMBERS} WHERE u.id = 'x' RETURN p.name"), MODEL))
    assert recipe_covers(signature, PAIRS, MODEL)[0].function == "members_of_unit"


def test_a_function_that_no_longer_resolves_is_left_out() -> None:
    other: Any = CATALOG[0]
    stale = type(other)(
        defn=other.defn,
        module=other.module,
        query="MATCH (n:`nope:Nope`) RETURN n",
        types=other.types,
        relations=other.relations,
        hash="stale",
    )
    assert function_pairs([stale], MODEL) == ()


def test_a_flag_or_a_number_fixes_no_entity() -> None:
    text = (
        "MATCH (l:LegalEntity {in_group: true, name: 'X'}) WHERE l.country IN [1, 2] RETURN l.name"
    )
    anchors = recipe_signature(resolve(parse(text), MODEL)).anchors
    assert [a.key for a in anchors] == ["name"]  # only text names an entity
    listed = "MATCH (l:LegalEntity) WHERE l.name IN ['X', 'Y'] AND l.alias = $a RETURN l.name"
    assert {a.key for a in recipe_signature(resolve(parse(listed), MODEL, {"a": "A"})).anchors} == {
        "name",
        "alias",
    }


def test_a_relationship_the_recipe_negates_must_be_negated_or_walked_by_the_function() -> None:
    # vacant_positions negates holds itself: the recipe may leave it to the function
    lone = (
        "MATCH (q:Position)-[:in_unit]->(u:OrgUnit)-[:part_of*0..5]->(d:OrgUnit)"
        " WHERE d.alias = 'x' AND q.status <> 'abolished' AND NOT (:Person)-[:holds]->(q)"
        " RETURN q.title, q.status, u.name"
    )
    assert covering(lone)[0] == "vacant_positions"
    # members_of_unit walks holds without negating it: its rows show who holds, so the caller can
    # tell the positions nobody holds; a negation of a relationship it never walks stays open
    unwalked = (
        f"{MEMBERS} WHERE u.alias = 'x' AND NOT (q)-[:carries]->(:Role) RETURN p.name, q.title"
    )
    assert covering(unwalked) == []


# Aggregates, columns and boolean switches


def with_functions(*functions: dict[str, str]) -> Any:
    """``judged`` against the company example's functions plus the given ones."""
    sources = {**EXAMPLE}
    for one in functions:
        sources.update(one)
    pairs = function_pairs(entries(sources), MODEL)

    def run(text: str) -> list[str]:
        verdict = judge_recipe(text, {}, MODEL, pairs)
        assert verdict.error is None, verdict.error
        return [c.function for c in verdict.cover]

    return run


UNIT_SIZES = function(
    "unit_sizes",
    "MATCH (u:OrgUnit) OPTIONAL MATCH (q:Position)-[:in_unit]->(u)"
    " RETURN u.name AS unit, count(q) AS positions",
    ("unit", "positions"),
)
UNIT_SIZES_WITH = function(
    "unit_sizes_with",
    "MATCH (u:OrgUnit) OPTIONAL MATCH (q:Position)-[:in_unit]->(u)"
    " WITH u, count(q) AS n RETURN u.name AS unit, n",
    ("unit", "n"),
)
TITLE_RANGE = function(
    "title_range",
    "MATCH (u:OrgUnit)<-[:in_unit]-(q:Position) WITH u, min(q.title) AS first, max(q.title) AS last"
    " RETURN u.name AS unit, first, last",
    ("unit", "first", "last"),
)


def test_a_grouped_aggregate_keeps_a_row_per_entity_and_answers_a_listing() -> None:
    run = with_functions(UNIT_SIZES)
    assert run("MATCH (u:OrgUnit) RETURN u.name") == ["unit_sizes"]


def test_an_aggregate_of_a_with_counts_as_the_function_aggregating() -> None:
    run = with_functions(UNIT_SIZES_WITH)
    text = "MATCH (u:OrgUnit) OPTIONAL MATCH (q:Position)-[:in_unit]->(u) RETURN u.name, count(q)"
    assert run(text) == ["unit_sizes_with"]


def test_an_aggregate_of_a_key_is_not_the_key_or_another_aggregate_of_it() -> None:
    run = with_functions(TITLE_RANGE)
    last = "MATCH (u:OrgUnit)<-[:in_unit]-(q:Position) RETURN u.name, max(q.title)"
    assert run(last) == ["title_range"]
    every = "MATCH (u:OrgUnit)<-[:in_unit]-(q:Position) RETURN u.name, collect(q.title)"
    assert run(every) == []
    plain = "MATCH (u:OrgUnit)<-[:in_unit]-(q:Position) RETURN u.name, q.title"
    assert run(plain) == []


def test_a_variable_that_holds_a_list_or_a_number_is_no_entity() -> None:
    # unit_sizes_with returns n, a count: a recipe asking for other properties is not covered
    run = with_functions(UNIT_SIZES_WITH)
    assert run("MATCH (u:OrgUnit) RETURN u.name, u.alias") == []


def test_a_boolean_switch_that_lifts_a_filter_is_set_by_the_call() -> None:
    external = function(
        "legal_entities",
        "MATCH (l:LegalEntity) WHERE $include_external = true OR l.in_group = true"
        " RETURN l.name AS entity, l.in_group AS in_group",
        ("entity", "in_group"),
        params=(
            "params:\n  include_external:\n    type: boolean\n    default: false\n"
            "    summary: Also entities outside the group."
        ),
    )
    pairs = function_pairs(entries({**EXAMPLE, **external}), MODEL)
    text = "MATCH (l:LegalEntity) RETURN l.name, l.in_group"
    (cover, *_) = judge_recipe(text, {}, MODEL, pairs).cover
    assert (cover.function, cover.value_params) == (
        "legal_entities",
        (("include_external", "true"),),
    )
    # a switch whose lifting value is the default needs no parameter in the call
    default = function(
        "group_entities",
        "MATCH (l:LegalEntity) WHERE $only_group = false OR l.in_group = true"
        " RETURN l.name AS entity, l.in_group AS in_group",
        ("entity", "in_group"),
        params=(
            "params:\n  only_group:\n    type: boolean\n    default: false\n"
            "    summary: Only entities of the group."
        ),
    )
    pairs = function_pairs(entries({**EXAMPLE, **default}), MODEL)
    (cover, *_) = judge_recipe(text, {}, MODEL, pairs).cover
    assert (cover.function, cover.value_params) == ("group_entities", ())
    # the same filter without a switch narrows the question
    fixed = function(
        "group_only",
        "MATCH (l:LegalEntity) WHERE l.in_group = true RETURN l.name AS entity, l.in_group AS g",
        ("entity", "g"),
    )
    assert with_functions(fixed)(text) == []


# Constants, limits, anchors that name an entity, required switches, ellipses


def test_a_constant_filter_of_the_function_must_be_the_recipes() -> None:
    same = "MATCH (q:Position) WHERE q.status = 'vacant' RETURN q.title, q.status"
    assert covering(same) == ["open_positions"]
    other = "MATCH (q:Position) WHERE q.status = 'abolished' RETURN q.title, q.status"
    assert covering(other) == []  # open_positions keeps vacant ones only
    many = function(
        "staffing",
        "MATCH (q:Position) WHERE q.status IN ['vacant', 'filled']"
        " RETURN q.title AS t, q.status AS s",
        ("t", "s"),
    )
    run = with_functions(many)
    assert run(same) == ["staffing"]  # the recipe's value is among the function's
    assert run(other) == []


def test_a_function_with_a_limit_covers_only_a_recipe_with_one() -> None:
    top = function(
        "first_titles",
        "MATCH (q:Position) RETURN q.title AS title ORDER BY title LIMIT 5",
        ("title",),
    )
    run = with_functions(top)
    assert run("MATCH (q:Position) RETURN q.title") == []
    assert run("MATCH (q:Position) RETURN q.title ORDER BY q.title LIMIT 3") == ["first_titles"]


def test_only_a_field_that_names_an_entity_supplies_a_ref() -> None:
    holder = function(
        "position_holder",
        "MATCH (p:Person)-[:holds]->(q:Position) WHERE q.id = $position RETURN p.name AS person",
        ("person",),
        params="params:\n  position:\n    type: ref\n    class: org:Position\n    summary: One.",
    )
    entity = function(
        "legal_name",
        "MATCH (l:LegalEntity) WHERE l.id = $entity RETURN l.name AS name",
        ("name",),
        params="params:\n  entity:\n    type: ref\n    class: leg:LegalEntity\n    summary: One.",
    )
    run = with_functions(holder, entity)
    holds = "MATCH (p:Person)-[:holds]->(q:Position) WHERE q.{} RETURN p.name"
    assert run(holds.format("alias = 'POS-0001'")) == ["position_holder"]
    assert run(holds.format("title = 'Lead'")) == ["position_holder"]  # a label
    assert run(holds.format("status = 'filled'")) == []  # a lifecycle state names nobody
    legal = "MATCH (l:LegalEntity) WHERE l.{} RETURN l.name"
    assert run(legal.format("registration_number = 'R1'")) == ["legal_name"]  # unique
    assert run(legal.format("country = 'DE'")) == []


def test_a_required_switch_without_a_default_is_shown_in_the_call() -> None:
    required = function(
        "internal_entities",
        "MATCH (l:LegalEntity) WHERE $internal = true RETURN l.name AS name",
        ("name",),
        params="params:\n  internal:\n    type: boolean\n    summary: Only entities of the group.",
    )
    pairs = function_pairs(entries({**EXAMPLE, **required}), MODEL)
    (cover, *_) = judge_recipe("MATCH (l:LegalEntity) RETURN l.name", {}, MODEL, pairs).cover
    assert cover.value_params == (("internal", "<true|false>"),)


def test_an_ellipsis_is_a_placeholder_where_cypher_has_none() -> None:
    verdict = judged("MATCH (n:Position) WHERE n.title = ... RETURN n.title")
    assert verdict.error is not None and verdict.placeholder_parse_failure
