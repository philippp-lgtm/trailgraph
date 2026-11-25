"""Compiling functions against the company model (plan K3, K4, K5)."""

import json
from dataclasses import replace
from typing import Any

import pytest
from support.fixtures import GOLDENS, assert_golden, compiled_model
from support.functions import BROKEN_FUNCTIONS, edited, read_dir, valid_sources

from graph_cli.kernel.canonical import JsonObject, hash_document, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kinetic import compile as compile_module
from graph_cli.kinetic.compile import (
    MAX_FUNCTIONS,
    MAX_QUERY_CHARS,
    check_catalog,
    compile_catalog,
    single_refs,
)
from graph_cli.kinetic.format import CATALOG_SCHEMA, Problem
from graph_cli.query.cypher import render
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve

MODEL = compiled_model("company", "1.1.0")


def compiled() -> tuple[dict[str, Any], list[Problem]]:
    document, problems = compile_catalog(valid_sources(), MODEL)
    return document, problems


def entries() -> dict[str, dict[str, Any]]:
    document, problems = compiled()
    assert problems == []
    return {e["name"]: e for e in document["functions"]}


def codes(sources: dict[str, str]) -> list[str]:
    return [p.code for p in compile_catalog(sources, MODEL)[1]]


def test_the_valid_functions_compile_without_problems() -> None:
    document, problems = compiled()
    assert problems == []
    assert document["schema"] == CATALOG_SCHEMA
    assert [e["name"] for e in document["functions"]] == sorted(valid_sources_names())


def valid_sources_names() -> list[str]:
    return [name.removesuffix(".yaml") for name in valid_sources()]


def test_catalog_golden() -> None:
    document, _ = compiled()
    assert_golden(GOLDENS / "functions" / "company_1.1.0.json", pretty_bytes(document))


def test_the_compiled_document_is_deterministic_and_hashed() -> None:
    first, second = compiled()[0], compiled()[0]
    assert first == second
    for entry in first["functions"]:
        assert entry["hash"] == hash_document(entry)
    assert len({e["hash"] for e in first["functions"]}) == len(first["functions"])


def test_a_change_to_the_query_changes_only_its_hash() -> None:
    before = entries()
    document, problems = compile_catalog(
        edited("people_by_email_text.yaml", "ORDER BY person", "ORDER BY email"), MODEL
    )
    assert problems == []
    after = {e["name"]: e for e in document["functions"]}
    changed = {n for n in after if after[n]["hash"] != before[n]["hash"]}
    assert changed == {"people_by_email_text"}


def test_the_normalized_query_uses_curies_and_resolves_again() -> None:
    entry = entries()["members_of_unit"]
    assert "(p:`ppl:Person`)-[:`org:holds`]->(q:`org:Position`)" in entry["query"]
    assert "Person" not in entry["query"].replace("ppl:Person", "")
    resolved = resolve(parse(entry["query"]), MODEL, {"unit": "x"})
    assert "\n".join(render.clause(c) for c in resolved.query.clauses) == entry["query"]
    for name, found in entries().items():
        again = resolve(parse(found["query"]), MODEL, dict.fromkeys(_params(found), "2000-01-01"))
        assert "\n".join(render.clause(c) for c in again.query.clauses) == found["query"], name


def test_a_parameter_first_in_a_condition_reads_back() -> None:
    # The stored form parenthesizes it as ``(($unit = ...``, which once read
    # as a node pattern with a parameter as property map (F67).
    sources = edited("vacant_positions.yaml", "WHERE d.id = $unit", "WHERE $unit = d.id")
    document, problems = compile_catalog(sources, MODEL)
    assert problems == []
    entry = {e["name"]: e for e in document["functions"]}["vacant_positions"]
    assert "WHERE (($unit = d.id) AND" in entry["query"]
    assert check_catalog(document, MODEL) == []


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        (lambda text: text.replace("MATCH", "MATCH (", 1), "query.syntax"),
        (lambda text: text + " LIMIT 1", "it reads back as another query"),
    ],
)
def test_a_stored_form_that_does_not_read_back_is_refused(
    monkeypatch: pytest.MonkeyPatch, stored: Any, reason: str
) -> None:
    normalized = compile_module._normalized
    monkeypatch.setattr(compile_module, "_normalized", lambda r: stored(normalized(r)))
    sources = {"vacant_positions.yaml": valid_sources()["vacant_positions.yaml"]}
    document, problems = compile_catalog(sources, MODEL)
    assert [p.code for p in problems] == ["function.round_trip_failed"]
    assert reason in problems[0].message
    assert problems[0].details["normalized"].startswith("MATCH")
    assert document["functions"] == []


