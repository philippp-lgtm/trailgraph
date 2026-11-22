"""Which functions cover an ad-hoc query (usage shapes plan U2), on the example company."""

import statistics
import time
from dataclasses import replace
from typing import Any

import pytest
from support.actions import company_model
from support.fixtures import EXAMPLES

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.cover import (
    Cover,
    SignatureCache,
    covers,
    function_signature,
    query_signature,
)
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve

MODEL = company_model()
SOURCES = {
    p.name: p.read_text() for p in sorted((EXAMPLES / "company" / "functions").glob("*.yaml"))
}
MEMBERS = "MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u:OrgUnit)"


def entries(sources: dict[str, str]) -> list[FunctionEntry]:
    document, problems = compile_catalog(sources, MODEL)
    assert not problems, [p.code for p in problems]
    return list(load_entries(document))


CATALOG = entries(SOURCES)
CACHE = SignatureCache()


def found(
    text: str, catalog: list[FunctionEntry] | None = None, **params: Any
) -> tuple[Cover, ...]:
    signature = query_signature(resolve(parse(text), MODEL, params))
    return covers(signature, CACHE.pairs(catalog or CATALOG, MODEL), MODEL)


def names(text: str, **params: Any) -> list[str]:
    return [c.function for c in found(text, **params)]


def variant(name: str, *edits: tuple[str, str], new_name: str | None = None) -> dict[str, str]:
    """The example sources with edits to one function file, or (``new_name``) a renamed copy."""
    text = SOURCES[name]
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    if new_name is None:
        return {**SOURCES, name: text}
    return {**SOURCES, new_name + ".yaml": text.replace(f"name: {name[:-5]}", f"name: {new_name}")}


def test_a_query_with_the_pattern_of_a_function_and_its_anchor_is_covered() -> None:
    covered = found(f"{MEMBERS} WHERE u.alias = 'x' RETURN p.name")
    first = covered[0]
    assert (first.function, first.parameter, first.of_class) == (
        "members_of_unit",
        "unit",
        "org:OrgUnit",
    )
    assert first.list_fit and not first.many
    assert first.summary.startswith("People holding a position")


def test_an_extra_required_triple_uncovers_the_query() -> None:
    query = f"{MEMBERS}, (u)-[:charged_to]->(c:CostCenter) WHERE u.alias = 'x' RETURN p.name"
    assert names(query) == []


def test_a_function_that_requires_more_relationships_answers_a_narrower_question() -> None:
    # members_of_unit lists held positions only: no answer for every position of a unit
    positions = "MATCH (q:Position)-[:in_unit]->(u:OrgUnit) WHERE u.id = 'x' RETURN q.title"
    assert "members_of_unit" not in names(positions)
    # missing_required_trainings also requires the training path: not every member
    members = names(f"{MEMBERS} WHERE u.id = 'x' RETURN p.name, q.title")
    assert "members_of_unit" in members and "missing_required_trainings" not in members


def test_an_anchor_on_a_subclass_of_the_function_anchor_class_is_covered() -> None:
    assert "members_of_unit" in names(
        f"{MEMBERS.replace('OrgUnit', 'Team')} WHERE u.id = 'x' RETURN p.name"
    )


def test_an_anchor_on_a_superclass_of_the_function_anchor_class_is_not() -> None:
    assert (
        names("MATCH (p:Person)-[:employed_by]->(l:LegalEntity) WHERE p.id = 'x' RETURN l.name")
        == []
    )
    employee = "MATCH (p:Employee)-[:employed_by]->(l:LegalEntity) WHERE p.id = 'x' RETURN l.name"
    assert names(employee) == ["employer_of"]


def test_a_query_with_only_optional_edges_is_never_covered() -> None:
    optional = "OPTIONAL MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u)"
    query = f"MATCH (u:OrgUnit) WHERE u.id = 'x' {optional} RETURN u"
    assert names(query) == []


