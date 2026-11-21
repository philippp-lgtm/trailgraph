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