def _params(entry: dict[str, Any]) -> list[str]:
    return [p["name"] for p in entry["params"]]


def test_touches_name_types_and_relations_with_their_modules() -> None:
    touches = entries()["missing_required_trainings"]["touches"]
    assert touches["types"] == [
        {"curie": "ops:Training", "module": "operations"},
        {"curie": "org:OrgUnit", "module": "organization"},
        {"curie": "org:Position", "module": "organization"},
        {"curie": "ppl:Person", "module": "people"},
    ]
    assert [r["curie"] for r in touches["relations"]] == [
        "ops:completed",
        "ops:required_for",
        "org:holds",
        "org:in_unit",
    ]


def test_field_uses_carry_their_datatypes() -> None:
    fields = {(f["variable"], f["field"]): f for f in entries()["training_completions"]["fields"]}
    assert fields[("c", "on")]["datatypes"] == ["date"]
    assert fields[("t", "title")]["datatypes"] == ["string"]


def test_the_module_prefix_is_stored_as_the_module_id() -> None:
    sources = edited("people_by_email_text.yaml", "module: people", "module: ppl")
    document, problems = compile_catalog(sources, MODEL)
    assert problems == []
    assert {e["name"]: e["module"] for e in document["functions"]}["people_by_email_text"] == (
        "people"
    )


def test_a_deprecated_function_may_name_an_active_replacement() -> None:
    assert entries()["unit_members"]["replaced_by"] == "members_of_unit"
    assert (
        codes(
            edited(
                "unit_members.yaml", "replaced_by: members_of_unit", "replaced_by: vacant_positions"
            )
        )
        == []
    )
    assert codes(
        edited("unit_members.yaml", "replaced_by: members_of_unit", "replaced_by: unit_members")
    ) == ["function.invalid_value"]


# One broken file per rule: the file name is the case.
EXPECTED = {
    "invalid_yaml": "function.invalid_yaml",
    "unsupported_schema": "function.unsupported_schema",
    "unknown_key": "function.unknown_key",
    "missing_key": "function.missing_key",
    "invalid_value": "function.invalid_value",
    "name_mismatch": "function.name_mismatch",
    "unknown_module": "function.unknown_module",
    "module_required": "function.module_required",
    "builtin_module": "function.builtin_module",
    "replaced_by_invalid": "function.replaced_by_invalid",
    "query_syntax": "query.syntax",
    "term_unknown": "term.unknown",
    "unbound_variable": "query.unbound_variable",
    "parameter_undeclared": "function.parameter_undeclared",
    "parameter_unused": "function.parameter_unused",
    "parameter_paging": "function.parameter_paging",
    "unknown_class": "function.unknown_class",
    "unknown_enum": "function.unknown_enum",
    "invalid_default": "function.invalid_default",
    "invalid_example": "function.invalid_example",
    "type_mismatch": "function.type_mismatch",
    "pattern_impossible": "function.pattern_impossible",
    "returns_star": "function.returns_star",
    "returns_mismatch": "function.returns_mismatch",
    "returns_type_mismatch": "function.returns_type_mismatch",
}


def test_every_broken_fixture_has_an_expectation() -> None:
    assert sorted(p.stem for p in BROKEN_FUNCTIONS.glob("*.yaml")) == sorted(EXPECTED)


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_a_broken_function_gets_exactly_its_problem(case: str) -> None:
    sources = read_dir(BROKEN_FUNCTIONS)
    document, problems = compile_catalog({f"{case}.yaml": sources[f"{case}.yaml"]}, MODEL)
    assert [p.code for p in problems] == [EXPECTED[case]]
    assert problems[0].function == case if case != "name_mismatch" else True
    assert document["functions"] == []


