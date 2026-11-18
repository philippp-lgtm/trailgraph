"""Compiling views against the example company model and its functions (plan V9)."""

import copy
from dataclasses import replace
from typing import Any

import pytest
from support.fixtures import GOLDENS, assert_golden
from support.views import (
    BROKEN_VIEWS,
    company_model,
    edited,
    function_entries,
    function_sources,
    read_dir,
    valid_sources,
)

from graph_cli.kernel.canonical import hash_document, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kinetic.params import ParamDef, Problem
from graph_cli.kinetic.view_compile import (
    MAX_VIEWS,
    check_views,
    compile_views,
    deprecated_function_uses,
)
from graph_cli.kinetic.view_format import CATALOG_SCHEMA

MODEL = company_model()
FUNCTIONS = function_entries(MODEL)

ENTITY = (
    "schema: graph_cli.view/1\nname: probe\nmodule: organization\nsummary: A probe.\n"
    "use_when: Testing.\ntakes: {takes}\n{extra}sections:\n{sections}"
)
SET = (
    "schema: graph_cli.view/1\nname: probe\nmodule: operations\nsummary: A probe.\n"
    "use_when: Testing.\n{extra}sections:\n{sections}"
)


def compiled(**kwargs: Any) -> tuple[dict[str, Any], list[Problem]]:
    return compile_views(valid_sources(), MODEL, FUNCTIONS, **kwargs)


def entries() -> dict[str, dict[str, Any]]:
    document, problems = compiled()
    assert problems == []
    return {e["name"]: e for e in document["views"]}


def entity_view(sections: str, takes: str = "org:Team", extra: str = "") -> dict[str, str]:
    return {"probe.yaml": ENTITY.format(takes=takes, extra=extra, sections=sections)}


def set_view(sections: str, extra: str = "") -> dict[str, str]:
    return {"probe.yaml": SET.format(extra=extra, sections=sections)}


def problems_of(
    sources: dict[str, str], functions: Any = FUNCTIONS, model: Any = MODEL
) -> list[Problem]:
    return compile_views(sources, model, functions)[1]


def codes(sources: dict[str, str], **kwargs: Any) -> list[str]:
    return [p.code for p in problems_of(sources, **kwargs)]


def param(name: str, decl: str) -> str:
    body = "".join(f"    {line}\n" for line in decl.split("; "))
    return f"  {name}:\n{body}    summary: A parameter.\n"


def test_the_valid_views_compile_without_problems() -> None:
    document, problems = compiled()
    assert problems == []
    assert document["schema"] == CATALOG_SCHEMA
    names = sorted(n.removesuffix(".yaml") for n in valid_sources())
    assert [e["name"] for e in document["views"]] == names


def test_catalog_golden() -> None:
    document, _ = compiled()
    assert_golden(GOLDENS / "views" / "company_1.1.0.json", pretty_bytes(document))


def test_the_compiled_document_is_deterministic_and_hashed() -> None:
    first, second = compiled()[0], compiled()[0]
    assert first == second
    for entry in first["views"]:
        assert entry["hash"] == hash_document(entry)
    assert len({e["hash"] for e in first["views"]}) == len(first["views"])


def test_the_hash_does_not_depend_on_the_order_of_the_files() -> None:
    forward = valid_sources()
    backward = dict(reversed(list(forward.items())))
    assert (
        compile_views(forward, MODEL, FUNCTIONS)[0] == compile_views(backward, MODEL, FUNCTIONS)[0]
    )


def test_a_change_to_a_section_changes_only_its_hash() -> None:
    before = entries()
    document, problems = compile_views(
        edited("unit_overview.yaml", "order: [title]", "order: [status, title]"), MODEL, FUNCTIONS
    )
    assert problems == []
    after = {e["name"]: e for e in document["views"]}
    assert {n for n in after if after[n]["hash"] != before[n]["hash"]} == {"unit_overview"}


def test_terms_are_normalized_to_curies_and_the_source_is_kept() -> None:
    entry = entries()["position_overview"]
    unit, holder, reports, relations = entry["sections"][1:]
    assert [s["relation"] for s in (unit, holder, reports)] == [
        "org:in_unit",
        "org:holds",
        "org:reports_to",
    ]
    assert relations["relations"] == [{"relation": "org:carries", "direction": "out"}]
    assert entry["module"] == "organization"
    assert entry["source"]["sections"][1]["relation"] == "in_unit"  # as authored
    assert entry["source"]["sections"][3]["relation"] == "reports_to"