def test_a_zero_length_step_is_possible_but_not_required() -> None:
    zero = "MATCH (a:OrgUnit)-[:part_of*0..3]->(d:OrgUnit) WHERE d.id = 'x' RETURN a"
    assert names(zero) == []
    one = "MATCH (a:OrgUnit)-[:part_of*1..3]->(d:OrgUnit) WHERE d.id = 'x' RETURN a"
    # the functions walk part_of only from zero: they cannot vouch for the step
    assert names(one) == []
    mixed = (
        "MATCH (q:Position)-[:in_unit]->(u:OrgUnit)-[:part_of*0..3]->(d:OrgUnit) WHERE d.id = $x"
    )
    assert "vacant_positions" in names(mixed + " RETURN q.title", x="ref")


def test_a_step_of_a_function_that_is_required_there_covers_it() -> None:
    chain = "MATCH (a:Position)-[:reports_to*1..3]->(b:Position) WHERE a.id = 'x' RETURN b.title"
    assert names(chain) == ["reporting_chain"]


def test_returning_the_anchor_key_the_caller_gave_still_covers() -> None:
    batch = f"{MEMBERS} WHERE u.alias IN ['a', 'b'] RETURN u.alias, p.name, q.title"
    assert "members_of_unit" in names(batch)
    # another entity's alias is data the function does not return
    other = f"{MEMBERS} WHERE u.alias IN ['a', 'b'] RETURN p.alias, p.name"
    assert "members_of_unit" not in names(other)


def test_a_list_anchor_on_a_single_ref_parameter_does_not_fit() -> None:
    (cover, *_) = found(f"{MEMBERS} WHERE u.alias IN ['a', 'b', 'c'] RETURN p.name")
    assert cover.function == "members_of_unit" and not cover.list_fit


def test_a_list_valued_parameter_counts_as_a_list() -> None:
    (cover, *_) = found(f"{MEMBERS} WHERE u.alias IN $names RETURN p.name", names=["a", "b"])
    assert not cover.list_fit
    (single, *_) = found(f"{MEMBERS} WHERE u.alias = $name RETURN p.name", name="a")
    assert single.list_fit


def test_a_list_anchor_fits_a_many_parameter() -> None:
    sources = variant(
        "members_of_unit.yaml",
        ("type: ref\n", "type: ref\n    many: true\n"),
        ("d.id = $unit", "d.id IN $unit"),
    )
    (cover, *_) = found(f"{MEMBERS} WHERE u.alias IN ['a', 'b'] RETURN p.name", entries(sources))
    assert cover.function == "members_of_unit" and cover.many and cover.list_fit


def test_an_or_of_equalities_on_one_key_is_a_list_anchor() -> None:
    (cover, *_) = found(f"{MEMBERS} WHERE u.alias = 'a' OR u.alias = 'b' RETURN p.name")
    assert cover.function == "members_of_unit" and not cover.list_fit


@pytest.mark.parametrize(
    "where",
    [
        "WHERE u.alias = 'a' OR u.id = 'b'",  # two keys: no single anchor
        "WHERE u.name = 'a'",  # a property that does not identify a ref
        "WHERE u.name IN ['a', 'b']",
        "WHERE NOT u.name = 'a'",
        "WHERE u.name <> 'a'",
        "WHERE u.name STARTS WITH 'a'",
        "WHERE u.name = p.name",
        "",
    ],
)
def test_comparisons_that_fix_no_value_are_no_anchor(where: str) -> None:
    assert names(f"{MEMBERS} {where} RETURN p.name") == []


def test_an_inline_property_map_anchors() -> None:
    assert "members_of_unit" in names(
        "MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u:OrgUnit {alias: 'x'}) RETURN p.name"
    )


def test_an_anchor_in_an_optional_match_fixes_nothing() -> None:
    query = (
        "MATCH (p:Person)-[:holds]->(q:Position) "
        "OPTIONAL MATCH (q)-[:in_unit]->(u:OrgUnit {alias: 'x'}) RETURN p.name"
    )
    assert names(query) == []