def test_all_problems_come_in_one_answer() -> None:
    broken = read_dir(BROKEN_FUNCTIONS)
    sources = {
        k: broken[k] for k in ("returns_star.yaml", "unknown_module.yaml", "term_unknown.yaml")
    }
    document, problems = compile_catalog({**sources, **valid_sources()}, MODEL)
    assert sorted(p.code for p in problems) == [
        "function.returns_star",
        "function.unknown_module",
        "term.unknown",
    ]
    assert len(document["functions"]) == len(valid_sources())


def test_a_query_error_keeps_the_engine_details() -> None:
    sources = {"term_unknown.yaml": read_dir(BROKEN_FUNCTIONS)["term_unknown.yaml"]}
    (problem,) = compile_catalog(sources, MODEL)[1]
    assert problem.path == "query"
    assert problem.details["term"] == "Postion"
    assert "Position" in problem.details["did_you_mean"]


def test_pattern_impossible_names_the_ends_and_what_would_connect() -> None:
    sources = {"pattern_impossible.yaml": read_dir(BROKEN_FUNCTIONS)["pattern_impossible.yaml"]}
    (problem,) = compile_catalog(sources, MODEL)[1]
    assert problem.details["relations"] == ["org:in_unit"]
    assert problem.details["source"] == ["org:Position"]
    assert problem.details["target"] == ["ppl:Person"]
    assert problem.details["connecting"] == {
        "direct": [{"relation": "org:holds", "from": "ppl:Person", "to": "org:Position"}],
        "paths": [],
    }
    assert (problem.details["line"], problem.details["column"]) == (1, 19)


def minimal(query: str, columns: str = "name", **params: str) -> dict[str, str]:
    """One small function around ``query``; ``params`` map a name to its declaration."""
    declared = "".join(
        f"  {name}:\n    summary: A parameter.\n    {decl.replace('; ', chr(10) + '    ')}\n"
        for name, decl in params.items()
    )
    text = (
        "schema: graph_cli.function/1\nname: probe\nmodule: organization\n"
        "summary: A probe.\nuse_when: Testing.\n"
        + (f"params:\n{declared}" if declared else "")
        + "query: |\n"
        + "".join(f"  {line}\n" for line in query.splitlines())
        + "returns:\n"
        + "".join(f"  - column: {c}\n    summary: A column.\n" for c in columns.split(","))
        + "examples:\n  - question: A question?\n"
    )
    return {"probe.yaml": text}


UNIT = "type: ref; class: org:OrgUnit"


@pytest.mark.parametrize(
    ("old", "new", "possible"),
    [
        # a subclass satisfies the domain and range of the relation
        ("(q:Position)-[:in_unit]->(u:OrgUnit)", "(q:Position)-[:in_unit]->(u:Team)", True),
        # the relation points the other way
        ("(q:Position)-[:in_unit]->(u:OrgUnit)", "(q:Position)<-[:in_unit]-(u:OrgUnit)", False),
        # a pattern predicate is checked as well
        ("NOT (:Person)-[:holds]->(q)", "NOT (:OrgUnit)-[:holds]->(q)", False),
        # either direction is enough for an undirected pattern
        ("NOT (:Person)-[:holds]->(q)", "NOT (q)-[:holds]-(:Person)", True),
    ],
)
def test_possible_patterns(old: str, new: str, possible: bool) -> None:
    found = codes(edited("vacant_positions.yaml", old, new))
    assert (found == []) is possible, found


def test_one_end_without_a_label_is_checked_against_the_other() -> None:
    assert codes(minimal("MATCH (:Training)-[:holds]->() RETURN 1 AS name")) == [
        "function.pattern_impossible"
    ]
    assert codes(minimal("MATCH ()-[:holds]->(q) RETURN q.title AS name")) == []
    assert codes(minimal("MATCH (q:Position)-[:holds]->() RETURN q.title AS name")) == [
        "function.pattern_impossible"
    ]