def test_the_entry_names_what_it_touches() -> None:
    touches = entries()["unit_overview"]["touches"]
    assert touches["types"] == [{"curie": "org:OrgUnit", "module": "organization"}]
    assert [t["curie"] for t in touches["relations"]] == [
        "ops:applies_to",
        "org:charged_to",
        "org:holds",
        "org:in_unit",
        "org:part_of",
    ]
    assert touches["attributes"] == ["alt_labels", "name", "status", "title", "work_email"]
    assert touches["functions"] == ["members_of_unit"]
    catalog = entries()["org_catalog"]["touches"]
    assert [t["curie"] for t in catalog["types"]] == [
        "org:OrgUnit",
        "org:Position",
        "ppl:Employee",
        "ppl:External",
    ]
    assert catalog["functions"] == ["training_completions"]
    assert "id" not in entries()["tasks_by_state"]["touches"]["attributes"]


def test_a_set_view_has_no_class_and_an_entity_view_does() -> None:
    found = entries()
    assert found["unit_overview"]["takes"] == "org:OrgUnit"
    assert found["unit_overview"]["default"] is True
    assert found["org_catalog"]["takes"] is None
    assert (found["org_catalog"]["page_bytes"], found["org_catalog"]["max_bytes"]) == (65536, None)
    assert (found["unit_overview"]["page_bytes"], found["unit_overview"]["max_bytes"]) == (
        None,
        4096,
    )


EXPECTED = {
    "argument_missing": "view.argument_missing",
    "builtin_module": "view.builtin_module",
    "card": "view.reserved_name",
    "duplicate_section": "view.duplicate_section",
    "flavour_mismatch": "view.flavour_mismatch",
    "invalid_default": "view.invalid_default",
    "invalid_example": "view.invalid_example",
    "invalid_order": "view.invalid_order",
    "invalid_value": "view.invalid_value",
    "invalid_where": "view.invalid_where",
    "invalid_yaml": "view.invalid_yaml",
    "literal_reference": "view.literal_reference",
    "missing_key": "view.missing_key",
    "module_required": "view.module_required",
    "name_mismatch": "view.name_mismatch",
    "parameter_undeclared": "view.parameter_undeclared",
    "parameter_unused": "view.parameter_unused",
    "placeholder_position": "view.placeholder_position",
    "relation_mismatch": "view.relation_mismatch",
    "replaced_by_invalid": "view.replaced_by_invalid",
    "type_mismatch": "view.type_mismatch",
    "unknown_argument": "view.unknown_argument",
    "unknown_attribute": "view.unknown_attribute",
    "unknown_class": "view.unknown_class",
    "unknown_enum": "view.unknown_enum",
    "unknown_function": "view.unknown_function",
    "unknown_key": "view.unknown_key",
    "unknown_module": "view.unknown_module",
    "unknown_relation": "view.unknown_relation",
    "unsupported_schema": "view.unsupported_schema",
}


def test_every_broken_fixture_has_an_expectation() -> None:
    assert sorted(p.stem for p in BROKEN_VIEWS.glob("*.yaml")) == sorted(EXPECTED)


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_a_broken_view_gets_exactly_its_problem(case: str) -> None:
    sources = read_dir(BROKEN_VIEWS)
    document, problems = compile_views({f"{case}.yaml": sources[f"{case}.yaml"]}, MODEL, FUNCTIONS)
    assert [p.code for p in problems] == [EXPECTED[case]]
    assert problems[0].kind == "view"
    assert problems[0].to_json()["view"] == problems[0].name
    assert document["views"] == []


def test_all_problems_come_in_one_answer() -> None:
    broken = read_dir(BROKEN_VIEWS)
    sources = {
        k: broken[k] for k in ("unknown_relation.yaml", "unknown_module.yaml", "invalid_where.yaml")
    }
    document, problems = compile_views({**sources, **valid_sources()}, MODEL, FUNCTIONS)
    assert sorted(p.code for p in problems) == [
        "view.invalid_where",
        "view.unknown_module",
        "view.unknown_relation",
    ]
    assert len(document["views"]) == len(valid_sources())


def test_two_default_views_of_one_class_are_refused() -> None:
    sources = valid_sources()
    sources["z_second_default.yaml"] = sources["unit_overview.yaml"].replace(
        "name: unit_overview", "name: z_second_default"
    )
    document, problems = compile_views(sources, MODEL, FUNCTIONS)
    assert [(p.code, p.name) for p in problems] == [("view.default_taken", "z_second_default")]
    assert problems[0].details == {"takes": "org:OrgUnit", "default": "unit_overview"}
    assert "z_second_default" not in [e["name"] for e in document["views"]]


def test_a_default_of_a_subclass_does_not_clash_with_its_superclass() -> None:
    sources = valid_sources()
    sources["team_default.yaml"] = (
        sources["unit_overview.yaml"]
        .replace("name: unit_overview", "name: team_default")
        .replace("takes: org:OrgUnit", "takes: org:Team")
    )
    assert problems_of(sources) == []