def test_a_parameter_of_a_function_that_is_no_ref_does_not_anchor() -> None:
    sources = variant(
        "members_of_unit.yaml",
        ("type: ref\n    class: org:OrgUnit\n", "type: string\n"),
        ("d.id = $unit", "d.name = $unit"),
    )
    query = f"{MEMBERS} WHERE u.alias = 'x' RETURN p.name"
    assert "members_of_unit" not in [c.function for c in found(query, entries(sources))]


def test_queries_without_anchor_or_without_edges_are_never_covered() -> None:
    assert names(f"{MEMBERS} RETURN p.name") == []
    assert names("MATCH (u:OrgUnit) WHERE u.name = 'x' RETURN u") == []
    assert names("MATCH (u:OrgUnit) RETURN count(u)") == []


def test_direction_is_normalized_and_an_undirected_step_matches_either_way() -> None:
    incoming = "MATCH (q:Position)<-[:holds]-(p:Person), (q)-[:in_unit]->(u:OrgUnit)"
    assert "members_of_unit" in names(f"{incoming} WHERE u.id = 'x' RETURN p.name")
    undirected = "MATCH (p:Person)-[:holds]-(q:Position)-[:in_unit]-(u:OrgUnit)"
    assert "members_of_unit" in names(f"{undirected} WHERE u.id = 'x' RETURN p.name")
    reversed_ = "MATCH (q:Position)-[:holds]-(p:Person), (q)-[:in_unit]->(u:OrgUnit)"
    assert "members_of_unit" in names(f"{reversed_} WHERE u.id = 'x' RETURN p.name")
    wrong_way = "MATCH (q:Position)-[:holds]->(p:Person), (q)-[:in_unit]->(u:OrgUnit)"
    assert "members_of_unit" not in names(f"{wrong_way} WHERE u.id = 'x' RETURN p.name")


def test_a_pattern_under_not_is_no_triple() -> None:
    # vacant_positions excludes the holders (``NOT (:Person)-[:holds]->(q)``): it does not list them
    query = f"{MEMBERS} WHERE u.id = 'x' RETURN p.name, q.title, u.name"
    assert "members_of_unit" in names(query) and "vacant_positions" not in names(query)
    unheld = (
        "MATCH (q:Position)-[:in_unit]->(u:OrgUnit) "
        "WHERE u.id = 'x' AND NOT (:Person)-[:holds]->(q)"
    )
    assert "vacant_positions" in names(f"{unheld} RETURN q.title, q.status, u.name")


def test_relation_types_of_the_query_must_be_among_the_functions() -> None:
    other = "MATCH (q:Position)-[:in_unit|carries]->(u:OrgUnit) WHERE u.id = 'x' RETURN q.title"
    assert "members_of_unit" not in names(other)
    one_of = "MATCH (q:Position)-[:in_unit|part_of]->(u:OrgUnit) WHERE u.id = 'x' RETURN q.title"
    assert "members_of_unit" not in names(one_of)


def test_an_untyped_relationship_is_a_wildcard() -> None:
    assert "members_of_unit" in names(
        "MATCH (p:Person)-[]->(q:Position)-[:in_unit]->(u:OrgUnit) WHERE u.id = 'x' RETURN p.name"
    )


def test_ranking_prefers_the_larger_share_then_featured_then_name() -> None:
    sources = {
        **variant(
            "members_of_unit.yaml",
            (
                "RETURN p.name",
                "OPTIONAL MATCH (p)-[:completed]->(t:Training)\n  RETURN p.name",
            ),
            new_name="a_wide",
        ),
        **variant("members_of_unit.yaml", new_name="z_members"),
        **variant(
            "members_of_unit.yaml", ("featured: true", "featured: false"), new_name="a_members"
        ),
    }
    covered = found(f"{MEMBERS} WHERE u.id = 'x' RETURN p.name", entries(sources))
    ordered = [c.function for c in covered if "members" in c.function or c.function == "a_wide"]
    assert ordered == ["members_of_unit", "z_members", "a_members", "a_wide"]
    shares = {c.function: c.share for c in covered}
    assert shares["members_of_unit"] > shares["a_wide"]


