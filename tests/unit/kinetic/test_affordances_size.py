"""F6: the affordances block of one entity stays small however much matches (T5)."""

import json
from dataclasses import replace

from support.actions import company_model, valid_sources
from support.functions import valid_sources as function_sources
from support.snapshots import DictSnapshot, revision
from support.views import compiled_entries

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.kinetic.affordances import VIEWS_MAX, actions_for, functions_for, views_for
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import load_entries
from graph_cli.ontology_layer.affordances import MAX_ENTRIES, relations, transitions

MODEL = company_model()
LIMIT = 16 * 1024
LONG = "a_long_name_of_a_synthetic_entry"  # 31 characters, plus the counter


def test_many_matching_actions_functions_and_relations_stay_below_16_kib() -> None:
    action = next(
        e
        for e in load_action_entries(compile_actions(valid_sources(), MODEL)[0])
        if e.name == "offboard_employee"
    )
    function = next(
        e
        for e in load_entries(compile_catalog(function_sources(), MODEL)[0])
        if e.name == "unit_members"
    )
    actions = [
        replace(action, defn=replace(action.defn, name=f"{LONG}_{n:03d}")) for n in range(60)
    ]
    functions = [
        replace(function, defn=replace(function.defn, name=f"{LONG}_{n:03d}")) for n in range(60)
    ]
    view = compiled_entries()["unit_overview"]
    views = [replace(view, defn=replace(view.defn, name=f"{LONG}_{n:03d}")) for n in range(60)]
    many = {f"org:{LONG}_{n:03d}": MODEL.relations["org:carries"] for n in range(60)}
    big = replace(MODEL, relations=many)
    employee = revision("mara", "ppl:Employee", {"name": "Mara", "status": "active"})
    team = revision("ops", "org:Team", {"name": "Ops"})
    seat = revision("seat", "org:Position", {"title": "Seat", "status": "active"})
    snapshot = DictSnapshot(employee, team, seat)

    found: dict[str, JsonValue] = {"record_status": "active"}
    found.update(transitions(MODEL, seat, snapshot).to_json())
    found.update(relations(big, seat, snapshot).to_json())
    found.update(actions_for(employee, actions, snapshot, MODEL).to_json())
    found.update(functions_for(team, functions, MODEL).to_json())
    found.update(views_for(team, views, MODEL).to_json())

    assert found["relations_more"] == 60 - MAX_ENTRIES
    assert found["actions_more"] == 60 - MAX_ENTRIES
    assert found["functions_more"] == 60 - MAX_ENTRIES
    assert len(found["views"]) == VIEWS_MAX and found["views_more"] == 60 - VIEWS_MAX  # type: ignore[arg-type]
    assert len(json.dumps(found).encode()) < LIMIT
