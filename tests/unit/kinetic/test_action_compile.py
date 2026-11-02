"""Compiling actions against the example company model (actions plan A9)."""

from typing import Any

import pytest
from support.actions import BROKEN_ACTIONS, company_model, edited, read_dir, valid_sources
from support.fixtures import GOLDENS, assert_golden, compiled_model

from graph_cli.kernel.canonical import hash_document, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kinetic.action_compile import (
    MAX_ACTIONS,
    MAX_OPS,
    MAX_PRECONDITIONS,
    check_actions,
    compile_actions,
)
from graph_cli.kinetic.action_format import CATALOG_SCHEMA
from graph_cli.kinetic.params import Problem

MODEL = company_model()


def compiled() -> tuple[dict[str, Any], list[Problem]]:
    return compile_actions(valid_sources(), MODEL)


def entries() -> dict[str, dict[str, Any]]:
    document, problems = compiled()
    assert problems == []
    return {e["name"]: e for e in document["actions"]}


def probe(ops: str, params: str = "", *, pre: str = "", extra: str = "") -> dict[str, str]:
    """One small action named ``probe`` around ``ops`` (YAML lines) and ``params``."""
    text = (
        "schema: graph_cli.action/1\nname: probe\nmodule: organization\n"
        "summary: A probe.\nuse_when: Testing.\n"
        + (f"params:\n{params}" if params else "")
        + pre
        + f"ops:\n{ops}"
        + "examples:\n  - request: A request.\n"
        + extra
    )
    return {"probe.yaml": text}


def param(name: str, decl: str) -> str:
    body = "".join(f"    {line}\n" for line in decl.split("; "))
    return f"  {name}:\n{body}    summary: A parameter.\n"


PERSON = param("person", "type: ref; class: ppl:Person")
POSITION = param("position", "type: ref; class: org:Position")


def codes(sources: dict[str, str], **kwargs: Any) -> list[str]:
    return [p.code for p in compile_actions(sources, MODEL, **kwargs)[1]]


def test_the_valid_actions_compile_without_problems() -> None:
    document, problems = compiled()
    assert problems == []
    assert document["schema"] == CATALOG_SCHEMA
    names = sorted(n.removesuffix(".yaml") for n in valid_sources())
    assert [e["name"] for e in document["actions"]] == names


def test_catalog_golden() -> None:
    document, _ = compiled()
    assert_golden(GOLDENS / "actions" / "company_1.1.0.json", pretty_bytes(document))


def test_the_compiled_document_is_deterministic_and_hashed() -> None:
    first, second = compiled()[0], compiled()[0]
    assert first == second
    for entry in first["actions"]:
        assert entry["hash"] == hash_document(entry)
    assert len({e["hash"] for e in first["actions"]}) == len(first["actions"])


def test_a_change_to_an_op_changes_only_its_hash() -> None:
    before = entries()
    document, problems = compile_actions(
        edited("assign_position.yaml", "since: $since", "since: $since, note: x"), MODEL
    )
    assert [p.code for p in problems] == ["action.unknown_qualifier"]
    document, problems = compile_actions(
        edited("assign_position.yaml", "Date the person takes over.", "The day it starts."), MODEL
    )
    assert problems == []
    after = {e["name"]: e for e in document["actions"]}
    assert {n for n in after if after[n]["hash"] != before[n]["hash"]} == {"assign_position"}


def test_ops_are_normalized_to_curies() -> None:
    sources = probe(
        "  - {op: link, id: $person, rel: holds, target: $position}\n", PERSON + POSITION
    )
    document, problems = compile_actions(sources, MODEL)
    assert problems == []
    (entry,) = document["actions"]
    assert entry["ops"][0]["rel"] == "org:holds"
    assert entry["source"]["ops"][0]["rel"] == "holds"  # the authored text is kept