def test_deprecated_functions_never_cover() -> None:
    query = f"{MEMBERS} WHERE u.id = 'x' RETURN p.name"
    old = [
        replace(e, defn=replace(e.defn, status="deprecated")) if e.name == "members_of_unit" else e
        for e in CATALOG
    ]
    assert "members_of_unit" in names(query)
    assert "members_of_unit" not in [c.function for c in found(query, old)]


def test_the_query_must_return_what_the_function_returns() -> None:
    where = f"{MEMBERS} WHERE u.id = 'x'"
    assert "members_of_unit" in names(f"{where} RETURN p.name, q.title, u.name")
    assert names(f"{where} RETURN p.name, u.alt_labels") == []  # a column the function lacks
    assert names(f"{where} RETURN p") == []  # a whole entity
    assert names(f"{where} RETURN *") == []


def test_an_aggregating_query_needs_an_aggregating_function() -> None:
    where = f"{MEMBERS} WHERE u.id = 'x'"
    assert names(f"{where} RETURN u.name, count(p) AS n") == []
    assert names(f"{where} RETURN count(*)") == []
    chain = "MATCH (a:Position)-[r:reports_to*1..3]->(b:Position) WHERE a.id = 'x'"
    assert names(f"{chain} RETURN b.title, size(r) AS level") == ["reporting_chain"]


def test_the_signature_of_a_function_comes_from_its_stored_text() -> None:
    entry = next(e for e in CATALOG if e.name == "reporting_chain")
    signature = function_signature(entry, MODEL)
    (anchor,) = signature.anchors
    assert (anchor.parameter, anchor.is_ref, anchor.many) == ("position", True, False)
    assert len(signature.required) == 1 and len(signature.triples) == 2


def test_the_cache_keeps_signatures_and_survives_a_stale_function() -> None:
    cache = SignatureCache(limit=2)
    first, second, third = CATALOG[:3]
    a = cache.signature(first, MODEL)
    assert cache.signature(first, MODEL) is a
    cache.signature(second, MODEL)
    cache.signature(third, MODEL)  # evicts the oldest
    assert cache.signature(first, MODEL) is not a
    stale = replace(first, query="MATCH (n:`nope:Gone`) RETURN n", hash="sha256:stale")
    with pytest.raises(GraphCliError):
        function_signature(stale, MODEL)
    assert cache.signature(stale, MODEL).triples == frozenset()


def test_cover_check_is_fast_for_a_catalog_of_fifty() -> None:
    # The plan asks for 5 ms p50 including the query signature; the median keeps CI from flaking.
    catalog = [
        replace(entry, defn=replace(entry.defn, name=f"{entry.name}_{i}"), hash=f"h{i}_{n}")
        for i in range(9)
        for n, entry in enumerate(CATALOG)
    ][:50]
    cache = SignatureCache()
    pairs = cache.pairs(catalog, MODEL)  # signatures are computed once per function
    resolved = resolve(
        parse(f"{MEMBERS}, (q)-[:carries]->(r:Role) WHERE u.alias IN ['a', 'b'] RETURN p.name"),
        MODEL,
        {},
    )
    times = []
    for _ in range(60):
        started = time.perf_counter()
        covers(query_signature(resolved), pairs, MODEL)
        times.append(time.perf_counter() - started)
    assert statistics.median(times) < 0.005


def test_the_live_rule_anchors_on_references_only_not_on_names() -> None:
    # recipe lint (``kinetic.recipes``) accepts a name anchor for a maintainer; a hint must not
    assert names(f"{MEMBERS} WHERE u.name = 'x' RETURN p.name") == []
    assert names(f"{MEMBERS} WHERE u.alias = 'x' RETURN p.name")[0] == "members_of_unit"