def test_a_class_given_by_its_local_name_is_the_same_default() -> None:
    sources = valid_sources()
    sources["z_second_default.yaml"] = (
        sources["unit_overview.yaml"]
        .replace("name: unit_overview", "name: z_second_default")
        .replace("takes: org:OrgUnit", "takes: OrgUnit")
    )
    assert [p.code for p in problems_of(sources)] == ["view.default_taken"]


def test_too_many_views() -> None:
    sources = {f"v{i:04d}_view.yaml": "x" for i in range(MAX_VIEWS + 1)}
    found = [
        p for p in compile_views(sources, MODEL, FUNCTIONS)[1] if p.code == "view.too_many_views"
    ]
    assert len(found) == 1 and found[0].details == {"count": MAX_VIEWS + 1, "limit": MAX_VIEWS}


def test_a_view_replaced_by_must_be_active() -> None:
    sources = valid_sources()
    assert problems_of(sources) == []
    sources["legacy_unit_overview.yaml"] = sources["legacy_unit_overview.yaml"].replace(
        "replaced_by: unit_overview", "replaced_by: members_report"
    )
    assert problems_of(sources) == []
    deprecated = sources["members_report.yaml"].replace(
        "name: members_report", "name: members_report\nstatus: deprecated"
    )
    sources["members_report.yaml"] = deprecated
    assert [p.code for p in problems_of(sources)] == ["view.replaced_by_invalid"]


# Terms


def test_a_field_must_belong_to_a_class_of_the_section() -> None:
    ok = entity_view("  - kind: fields\n    fields: [name, alt_labels]\n", takes="org:OrgUnit")
    assert codes(ok) == []
    sub = entity_view("  - kind: fields\n    fields: [name]\n", takes="org:OrgUnit")
    assert codes(sub) == []
    # an attribute only a subclass has, and one that no class has
    people = entity_view("  - kind: fields\n    fields: [employee_number]\n", takes="ppl:Person")
    assert codes(people) == []
    bad = entity_view("  - kind: fields\n    fields: [employee_number]\n", takes="org:Team")
    (problem,) = problems_of(bad)
    assert problem.code == "view.unknown_attribute"
    assert problem.path == "sections[0].fields[0]"
    assert problem.details["term"] == "employee_number"
    assert problem.details["kind"] == "attribute"


def test_an_unknown_attribute_suggests_close_names() -> None:
    (problem,) = problems_of(entity_view("  - kind: fields\n    fields: [nme]\n"))
    assert problem.details["did_you_mean"] == ["name"]


def test_a_neighbors_section_reads_the_far_end_of_the_relation() -> None:
    ok = entity_view(
        "  - kind: neighbors\n    relation: org:in_unit\n    direction: in\n"
        "    fields: [title, status]\n",
        takes="org:OrgUnit",
    )
    assert codes(ok) == []
    # title belongs to Position, the far end of in_unit read in, not to the unit
    bad = entity_view(
        "  - kind: neighbors\n    relation: org:in_unit\n    fields: [title]\n",
        takes="org:Position",
    )
    assert codes(bad) == ["view.unknown_attribute"]
    wrong_start = entity_view(
        "  - kind: neighbors\n    relation: org:in_unit\n    direction: in\n", takes="org:Position"
    )
    assert codes(wrong_start) == ["view.relation_mismatch"]


def test_a_relation_must_start_at_the_class_of_the_section() -> None:
    superclass = entity_view(
        "  - kind: relations\n    relations: [org:holds]\n", takes="ppl:Person"
    )
    assert codes(superclass) == []
    subclass = entity_view(
        "  - kind: relations\n    relations: [org:holds]\n", takes="ppl:Employee"
    )
    assert codes(subclass) == []
    inbound = entity_view(
        "  - kind: relations\n    relations: [{relation: org:holds, direction: in}]\n",
        takes="org:Position",
    )
    assert codes(inbound) == []
    wrong = entity_view("  - kind: relations\n    relations: [org:holds]\n", takes="org:Position")
    (problem,) = problems_of(wrong)
    assert problem.code == "view.relation_mismatch"
    assert problem.details["expected"] == ["ppl:Person"]
    assert problem.details["direction"] == "out"


def test_a_row_relation_is_checked_against_the_rows_and_its_far_end() -> None:
    base = (
        "  - kind: list\n    type: org:Position\n    relations:\n"
        "      - {{relation: org:holds, direction: in, {more}}}\n"
    )
    assert codes(set_view(base.format(more="fields: [name]"))) == []
    # the holder is a person: no title there
    assert codes(set_view(base.format(more="fields: [title]"))) == ["view.unknown_attribute"]
    # an attribute the neighbours do not have is the same problem as in a fields list
    assert codes(set_view(base.format(more='where: ["email=x"]'))) == ["view.unknown_attribute"]
    out = base.replace("direction: in", "direction: out")
    assert codes(set_view(out.format(more="fields: [name]"))) == ["view.relation_mismatch"]