def test_the_entry_names_what_it_needs_and_touches() -> None:
    offboard = entries()["offboard_employee"]
    assert offboard["needs"] == [
        {"action": "write.transition", "type": "ppl:Employee"},
        {"action": "write.unlink", "type": "ppl:Employee"},
    ]
    assert [t["curie"] for t in offboard["touches"]["relations"]] == [
        "leg:employed_by",
        "org:holds",
    ]
    hand_over = entries()["hand_over_position"]
    assert {"curie": "org:Position", "module": "organization"} in hand_over["touches"]["types"]
    create = entries()["create_task"]
    assert create["needs"] == [{"action": "write.create", "type": "ops:Task"}]


def test_a_create_in_a_template_needs_write_create_and_a_local_can_be_written_to() -> None:
    sources = probe(
        "  - {op: create, type: org:Role, local: role, attrs: {name: Reviewer}}\n"
        "  - {op: link, id: $position, rel: org:carries, target: '@role'}\n"
        "  - {op: update, id: '@role', set: {name: $name}}\n",
        POSITION + param("name", "type: string"),
    )
    document, problems = compile_actions(sources, MODEL)
    assert problems == []
    (entry,) = document["actions"]
    assert {"action": "write.create", "type": "org:Role"} in entry["needs"]
    assert {"action": "write.update", "type": "org:Role"} in entry["needs"]


EXPECTED = {
    "abstract_type": "action.abstract_type",
    "builtin_module": "action.builtin_module",
    "invalid_condition": "action.invalid_condition",
    "invalid_default": "action.invalid_default",
    "invalid_op": "action.invalid_op",
    "invalid_value": "action.invalid_value",
    "invalid_yaml": "action.invalid_yaml",
    "lifecycle_direct_write": "action.lifecycle_direct_write",
    "lifecycle_missing": "action.lifecycle_missing",
    "literal_reference": "action.literal_reference",
    "local_duplicate": "action.local_duplicate",
    "local_unresolved": "action.local_unresolved",
    "missing_key": "action.missing_key",
    "module_required": "action.module_required",
    "name_mismatch": "action.name_mismatch",
    "not_numeric": "action.not_numeric",
    "op_impossible": "action.op_impossible",
    "optional_position": "action.optional_position",
    "parameter_undeclared": "action.parameter_undeclared",
    "parameter_unused": "action.parameter_unused",
    "placeholder_position": "action.placeholder_position",
    "precondition_on_invalid": "action.precondition_on_invalid",
    "precondition_unknown_attribute": "action.unknown_attribute",
    "precondition_unknown_relation": "action.unknown_relation",
    "replaced_by_invalid": "action.replaced_by_invalid",
    "template_invalid": "action.template_invalid",
    "type_mismatch": "action.type_mismatch",
    "unknown_attribute": "action.unknown_attribute",
    "unknown_class": "action.unknown_class",
    "unknown_enum": "action.unknown_enum",
    "unknown_key": "action.unknown_key",
    "unknown_module": "action.unknown_module",
    "unknown_qualifier": "action.unknown_qualifier",
    "unknown_relation": "action.unknown_relation",
    "unknown_state": "action.unknown_state",
    "unknown_type": "action.unknown_type",
    "unsupported_schema": "action.unsupported_schema",
}


def test_every_broken_fixture_has_an_expectation() -> None:
    assert sorted(p.stem for p in BROKEN_ACTIONS.glob("*.yaml")) == sorted(EXPECTED)


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_a_broken_action_gets_exactly_its_problem(case: str) -> None:
    sources = read_dir(BROKEN_ACTIONS)
    document, problems = compile_actions({f"{case}.yaml": sources[f"{case}.yaml"]}, MODEL)
    assert [p.code for p in problems] == [EXPECTED[case]]
    assert problems[0].kind == "action"
    assert problems[0].to_json()["action"] == problems[0].name
    assert document["actions"] == []


def test_all_problems_come_in_one_answer() -> None:
    broken = read_dir(BROKEN_ACTIONS)
    sources = {
        k: broken[k] for k in ("op_impossible.yaml", "unknown_module.yaml", "invalid_op.yaml")
    }
    document, problems = compile_actions({**sources, **valid_sources()}, MODEL)
    assert sorted(p.code for p in problems) == [
        "action.invalid_op",
        "action.op_impossible",
        "action.unknown_module",
    ]
    assert len(document["actions"]) == len(valid_sources())