def test_variable_length_paths_keep_the_empty_path() -> None:
    def members(pattern: str) -> list[str]:
        return codes(edited("members_of_unit.yaml", "[:part_of*0..5]->(d:OrgUnit)", pattern))

    assert members("[:part_of*0..5]->(d:Team)") == []
    # the empty path joins a unit with itself, a person is never a unit
    assert members("[:part_of*0..5]->(d:Person)") == ["function.pattern_impossible"]
    assert members("[:part_of*1..5]->(d:Person)") == ["function.pattern_impossible"]
    assert members("[:part_of*0..5]->(d:Position)") == ["function.pattern_impossible"]


def test_labels_that_exclude_each_other_through_a_variable_are_impossible() -> None:
    query = "MATCH (a:Position), (a:Team) RETURN a.id AS name"
    assert codes(minimal(query)) == ["function.pattern_impossible"]
    assert codes(minimal("MATCH (a:OrgUnit), (a:Team) RETURN a.id AS name")) == []


@pytest.mark.parametrize(
    ("query", "kind", "expected"),
    [
        ("MATCH (d:OrgUnit) WHERE d.id = $p RETURN d.id AS name", UNIT, []),
        ("MATCH (d:OrgUnit) WHERE $p = d.id RETURN d.id AS name", UNIT, []),
        ("MATCH (d:OrgUnit) WHERE d.alias <> $p RETURN d.id AS name", UNIT, []),
        ("MATCH (d:OrgUnit {id: $p}) RETURN d.id AS name", UNIT, []),
        ("MATCH (:OrgUnit {id: $p})-[:part_of]->(d) RETURN d.id AS name", UNIT, []),
        ("MATCH (d:OrgUnit) WHERE d.id IN $p RETURN d.id AS name", UNIT + "; many: true", []),
        # a ref is compared with an id or an alias, nothing else
        (
            "MATCH (d:OrgUnit) WHERE d.name = $p RETURN d.id AS name",
            UNIT,
            ["function.type_mismatch"],
        ),
        ("MATCH (d:OrgUnit {name: $p}) RETURN d.id AS name", UNIT, ["function.type_mismatch"]),
        (
            "MATCH (:OrgUnit {name: $p})-[:part_of]->(d) RETURN d.id AS name",
            UNIT,
            ["function.type_mismatch"],
        ),
        # scalar types against the field's base type
        ("MATCH (g:Guideline) WHERE g.effective_from <= $p RETURN g.id AS name", "type: date", []),
        (
            "MATCH (g:Guideline) WHERE g.effective_from <= $p RETURN g.id AS name",
            "type: datetime",
            ["function.type_mismatch"],
        ),
        (
            "MATCH (g:Guideline) WHERE g.effective_from <= $p RETURN g.id AS name",
            "type: string",
            ["function.type_mismatch"],
        ),
        ("MATCH (g:Guideline) WHERE g.rev >= $p RETURN g.id AS name", "type: integer", []),
        ("MATCH (g:Guideline) WHERE g.rev >= $p RETURN g.id AS name", "type: decimal", []),
        (
            "MATCH (g:Guideline) WHERE g.title = $p RETURN g.id AS name",
            "type: boolean",
            ["function.type_mismatch"],
        ),
        # lists: IN takes a list parameter, = takes a single value
        (
            "MATCH (g:Guideline) WHERE g.title IN $p RETURN g.id AS name",
            "type: string; many: true",
            [],
        ),
        (
            "MATCH (g:Guideline) WHERE g.title IN $p RETURN g.id AS name",
            "type: string",
            ["function.type_mismatch"],
        ),
        (
            "MATCH (g:Guideline) WHERE g.title = $p RETURN g.id AS name",
            "type: string; many: true",
            ["function.type_mismatch"],
        ),
        (
            "MATCH (g:Guideline) WHERE $p IN g.title RETURN g.id AS name",
            "type: string",
            ["function.type_mismatch"],
        ),
        ("MATCH (d:OrgUnit) WHERE $p IN d.alt_labels RETURN d.id AS name", "type: string", []),
        # text matching
        ("MATCH (g:Guideline) WHERE g.title CONTAINS $p RETURN g.id AS name", "type: string", []),
        (
            "MATCH (g:Guideline) WHERE g.title CONTAINS $p RETURN g.id AS name",
            "type: integer",
            ["function.type_mismatch"],
        ),
        (
            "MATCH (g:Guideline) WHERE g.effective_from STARTS WITH $p RETURN g.id AS name",
            "type: string",
            ["function.type_mismatch"],
        ),
        # a qualifier of a relationship variable
        (
            "MATCH ()-[c:completed]->() WHERE c.on >= $p RETURN c.edge_id AS name",
            "type: date",
            [],
        ),
        (
            "MATCH ()-[c:completed]->() WHERE c.on >= $p RETURN c.edge_id AS name",
            "type: integer",
            ["function.type_mismatch"],
        ),
        # outside a comparison nothing is checked
        ("MATCH (g:Guideline) RETURN $p AS name", "type: integer", []),
    ],
)
def test_type_fit(query: str, kind: str, expected: list[str]) -> None:
    assert codes(minimal(query, p=kind)) == expected