def test_a_local_name_that_two_terms_have_is_ambiguous() -> None:
    twin = MODEL.relations["org:part_of"]
    model = replace(MODEL, relations={**MODEL.relations, "ops:part_of": twin})
    view = entity_view("  - kind: relations\n    relations: [part_of]\n", takes="org:OrgUnit")
    (problem,) = problems_of(view, model=model)
    assert problem.code == "view.ambiguous_term"
    assert problem.details["candidates"] == ["ops:part_of", "org:part_of"]
    assert codes(view) == []  # in the real model the local name is unique


def test_an_unknown_relation_or_class_suggests_close_names() -> None:
    (rel,) = problems_of(entity_view("  - kind: relations\n    relations: [org:in_unt]\n"))
    assert rel.code == "view.unknown_relation"
    assert rel.details["did_you_mean"][0] == "org:in_unit"
    assert (rel.details["kind"], rel.details["term"]) == ("relation", "org:in_unt")
    (cls,) = problems_of(entity_view("  - kind: files\n", takes="org:Tam"))
    assert cls.code == "view.unknown_class" and cls.details["did_you_mean"] == ["org:Team"]


def test_a_list_section_names_known_classes() -> None:
    bad = set_view("  - kind: list\n    type: [ops:Task, ops:Nothing]\n")
    assert codes(bad) == ["view.unknown_class"]
    abstract = set_view(
        "  - kind: list\n    type: ppl:Person\n    fields: [name, employee_number]\n"
    )
    assert codes(abstract) == []  # an abstract class lists its subclasses


# where and order


def test_where_and_order_plan_with_the_find_planner() -> None:
    ops = [
        "status=open",
        "due_on:lt=2026-01-01",
        "title:contains=report",
        "due_on:exists=true",
        "status:in=open,done",
    ]
    for entry in ops:
        view = set_view(f'  - kind: list\n    type: ops:Task\n    where: ["{entry}"]\n')
        assert codes(view) == [], entry
    bad = set_view('  - kind: list\n    type: ops:Task\n    where: ["due_on:lt=yesterday"]\n')
    (problem,) = problems_of(bad)
    assert problem.code == "view.invalid_where" and problem.path == "sections[0].where[0]"
    assert problem.details["error"] == "usage.invalid_argument"
    assert "argument" not in problem.details
    assert not problem.message.startswith("--where")


def test_a_planner_error_is_carried_into_the_problem() -> None:
    unknown = set_view('  - kind: list\n    type: ops:Task\n    where: ["staus=open"]\n')
    (problem,) = problems_of(unknown)
    # an unknown attribute is one code wherever it stands, so rename hints reach where and order
    assert problem.code == "view.unknown_attribute" and problem.path == "sections[0].where[0]"
    assert (problem.details["kind"], problem.details["term"]) == ("attribute", "staus")
    assert problem.details["did_you_mean"] == ["status"]
    missing = set_view('  - kind: list\n    type: ops:Task\n    order: ["staus"]\n')
    assert [p.code for p in problems_of(missing)] == ["view.unknown_attribute"]
    order = set_view('  - kind: list\n    type: ops:Task\n    order: ["due_on:sideways"]\n')
    (problem,) = problems_of(order)
    assert problem.code == "view.invalid_order" and problem.path == "sections[0].order[0]"
    assert not problem.message.startswith("--sort")


def test_where_is_checked_against_the_classes_of_the_section() -> None:
    # work_email is an attribute of persons: not of tasks
    bad = set_view('  - kind: list\n    type: ops:Task\n    where: ["work_email=x"]\n')
    assert codes(bad) == ["view.unknown_attribute"]
    ok = set_view('  - kind: list\n    type: ppl:Person\n    where: ["work_email=x"]\n')
    assert codes(ok) == []
    # a neighbors section plans against the far end
    near = entity_view(
        "  - kind: neighbors\n    relation: org:in_unit\n    direction: in\n"
        '    where: ["title=x"]\n',
        takes="org:OrgUnit",
    )
    assert codes(near) == []
    far = entity_view(
        "  - kind: neighbors\n    relation: org:in_unit\n    direction: in\n"
        '    where: ["name=x"]\n',
        takes="org:OrgUnit",
    )
    assert codes(far) == ["view.unknown_attribute"]