def test_a_name_that_a_function_has_is_taken() -> None:
    sources = valid_sources()
    assert codes(sources) == []
    found = compile_actions(sources, MODEL, function_names=frozenset({"create_task", "other"}))
    assert [(p.code, p.name) for p in found[1]] == [("action.name_taken", "create_task")]
    assert "create_task" not in [e["name"] for e in found[0]["actions"]]


def test_op_impossible_names_the_ends_and_what_would_connect() -> None:
    sources = {"op_impossible.yaml": read_dir(BROKEN_ACTIONS)["op_impossible.yaml"]}
    (problem,) = compile_actions(sources, MODEL)[1]
    assert problem.details["relation"] == "org:holds"
    assert problem.details["source"] == "ppl:Person"
    assert problem.details["target"] == "org:OrgUnit"
    assert problem.details["connecting"] == {
        "direct": [],
        "paths": [
            [
                {"relation": "org:holds", "from": "ppl:Person", "to": "org:Position"},
                {"relation": "org:in_unit", "from": "org:Position", "to": "org:OrgUnit"},
            ]
        ],
    }


def test_a_name_error_keeps_the_term_details() -> None:
    sources = {"unknown_type.yaml": read_dir(BROKEN_ACTIONS)["unknown_type.yaml"]}
    (problem,) = compile_actions(sources, MODEL)[1]
    assert problem.details["term"] == "ppl:Persn"
    assert "ppl:Person" in problem.details["did_you_mean"]


def test_a_local_name_of_a_term_resolves_when_unique() -> None:
    sources = probe("  - {op: create, type: Role, attrs: {name: Reviewer}}\n")
    document, problems = compile_actions(sources, MODEL)
    assert problems == []
    assert document["actions"][0]["ops"][0]["type"] == "org:Role"


# Type fit of placeholders


def test_a_placeholder_must_fit_the_datatype() -> None:
    ok = probe(
        "  - {op: link, id: $person, rel: org:holds, target: $position, qualifiers: {since: $d}}\n",
        PERSON + POSITION + param("d", "type: date"),
    )
    assert codes(ok) == []
    wrong = probe(
        "  - {op: link, id: $person, rel: org:holds, target: $position, qualifiers: {since: $d}}\n",
        PERSON + POSITION + param("d", "type: integer"),
    )
    assert codes(wrong) == ["action.type_mismatch"]


def test_an_enum_attribute_takes_the_enum_parameter_of_the_same_enum() -> None:
    ops = "  - {op: update, id: $external, set: {kind: $kind}}\n"
    external = param("external", "type: ref; class: ppl:External")
    assert codes(probe(ops, external + param("kind", "type: enum; enum: ppl:ExternalKind"))) == []
    assert codes(probe(ops, external + param("kind", "type: string"))) == ["action.type_mismatch"]
    assert codes(probe(ops, external + param("kind", "type: enum; enum: ppl:IdentitySystem"))) == [
        "action.type_mismatch"
    ]


def test_a_list_parameter_fits_list_positions_only() -> None:
    external = param("role", "type: ref; class: org:Role")
    whole = probe(
        "  - {op: update, id: $role, add: {alt_labels: $labels}}\n",
        external + param("labels", "type: string; many: true"),
    )
    assert codes(whole) == []
    element = probe(
        "  - {op: update, id: $role, add: {alt_labels: [$label]}}\n",
        external + param("label", "type: string"),
    )
    assert codes(element) == []
    assert codes(
        probe(
            "  - {op: update, id: $role, add: {alt_labels: [$labels]}}\n",
            external + param("labels", "type: string; many: true"),
        )
    ) == ["action.type_mismatch"]
    assert codes(
        probe(
            "  - {op: update, id: $role, set: {alt_labels: $label}}\n",
            external + param("label", "type: string"),
        )
    ) == ["action.type_mismatch"]
    assert codes(
        probe(
            "  - {op: update, id: $role, add: {name: $label}}\n",
            external + param("label", "type: string"),
        )
    ) == ["action.type_mismatch"]