def test_limits() -> None:
    long_query = edited(
        "people_by_email_text.yaml",
        "ORDER BY person",
        "ORDER BY person /* " + "x" * MAX_QUERY_CHARS + " */",
    )
    assert "function.query_too_long" in codes(long_query)
    many = {f"fn_{i:04d}.yaml": "" for i in range(MAX_FUNCTIONS + 1)}
    assert "function.too_many_functions" in codes(many)


# The release invariant (K5)


def catalog() -> dict[str, Any]:
    return compile_catalog(valid_sources(), MODEL)[0]


def test_a_published_catalog_holds_against_its_own_model() -> None:
    assert check_catalog(catalog(), MODEL) == []


def test_a_model_without_a_touched_relation_breaks_the_functions_using_it() -> None:
    relations = {k: v for k, v in MODEL.relations.items() if k != "org:in_unit"}
    other = replace(MODEL, relations=relations)
    broken = sorted({p.function for p in check_catalog(catalog(), other)})
    assert broken == [
        "guidelines_in_effect",
        "members_of_unit",
        "missing_required_trainings",
        "unit_members",
        "vacant_positions",
    ]
    stale = [p for p in check_catalog(catalog(), other) if p.code == "function.stale"]
    assert {p.function for p in stale} == set(broken)


def test_a_release_that_only_adds_terms_keeps_the_catalog() -> None:
    added = replace(
        MODEL,
        classes={**MODEL.classes, "org:Division": MODEL.classes["org:Team"]},
        relations={**MODEL.relations, "org:covers": MODEL.relations["org:part_of"]},
    )
    assert check_catalog(catalog(), added) == []


def test_an_added_term_can_make_a_stored_short_name_ambiguous() -> None:
    twin = replace(MODEL, classes={**MODEL.classes, "ops:Position": MODEL.classes["org:Position"]})
    problems = check_catalog(catalog(), twin)
    assert {p.code for p in problems} == {"query.ambiguous_term"}
    # the stored normalized text uses CURIEs and still resolves
    assert not [p for p in problems if p.code == "function.stale"]


def test_a_removed_field_breaks_the_functions_using_it() -> None:
    title = {k: v for k, v in MODEL.classes["org:Position"].attributes.items() if k != "title"}
    position = replace(MODEL.classes["org:Position"], attributes=title)
    other = replace(MODEL, classes={**MODEL.classes, "org:Position": position})
    assert {p.function for p in check_catalog(catalog(), other)} == {
        "members_of_unit",
        "unit_members",
        "vacant_positions",
    }


def test_check_catalog_refuses_other_documents() -> None:
    with pytest.raises(GraphCliError) as caught:
        check_catalog({"schema": "graph_cli.functions/2", "functions": []}, MODEL)
    assert caught.value.code == "format.unsupported_version"
    bad: JsonObject = {"schema": CATALOG_SCHEMA, "functions": [{"name": "x"}]}
    with pytest.raises(JsonShapeError):
        check_catalog(bad, MODEL)