@pytest.mark.parametrize(
    ("where", "decl", "expected"),
    [
        ("status=$p", "type: string", []),
        ("status=$p", "type: enum; enum: ppl:ExternalKind", []),
        ("due_on:lt=$p", "type: date", []),
        ("due_on:lt=$p", "type: datetime", ["view.type_mismatch"]),
        ("due_on:lt=$p", "type: string", ["view.type_mismatch"]),
        ("due_on:lt=$p", "type: integer", ["view.type_mismatch"]),
        ("status:in=$p", "type: string; many: true", []),
        ("status:in=$p", "type: string", ["view.type_mismatch"]),
        ("status=$p", "type: string; many: true", ["view.type_mismatch"]),
        ("title:contains=$p", "type: string", []),
        ("title:prefix=$p", "type: integer", ["view.type_mismatch"]),
        ("title:contains=$p", "type: string; many: true", ["view.type_mismatch"]),
        ("due_on:exists=$p", "type: boolean", ["view.type_mismatch"]),
        ("id=$p", "type: ref; class: ops:Task", []),
        ("title=$p", "type: ref; class: ops:Task", ["view.type_mismatch"]),
        ("tx_seq:gt=$p", "type: integer", []),
        ("tx_seq:gt=$p", "type: string", ["view.type_mismatch"]),
    ],
)
def test_a_parameter_must_fit_the_attribute_it_is_compared_with(
    where: str, decl: str, expected: list[str]
) -> None:
    view = set_view(
        f'  - kind: list\n    type: ops:Task\n    where: ["{where}"]\n',
        extra="params:\n" + param("p", decl),
    )
    assert codes(view) == expected


def test_a_decimal_attribute_takes_integer_and_decimal_parameters() -> None:
    sections = '  - kind: list\n    type: leg:Shareholding\n    where: ["percent:ge=$p"]\n'
    for decl in ("type: decimal", "type: integer"):
        view = set_view(sections, extra="params:\n" + param("p", decl)).copy()
        view["probe.yaml"] = view["probe.yaml"].replace("module: operations", "module: legal")
        assert codes(view) == [], decl
    bad = set_view(sections, extra="params:\n" + param("p", "type: boolean"))
    bad["probe.yaml"] = bad["probe.yaml"].replace("module: operations", "module: legal")
    assert codes(bad) == ["view.type_mismatch"]


def test_entity_is_not_a_where_value_and_an_undeclared_name_is_refused() -> None:
    entity = entity_view(
        "  - kind: neighbors\n    relation: org:in_unit\n    direction: in\n"
        '    where: ["title=$entity"]\n',
        takes="org:OrgUnit",
    )
    assert codes(entity) == ["view.placeholder_position"]
    undeclared = set_view('  - kind: list\n    type: ops:Task\n    where: ["status=$nope"]\n')
    assert codes(undeclared) == ["view.parameter_undeclared"]


def test_a_parameter_that_nothing_uses_is_refused_and_a_used_one_is_not() -> None:
    declared = "params:\n" + param("p", "type: string; default: open")
    unused = set_view("  - kind: list\n    type: ops:Task\n", extra=declared)
    assert codes(unused) == ["view.parameter_unused"]
    used = set_view(
        '  - kind: list\n    type: ops:Task\n    where: ["status=$p"]\n', extra=declared
    )
    assert codes(used) == []
    in_args = set_view(
        "  - kind: rows\n    function: training_completions\n    args: {titles: [a], since: $p}\n",
        extra="params:\n" + param("p", "type: date; default: 2026-01-01"),
    )
    assert codes(in_args) == []


def test_parameters_are_checked_like_those_of_functions() -> None:
    sections = '  - kind: list\n    type: ops:Task\n    where: ["status=$p"]\n'

    def view(decl: str) -> dict[str, str]:
        return set_view(sections, extra="params:\n" + param("p", decl))

    assert "view.unknown_class" in codes(view("type: ref; class: org:Nothing"))
    assert "view.unknown_enum" in codes(view("type: enum; enum: ppl:Nothing"))
    assert codes(view("type: string; default: open")) == []
    assert codes(view("type: integer; default: x")) == [
        "view.invalid_default",
        "view.type_mismatch",
    ]
    refs = view("type: ref; class: ops:Task; default: abcdef12")
    assert "view.invalid_default" in codes(refs)


def test_examples_name_declared_parameters_and_no_entity() -> None:
    sections = '  - kind: list\n    type: ops:Task\n    where: ["status=$p"]\n'
    decl = "params:\n" + param("p", "type: string; default: open")

    def view(example: str) -> dict[str, str]:
        return set_view(sections, extra=decl + "examples:\n  - question: Q.\n" + example)

    assert codes(view("    params: {p: done}\n")) == []
    assert codes(view("    params: {q: done}\n")) == ["view.invalid_example"]
    ref = set_view(
        "  - kind: rows\n    function: members_of_unit\n    args: {unit: $p}\n",
        extra="params:\n"
        + param("p", "type: ref; class: org:OrgUnit")
        + "examples:\n  - question: Q.\n    params: {p: TEAM-0001}\n",
    )
    assert codes(ref) == ["view.invalid_example"]