def test_a_ref_placeholder_fits_with_subclasses() -> None:
    # org:holds takes a ppl:Person at the source: Employee, a subclass of Person, fits too
    sources = probe(
        "  - {op: link, id: $employee, rel: org:holds, target: $position}\n",
        param("employee", "type: ref; class: ppl:Employee") + POSITION,
    )
    assert codes(sources) == []
    wrong = probe(
        "  - {op: link, id: $position, rel: org:holds, target: $person}\n", PERSON + POSITION
    )
    assert codes(wrong) == ["action.op_impossible"]


def test_a_ref_position_takes_a_ref_parameter() -> None:
    sources = probe(
        "  - {op: link, id: $person, rel: org:holds, target: $position}\n",
        PERSON + param("position", "type: string"),
    )
    assert codes(sources) == ["action.type_mismatch"]


def test_a_value_position_refuses_a_ref_parameter() -> None:
    sources = probe(
        "  - {op: update, id: $position, set: {title: $other}}\n",
        POSITION + param("other", "type: ref; class: org:Position"),
    )
    assert codes(sources) == ["action.type_mismatch"]


def test_a_transition_checks_the_lifecycle_of_the_class() -> None:
    ok = probe(
        "  - {op: transition, id: $position, to: abolished}\n"
        "  - {op: transition, id: $position, to: active, attr: status}\n",
        POSITION,
    )
    assert codes(ok) == []
    assert codes(probe("  - {op: transition, id: $position, to: left}\n", POSITION)) == [
        "action.unknown_state"
    ]
    assert codes(
        probe("  - {op: transition, id: $position, to: active, attr: title}\n", POSITION)
    ) == ["action.lifecycle_missing"]
    assert codes(
        probe("  - {op: transition, id: $position, to: active, attr: nope}\n", POSITION)
    ) == ["action.unknown_attribute"]


def test_a_lifecycle_attribute_is_set_by_a_create_but_not_by_an_update() -> None:
    created = probe("  - {op: create, type: org:Position, attrs: {title: Lead, status: planned}}\n")
    assert codes(created) == []
    assert codes(probe("  - {op: update, id: $position, unset: [status]}\n", POSITION)) == [
        "action.lifecycle_direct_write"
    ]


def test_a_dynamic_unlink_needs_an_entity_that_can_have_the_edge() -> None:
    ok_in = probe(
        "  - {op: unlink, id: $position, rel: holds, direction: in, all: true}\n", POSITION
    )
    assert codes(ok_in) == []
    wrong = probe(
        "  - {op: unlink, id: $position, rel: holds, direction: out, all: true}\n", POSITION
    )
    assert codes(wrong) == ["action.op_impossible"]
    local = probe(
        "  - {op: create, type: org:Role, local: r, attrs: {name: X}}\n"
        "  - {op: unlink, id: '@r', rel: org:carries, direction: in, all: true}\n"
    )
    assert codes(local) == ["action.invalid_op"]


def test_the_dynamic_unlink_keeps_its_default_direction_explicit() -> None:
    sources = probe("  - {op: unlink, id: $person, rel: holds, all: true}\n", PERSON)
    document, problems = compile_actions(sources, MODEL)
    assert problems == []
    assert document["actions"][0]["ops"][0] == {
        "op": "unlink",
        "id": "$person",
        "rel": "org:holds",
        "all": True,
        "direction": "out",
    }


# The template assembles


def test_an_optional_parameter_that_leaves_an_op_empty_is_a_template_defect() -> None:
    sources = probe(
        "  - {op: update, id: $position, set: {title: $title}}\n",
        POSITION + param("title", "type: string; required: false"),
    )
    (problem,) = compile_actions(sources, MODEL)[1]
    assert problem.code == "action.template_invalid"
    assert "optional parameter absent" in problem.message
    assert problem.path == "ops[0]"