def test_the_catalog_survives_a_json_round_trip() -> None:
    document = catalog()
    assert check_catalog(json.loads(json.dumps(document)), MODEL) == []


# Single-value refs that a list would serve (admin.function_single_ref)


def refs_of(
    sources: dict[str, str], exempt: frozenset[str] = frozenset()
) -> list[tuple[str, str, str]]:
    document, problems = compile_catalog(sources, MODEL)
    assert problems == []
    return single_refs(document, MODEL, exempt)


def test_single_refs_name_each_active_function_with_a_one_value_ref() -> None:
    found = refs_of(valid_sources())
    names = {function for function, _, _ in found}
    assert ("members_of_unit", "unit", "org:OrgUnit") in found
    assert found == sorted(found)  # catalog order is by name
    assert "unit_members" not in names  # deprecated
    assert "training_completions" not in names  # no ref


def test_single_refs_skip_the_exempt_functions() -> None:
    found = refs_of(valid_sources(), frozenset({"members_of_unit"}))
    assert "members_of_unit" not in {function for function, _, _ in found}


@pytest.mark.parametrize(
    ("query", "flagged"),
    [
        ("MATCH (u:OrgUnit) WHERE u.id = $p RETURN u.name AS name", True),
        ("MATCH (u:OrgUnit) WHERE $p = u.id RETURN u.name AS name", True),
        ("MATCH (u:OrgUnit {id: $p}) RETURN u.name AS name", True),
        ("MATCH (u:OrgUnit) WHERE u.alias = $p OR u.id = $p RETURN u.name AS name", True),
        ("MATCH (u:OrgUnit) WHERE u.id <> $p RETURN u.name AS name", False),
        ("MATCH (u:OrgUnit) WHERE u.id = $p RETURN $p AS name", False),
        ("MATCH (u:OrgUnit) WHERE u.id = $p OR u.alias <> $p RETURN u.name AS name", False),
        ("MATCH (u:OrgUnit) RETURN $p AS name", False),
    ],
)
def test_single_refs_only_for_parameters_compared_with_equals(query: str, flagged: bool) -> None:
    found = refs_of(minimal(query, p=UNIT))
    assert found == ([("probe", "p", "org:OrgUnit")] if flagged else [])


def test_single_refs_ignore_lists_and_other_types() -> None:
    assert (
        refs_of(
            minimal(
                "MATCH (u:OrgUnit) WHERE u.id IN $p RETURN u.id AS name", p=UNIT + "; many: true"
            )
        )
        == []
    )
    assert (
        refs_of(
            minimal("MATCH (u:OrgUnit) WHERE u.name = $p RETURN u.id AS name", p="type: string")
        )
        == []
    )


def test_single_refs_skip_a_function_with_two_single_value_refs() -> None:
    query = (
        "MATCH (a:OrgUnit)-[:part_of]->(b:OrgUnit) WHERE a.id = $p AND b.id = $q"
        " RETURN a.name AS name"
    )
    assert refs_of(minimal(query, p=UNIT, q=UNIT)) == []
    mixed = "MATCH (a:OrgUnit) WHERE a.id = $p AND a.name = $q RETURN a.name AS name"
    assert refs_of(minimal(mixed, p=UNIT, q="type: string")) == [("probe", "p", "org:OrgUnit")]


def test_single_refs_skip_a_function_that_picks_with_a_limit() -> None:
    picked = "MATCH (u:OrgUnit) WHERE u.id = $p RETURN u.name AS name ORDER BY name LIMIT 1"
    assert refs_of(minimal(picked, p=UNIT)) == []
    paged = "MATCH (u:OrgUnit) WHERE u.id = $p WITH u LIMIT 3 RETURN u.name AS name"
    assert refs_of(minimal(paged, p=UNIT)) == []
    unlimited = "MATCH (u:OrgUnit) WHERE u.id = $p RETURN u.name AS name ORDER BY name"
    assert refs_of(minimal(unlimited, p=UNIT)) == [("probe", "p", "org:OrgUnit")]