# rows sections


def test_entity_binds_a_ref_parameter_of_its_class_or_a_superclass() -> None:
    rows = "  - kind: rows\n    function: {fn}\n    args: {{{arg}: $entity}}\n"
    assert codes(entity_view(rows.format(fn="members_of_unit", arg="unit"), takes="org:Team")) == []
    assert (
        codes(entity_view(rows.format(fn="members_of_unit", arg="unit"), takes="org:OrgUnit")) == []
    )
    assert (
        codes(entity_view(rows.format(fn="reporting_chain", arg="position"), takes="org:Position"))
        == []
    )
    wrong = entity_view(rows.format(fn="reporting_chain", arg="position"), takes="org:Team")
    (problem,) = problems_of(wrong)
    assert problem.code == "view.type_mismatch" and problem.path == "sections[0].args.position"
    # the entity of a view of the superclass may not be what the function wants
    wider = entity_view(rows.format(fn="employer_of", arg="person"), takes="ppl:Person")
    assert codes(wider) == ["view.type_mismatch"]
    narrower = entity_view(rows.format(fn="employer_of", arg="person"), takes="ppl:Employee")
    assert codes(narrower) == []
    not_ref = entity_view(
        "  - kind: rows\n    function: training_completions\n    args: {titles: $entity}\n"
    )
    assert codes(not_ref) == ["view.type_mismatch"]


def test_a_set_view_has_no_entity() -> None:
    view = set_view("  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n")
    assert codes(view) == ["view.placeholder_position"]


@pytest.mark.parametrize(
    ("decl", "expected"),
    [
        ("type: ref; class: org:OrgUnit", []),
        ("type: ref; class: org:Department", []),  # a subclass feeds a parameter of its superclass
        ("type: ref; class: org:Position", ["view.type_mismatch"]),
        ("type: ref; class: org:OrgUnit; many: true", ["view.type_mismatch"]),
        ("type: string", ["view.type_mismatch"]),
    ],
)
def test_a_view_parameter_feeds_a_function_parameter_of_a_fitting_type(
    decl: str, expected: list[str]
) -> None:
    view = set_view(
        "  - kind: rows\n    function: members_of_unit\n    args: {unit: $p}\n",
        extra="params:\n" + param("p", decl),
    )
    assert codes(view) == expected


def test_view_parameters_of_other_types_feed_function_parameters() -> None:
    def view(args: str, decl: str) -> dict[str, str]:
        return set_view(
            f"  - kind: rows\n    function: training_completions\n    args: {args}\n",
            extra="params:\n" + param("p", decl),
        )

    assert codes(view("{titles: $p}", "type: string; many: true")) == []
    assert codes(view("{titles: $p}", "type: string")) == ["view.type_mismatch"]
    assert codes(view("{titles: [a], since: $p}", "type: date")) == []
    assert codes(view("{titles: [a], since: $p}", "type: datetime")) == ["view.type_mismatch"]
    assert codes(view("{titles: [a], since: $p}", "type: string")) == ["view.type_mismatch"]


def test_a_literal_binds_like_a_run_value() -> None:
    def view(args: str) -> dict[str, str]:
        return set_view(f"  - kind: rows\n    function: training_completions\n    args: {args}\n")

    assert codes(view("{titles: [a, b], since: 2026-01-01}")) == []
    assert codes(view("{titles: [a]}")) == []  # since has a default
    assert codes(view("{titles: a}")) == ["view.invalid_value"]  # many wants a list
    assert codes(view("{titles: [a], since: yesterday}")) == ["view.invalid_value"]
    assert codes(view("{titles: [a], since: 5}")) == ["view.invalid_value"]
    assert codes(view("{since: 2026-01-01}")) == ["view.argument_missing"]


def test_an_unknown_function_suggests_close_names() -> None:
    view = set_view("  - kind: rows\n    function: members_of_unt\n    args: {unit: x}\n")
    (problem,) = problems_of(view)
    assert problem.code == "view.unknown_function"
    assert problem.details["did_you_mean"] == ["members_of_unit"]
    assert codes(view, functions=()) == ["view.unknown_function"]


def test_missing_and_unknown_arguments_name_the_function() -> None:
    missing = set_view("  - kind: rows\n    function: members_of_unit\n")
    (problem,) = problems_of(missing)
    assert problem.code == "view.argument_missing"
    assert problem.details == {"function": "members_of_unit", "argument": "unit"}
    unknown = set_view(
        "  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity, x: 1}\n"
    )
    assert sorted(codes(unknown)) == ["view.placeholder_position", "view.unknown_argument"]