def test_an_optional_parameter_drops_its_entry_in_a_valid_template() -> None:
    sources = probe(
        "  - {op: update, id: $position, set: {title: $title, alt_labels: [x]}}\n",
        POSITION + param("title", "type: string; required: false"),
    )
    assert codes(sources) == []


def test_an_optional_parameter_with_a_default_is_not_optional() -> None:
    sources = probe(
        "  - {op: update, id: $position, set: {title: $title}}\n",
        POSITION + param("title", "type: string; default: Lead"),
    )
    assert codes(sources) == []


def test_a_time_placeholder_takes_a_date() -> None:
    ops = "  - {op: create, type: org:Role, attrs: {name: R}, valid_from: $from}\n"
    assert codes(probe(ops, param("from", "type: date"))) == []
    assert codes(probe(ops, param("from", "type: string"))) == ["action.type_mismatch"]


def test_a_dollar_text_is_a_placeholder_of_an_undeclared_parameter() -> None:
    sources = probe("  - {op: update, id: $position, set: {title: '$5'}}\n", POSITION)
    assert codes(sources) == ["action.parameter_undeclared"]


def test_every_parameter_is_declared_and_used() -> None:
    assert codes(
        probe("  - {op: link, id: $person, rel: org:holds, target: $other}\n", PERSON)
    ) == ["action.parameter_undeclared"]
    assert codes(
        probe("  - {op: unlink, id: $person, rel: org:holds, all: true}\n", PERSON + POSITION)
    ) == ["action.parameter_unused"]


def test_a_default_is_checked_by_binding_and_a_ref_has_none() -> None:
    ops = "  - {op: update, id: $position, set: {title: $title}}\n"
    assert codes(probe(ops, POSITION + param("title", "type: integer; default: 3"))) == [
        "action.type_mismatch"
    ]
    assert "action.invalid_default" in codes(
        probe(ops, POSITION + param("title", "type: integer; default: x"))
    )
    ref_default = param("position", "type: ref; class: org:Position; default: POS-0001")
    assert codes(probe("  - {op: retract, id: $position}\n", ref_default)) == [
        "action.invalid_default"
    ]


# Preconditions


def test_a_precondition_is_compiled_against_the_class_of_its_parameter() -> None:
    expr = entries()["assign_position"]["preconditions"][1]["expr"]
    assert expr == {
        "eq": [
            {
                "count": {
                    "rel": "org:holds",
                    "direction": "in",
                    "where": {"record_status": ["active"]},
                }
            },
            {"const": 0},
        ]
    }


def precondition(on: str, condition: str, ident: str = "check") -> str:
    return (
        f"  - id: {ident}\n    on: {on}\n    summary: A check.\n    require:\n      - {condition}\n"
    )


def pre_on_position(condition: str) -> dict[str, str]:
    pre = "preconditions:\n" + precondition("position", condition)
    return probe("  - {op: retract, id: $position}\n", POSITION, pre=pre)


def test_precondition_conditions_mirror_the_rule_checks() -> None:
    assert codes(pre_on_position("{attr: status, equals_string: active}")) == []
    assert codes(pre_on_position("{attr: nope, equals_string: active}")) == [
        "action.unknown_attribute"
    ]
    assert codes(pre_on_position("{count: {rel: holdz}, equals_number: 0}")) == [
        "action.unknown_relation"
    ]
    assert codes(
        pre_on_position("{count: {rel: holds, where: {type: Persn}}, equals_number: 0}")
    ) == ["action.unknown_type"]
    assert codes(pre_on_position("{attr: title, minimum_value: {attr: title}}")) == [
        "action.not_numeric",
        "action.not_numeric",
    ]
    assert codes(
        pre_on_position("{sum: {rel: holds, direction: in, attr: name}, minimum_value: 1}")
    ) == ["action.not_numeric"]
    assert codes(
        pre_on_position("{sum: {rel: holds, direction: in, attr: nope}, minimum_value: 1}")
    ) == ["action.unknown_attribute"]


