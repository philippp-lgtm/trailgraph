"""Actions and functions that take an entity, on the example company model (T5)."""

from collections.abc import Sequence
from dataclasses import replace

from support.actions import company_model, valid_sources
from support.functions import valid_sources as function_sources
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.records import EdgeRecord, RecordStatus, Revision
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.kinetic.affordances import ActionAffordance, actions_for, functions_for
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.params import ParamDef
from graph_cli.ontology_layer.affordances import MAX_ENTRIES

MODEL = company_model()
ENTRIES = {e.name: e for e in load_action_entries(compile_actions(valid_sources(), MODEL)[0])}
FUNCTIONS = {e.name: e for e in load_entries(compile_catalog(function_sources(), MODEL)[0])}


def position(
    name: str,
    state: str = "active",
    edges: Sequence[EdgeRecord] = (),
    record: RecordStatus = "active",
) -> Revision:
    attrs = {"title": name.title(), "status": state}
    return revision(name, "org:Position", attrs, edges, status=record)


def employee(name: str, state: str = "active", *positions: str) -> Revision:
    edges = [edge("org:holds", p, name=f"{name}>{p}") for p in positions]
    return revision(name, "ppl:Employee", {"name": name, "status": state}, edges)


def affordances(
    entity: Revision, *others: Revision, names: Sequence[str] | None = None
) -> dict[tuple[str, str], ActionAffordance]:
    chosen = [ENTRIES[n] for n in names] if names else list(ENTRIES.values())
    found = actions_for(entity, chosen, DictSnapshot(entity, *others), MODEL)
    return {(a.action, a.parameter): a for a in found.entries}


def test_an_action_matches_through_a_parameter_of_a_superclass() -> None:
    found = affordances(employee("mara"))  # Employee is a ppl:Person
    assert set(found) == {
        ("assign_position", "person"),
        ("hand_over_position", "person"),
        ("offboard_employee", "employee"),
        ("create_task", "assignee"),
    }


def test_a_class_that_no_parameter_covers_matches_nothing() -> None:
    entity = revision("hq", "org:OrgUnit", {"name": "HQ"})
    assert affordances(entity) == {}


def test_preconditions_on_other_parameters_are_named_unchecked() -> None:
    found = affordances(employee("mara"))
    assign = found[("assign_position", "person")]
    assert assign.applies is True and assign.blocked_by == ()
    assert assign.unchecked == ("position_open", "position_vacant")
    assert found[("hand_over_position", "person")].unchecked == ("position_held",)
    assert found[("create_task", "assignee")].unchecked == ()


def test_a_precondition_on_the_matching_parameter_is_judged() -> None:
    found = affordances(employee("mara", "onboarding"))
    assert found[("offboard_employee", "employee")].applies is True
    left = affordances(employee("jon", "left"))[("offboard_employee", "employee")]
    assert left.applies is False
    assert [(b.precondition, b.summary) for b in left.blocked_by] == [
        ("not_left_yet", "The employee has not left already.")
    ]
    assert left.to_json() == {
        "action": "offboard_employee",
        "as": "employee",
        "applies": False,
        "blocked_by": [{"precondition": "not_left_yet", "summary": left.blocked_by[0].summary}],
        "unchecked": [],
    }


def test_every_failing_precondition_of_the_parameter_is_listed() -> None:
    lead = position("lead", "abolished")
    found = affordances(lead, employee("mara", "active", "lead"))
    assign = found[("assign_position", "position")]
    assert assign.applies is False
    assert [b.precondition for b in assign.blocked_by] == ["position_open", "position_vacant"]
    assert assign.unchecked == ()
    assert found[("hand_over_position", "position")].applies is True  # held


def test_a_vacant_open_position_takes_assign_but_not_hand_over() -> None:
    found = affordances(position("lead"))
    assert found[("assign_position", "position")].applies is True
    handover = found[("hand_over_position", "position")]
    assert [b.precondition for b in handover.blocked_by] == ["position_held"]


def test_two_matching_parameters_give_two_affordances() -> None:
    entry = ENTRIES["assign_position"]
    params = tuple(
        replace(p, of_class="ppl:Person") if p.name == "position" else p for p in entry.defn.params
    )
    twin = replace(entry, defn=replace(entry.defn, params=params))
    entity = employee("mara")
    found = actions_for(entity, [twin], DictSnapshot(entity), MODEL).entries
    assert [(a.action, a.parameter) for a in found] == [
        ("assign_position", "person"),
        ("assign_position", "position"),
    ]
    assert found[0].unchecked == ("position_open", "position_vacant")
    assert found[1].unchecked == ()  # judged on the entity: it is open and not held


def test_a_parameter_that_is_not_a_reference_never_matches() -> None:
    entry = ENTRIES["create_task"]
    params = (ParamDef("who", "string", "A name.", of_class="ppl:Person"),)
    plain = replace(entry, defn=replace(entry.defn, params=params))
    entity = employee("mara")
    assert actions_for(entity, [plain], DictSnapshot(entity), MODEL).entries == ()


def test_the_given_entries_are_all_that_is_listed() -> None:
    found = affordances(employee("mara"), names=["offboard_employee"])
    assert set(found) == {("offboard_employee", "employee")}


def test_an_inactive_entity_has_no_actions_and_no_functions() -> None:
    for record in ("superseded", "retracted"):
        entity = revision("hq", "org:OrgUnit", {"name": "HQ"}, status=record)  # type: ignore[arg-type]
        assert not functions_for(entity, list(FUNCTIONS.values()), MODEL).entries
        gone = employee("mara")
        gone = replace(gone, record_status=record)  # type: ignore[arg-type]
        assert actions_for(gone, list(ENTRIES.values()), DictSnapshot(gone), MODEL).entries == ()


def test_actions_are_capped_and_count_the_rest() -> None:
    entry: ActionEntry = ENTRIES["offboard_employee"]
    many = [
        replace(entry, defn=replace(entry.defn, name=f"offboard_{n:03d}"))
        for n in range(MAX_ENTRIES + 20)
    ]
    entity = employee("mara")
    found = actions_for(entity, many, DictSnapshot(entity), MODEL)
    assert len(found.entries) == MAX_ENTRIES and found.more == 20
    assert found.entries[0].action == "offboard_000"
    assert found.to_json()["actions_more"] == 20
    one = actions_for(entity, many[:1], DictSnapshot(entity), MODEL).to_json()
    assert "actions_more" not in one


def test_functions_match_through_the_class_of_a_parameter_with_subtypes() -> None:
    team = revision("ops", "org:Team", {"name": "Ops"})  # a Team is an org:OrgUnit
    found = functions_for(team, list(FUNCTIONS.values()), MODEL)
    assert {f.function for f in found.entries} == {
        n for n, f in FUNCTIONS.items() if any(p.of_class == "org:OrgUnit" for p in f.defn.params)
    }
    assert found.entries and all(f.parameter == "unit" for f in found.entries)
    assert found.to_json()["functions"][0] == {  # type: ignore[index]
        "function": found.entries[0].function,
        "as": "unit",
    }
    assert functions_for(position("lead"), list(FUNCTIONS.values()), MODEL).entries == ()


def test_functions_are_capped_and_count_the_rest() -> None:
    entry: FunctionEntry = FUNCTIONS["unit_members"]
    many = [
        replace(entry, defn=replace(entry.defn, name=f"members_{n:03d}"))
        for n in range(MAX_ENTRIES + 3)
    ]
    team = revision("ops", "org:Team", {"name": "Ops"})
    found = functions_for(team, many, MODEL)
    assert len(found.entries) == MAX_ENTRIES and found.more == 3