def test_a_deprecated_function_is_a_warning_and_not_a_refusal() -> None:
    sources = function_sources()
    sources["members_of_unit.yaml"] = sources["members_of_unit.yaml"].replace(
        "featured: true", "featured: true\nstatus: deprecated\nreplaced_by: vacant_positions"
    )
    functions = function_entries(MODEL, sources)
    document, problems = compile_views(valid_sources(), MODEL, functions)
    assert problems == []
    warnings = deprecated_function_uses(document, functions)
    assert [(w.name, w.code, w.path) for w in warnings] == [
        ("legacy_unit_overview", "view.function_deprecated", "sections[1].function"),
        ("members_report", "view.function_deprecated", "sections[0].function"),
        ("unit_overview", "view.function_deprecated", "sections[3].function"),
    ]
    assert warnings[0].details == {"function": "members_of_unit", "replaced_by": "vacant_positions"}
    assert deprecated_function_uses(document, FUNCTIONS) == []


# check_views: the release invariant


def document() -> dict[str, Any]:
    return compiled()[0]


def test_a_published_catalog_still_holds_against_the_same_model() -> None:
    assert check_views(document(), MODEL, FUNCTIONS) == []


def test_a_removed_attribute_breaks_the_views_that_name_it() -> None:
    classes = dict(MODEL.classes)
    position = classes["org:Position"]
    attributes = {k: v for k, v in position.attributes.items() if k != "title"}
    classes["org:Position"] = replace(position, attributes=attributes)
    problems = check_views(document(), replace(MODEL, classes=classes), FUNCTIONS)
    broken = sorted({p.name for p in problems})
    assert broken == ["org_catalog", "position_overview", "unit_overview"]
    assert {p.code for p in problems} == {"view.unknown_attribute"}  # fields, order and where


def test_a_removed_relation_breaks_the_views_that_name_it() -> None:
    relations = {k: v for k, v in MODEL.relations.items() if k != "org:charged_to"}
    problems = check_views(document(), replace(MODEL, relations=relations), FUNCTIONS)
    assert [(p.name, p.code) for p in problems] == [("unit_overview", "view.unknown_relation")]


def test_a_removed_function_breaks_the_views_that_use_it() -> None:
    rest = tuple(f for f in FUNCTIONS if f.name != "members_of_unit")
    problems = check_views(document(), MODEL, rest)
    assert sorted((p.name, p.code) for p in problems) == [
        ("legacy_unit_overview", "view.unknown_function"),
        ("members_report", "view.unknown_function"),
        ("unit_overview", "view.unknown_function"),
    ]


def test_a_function_that_changed_a_parameter_breaks_the_view_that_binds_it() -> None:
    changed = []
    for entry in FUNCTIONS:
        if entry.name == "members_of_unit":
            params = (ParamDef("unit", "ref", "A position.", of_class="org:Position"),)
            entry = replace(entry, defn=replace(entry.defn, params=params))
        changed.append(entry)
    problems = check_views(document(), MODEL, tuple(changed))
    assert sorted((p.name, p.code) for p in problems) == [
        ("legacy_unit_overview", "view.type_mismatch"),
        ("members_report", "view.type_mismatch"),
        ("unit_overview", "view.type_mismatch"),
    ]


def test_a_stored_entry_that_reads_differently_than_its_source_is_stale() -> None:
    doc = document()
    tampered = copy.deepcopy(doc)
    entry = next(e for e in tampered["views"] if e["name"] == "unit_overview")
    entry["source"]["sections"][2]["order"] = ["status"]  # the source changed, the entry did not
    problems = check_views(tampered, MODEL, FUNCTIONS)
    assert [(p.name, p.code) for p in problems] == [("unit_overview", "view.stale")]
    assert "differently" in problems[0].message


def test_a_stored_form_that_no_longer_compiles_is_stale() -> None:
    doc = document()
    entry = next(e for e in doc["views"] if e["name"] == "unit_overview")
    entry["sections"][2]["relation"] = "org:nope"  # the stored form names a relation that is gone
    (problem,) = check_views(doc, MODEL, FUNCTIONS)
    assert (problem.name, problem.code) == ("unit_overview", "view.stale")
    assert "no longer compiles" in problem.message


def test_a_release_that_only_changes_a_module_id_breaks_no_view() -> None:
    """The hash covers the module ids; the invariant does not (as for functions and actions)."""
    modules = tuple(
        replace(m, id="organisation") if m.id == "organization" else m for m in MODEL.modules
    )
    renamed = replace(MODEL, modules=modules)
    problems = check_views(document(), renamed, FUNCTIONS)
    # the views of the module name it: that is a real problem; the others still hold
    assert {p.name for p in problems if p.code == "view.stale"} == set()
    others = {n: t for n, t in valid_sources().items() if "module: operations" in t}
    compiled = compile_views(others, MODEL, FUNCTIONS)[0]
    assert check_views(compiled, renamed, FUNCTIONS) == []
    entry = compiled["views"][0]
    assert entry["touches"]["types"][0]["module"] == "operations"
    again = compile_views(others, renamed, FUNCTIONS)[0]
    assert again["views"][0]["hash"] == entry["hash"]  # no module changed here