def test_a_precondition_on_a_list_or_an_unknown_parameter_is_refused() -> None:
    present = "{attr: title, value_presence: PRESENT}"
    many = param("positions", "type: ref; class: org:Position; many: true")
    sources = probe(
        "  - {op: supersede, by: $position, targets: $positions}\n",
        POSITION + many,
        pre="preconditions:\n" + precondition("positions", present),
    )
    assert codes(sources) == ["action.precondition_on_invalid"]
    sources = probe(
        "  - {op: retract, id: $position}\n",
        POSITION,
        pre="preconditions:\n" + precondition("ghost", present),
    )
    assert codes(sources) == ["action.precondition_on_invalid"]


# Limits


def many_ops(count: int) -> dict[str, str]:
    return probe("  - {op: retract, id: $position}\n" * count, POSITION)


def test_the_limits() -> None:
    assert codes(many_ops(MAX_OPS)) == []
    assert codes(many_ops(MAX_OPS + 1)) == ["action.too_many_ops"]
    present = "{attr: title, value_presence: PRESENT}"
    pre = "".join(precondition("position", present, f"c{i}") for i in range(MAX_PRECONDITIONS + 1))
    sources = probe("  - {op: retract, id: $position}\n", POSITION, pre="preconditions:\n" + pre)
    assert codes(sources) == ["action.too_many_preconditions"]
    files = {f"a{i:04d}.yaml": "" for i in range(MAX_ACTIONS + 1)}
    found = [p.code for p in compile_actions(files, MODEL)[1]]
    assert found.count("action.too_many_actions") == 1


def test_a_replacement_must_be_an_active_action() -> None:
    sources = probe(
        "  - {op: retract, id: $position}\n",
        POSITION,
        extra="status: deprecated\nreplaced_by: other\n",
    )
    other = probe("  - {op: retract, id: $position}\n", POSITION)["probe.yaml"].replace(
        "name: probe", "name: other"
    )
    assert codes({**sources, "other.yaml": other}) == []
    assert codes(sources) == ["action.replaced_by_invalid"]


# The release invariant


def test_the_published_catalog_still_holds_against_its_own_model() -> None:
    assert check_actions(compiled()[0], MODEL) == []


def test_a_catalog_is_stale_against_a_model_without_the_relation() -> None:
    other = compiled_model("company", "1.1.0")  # the fixture ontology: no employment, no legal
    problems = check_actions(compiled()[0], other)
    assert problems  # at least the unknown terms
    assert "offboard_employee" in {p.name for p in problems}
    assert "action.unknown_class" in {p.code for p in problems}
    assert all(p.kind == "action" for p in problems)


def test_an_unknown_relation_in_the_stored_form_is_stale() -> None:
    document, _ = compiled()
    broken = {**document, "actions": [dict(e) for e in document["actions"]]}
    for entry in broken["actions"]:
        if entry["name"] == "assign_position":
            entry["ops"] = [{**entry["ops"][0], "rel": "org:removed"}]
    found = check_actions(broken, MODEL)
    assert [(p.code, p.name) for p in found] == [("action.stale", "assign_position")]
    assert "ops" in (found[0].path or "")


def test_a_stored_condition_that_reads_differently_is_stale() -> None:
    document, _ = compiled()
    broken = {**document, "actions": [dict(e) for e in document["actions"]]}
    for entry in broken["actions"]:
        if entry["name"] == "assign_position":
            entry["preconditions"] = []
    found = check_actions(broken, MODEL)
    assert [(p.code, p.name) for p in found] == [("action.stale", "assign_position")]


def test_check_actions_refuses_another_schema_or_shape() -> None:
    with pytest.raises(GraphCliError) as caught:
        check_actions({"schema": "graph_cli.actions/2", "actions": []}, MODEL)
    assert caught.value.code == "format.unsupported_version"
    with pytest.raises(JsonShapeError):
        check_actions({"schema": CATALOG_SCHEMA}, MODEL)