def test_the_names_of_an_entry_and_its_source_must_agree() -> None:
    doc = document()
    entry = next(e for e in doc["views"] if e["name"] == "members_report")
    entry["source"]["name"] = "another_name"
    (problem,) = check_views(doc, MODEL, FUNCTIONS)
    assert (problem.name, problem.code) == ("members_report", "view.stale")


def test_a_stored_entry_that_does_not_read_is_stale() -> None:
    doc = document()
    entry = next(e for e in doc["views"] if e["name"] == "members_report")
    entry["source"]["sections"][0]["kind"] = "nonsense"
    assert [(p.name, p.code) for p in check_views(doc, MODEL, FUNCTIONS)] == [
        ("members_report", "view.stale")
    ]


def test_check_views_refuses_a_document_of_another_shape() -> None:
    with pytest.raises(GraphCliError) as exc:
        check_views({"schema": "graph_cli.view_catalog/9", "views": []}, MODEL, FUNCTIONS)
    assert exc.value.code == "format.unsupported_version"
    with pytest.raises(JsonShapeError):
        check_views({"schema": CATALOG_SCHEMA}, MODEL, FUNCTIONS)


def test_the_published_check_repeats_the_default_rule() -> None:
    doc = document()
    twin = copy.deepcopy(next(e for e in doc["views"] if e["name"] == "unit_overview"))
    twin["name"] = twin["source"]["name"] = "zz_twin"
    doc["views"].append(twin)
    assert [(p.name, p.code) for p in check_views(doc, MODEL, FUNCTIONS)] == [
        ("zz_twin", "view.default_taken")
    ]


# review of step 1: where operands, unused parameters, touches


def test_lang_takes_a_string_parameter() -> None:
    sections = '  - kind: list\n    type: ops:Task\n    where: ["title:lang=$p"]\n'
    view = set_view(sections, extra="params:\n" + param("p", "type: string; default: de"))
    model = _with_language_attribute()
    assert [p.code for p in problems_of(view, model=model)] == []
    assert codes(view) == ["view.invalid_where"]  # tasks have no language-tagged attribute


def _with_language_attribute() -> Any:
    from support.cards import with_attribute

    return with_attribute(MODEL, "ops:Task", "title", "title", type="lang_string")


def test_a_list_parameter_is_planned_as_a_list_whatever_a_value_looks_like() -> None:
    from graph_cli.query.plan import plan_clauses

    pred, _ = plan_clauses(MODEL, ("ops:Task",), [("title:in", ["a,b", "c"])])
    assert pred is not None and list(pred.value) == ["a,b", "c"]  # type: ignore[attr-defined]


def test_no_false_unused_parameter_follows_a_skipped_section() -> None:
    declared = "params:\n" + param("p", "type: string; default: open")
    where = '    where: ["status=$p"]\n'
    cases = [
        (
            set_view("  - kind: list\n    type: [ops:Task, ops:Nope]\n" + where, extra=declared),
            ["view.unknown_class"],
        ),
        (
            entity_view("  - kind: neighbors\n    relation: org:nope\n" + where, extra=declared),
            ["view.unknown_relation"],
        ),
        (
            entity_view("  - kind: neighbors\n    relation: org:holds\n" + where, extra=declared),
            ["view.relation_mismatch"],
        ),
        (
            entity_view(
                "  - kind: neighbors\n    relation: org:in_unit\n    direction: in\n" + where,
                takes="org:Nope",
                extra=declared,
            ),
            ["view.unknown_class"],
        ),
    ]
    for sources, expected in cases:
        assert codes(sources) == expected


def test_the_classes_of_ref_parameters_are_touched() -> None:
    touches = entries()["members_report"]["touches"]
    assert [t["curie"] for t in touches["types"]] == ["org:Department"]


# review of step 2: a decimal parameter against an integer attribute


def test_a_decimal_parameter_does_not_fit_an_integer_attribute() -> None:
    def view(decl: str) -> dict[str, str]:
        found = set_view(
            '  - kind: list\n    type: ops:Task\n    where: ["rev:ge=$p"]\n',
            extra="params:\n" + param("p", decl),
        )
        return found

    assert codes(view("type: integer")) == []  # an integer fits an integer system field
    assert codes(view("type: decimal")) == ["view.type_mismatch"]  # 1.5 would fail at render
