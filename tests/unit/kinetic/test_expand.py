"""Preconditions and template expansion over an in-memory snapshot."""

from decimal import Decimal

import pytest
from support.actions import company_model, valid_sources
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.changeset import (
    CreateOp,
    LinkOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
    changeset_to_json,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import Revision
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.kinetic.expand import check_preconditions, expand

MODEL = company_model()
DOCUMENT, PROBLEMS = compile_actions(valid_sources(), MODEL)
ENTRIES = {e.name: e for e in load_action_entries(DOCUMENT)}


def entity(name: str) -> str:
    return str(eid(name))


def position(name: str, status: str = "active") -> Revision:
    return revision(name, "org:Position", {"title": name.title(), "status": status})


def holder(name: str, *positions: str) -> Revision:
    edges = [edge("org:holds", p, name=f"{name}>{p}") for p in positions]
    return revision(name, "ppl:Employee", {"name": name, "status": "active"}, edges)


def action(name: str) -> ActionEntry:
    return ENTRIES[name]


def refusal(call) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        call()
    return caught.value


# Preconditions


def test_the_valid_actions_compile_for_these_tests() -> None:
    assert PROBLEMS == [] and sorted(ENTRIES) == [
        "assign_position",
        "create_task",
        "hand_over_position",
        "offboard_employee",
    ]


def test_preconditions_that_hold_pass() -> None:
    snapshot = DictSnapshot(position("lead"), holder("mara"))
    bound = {"person": entity("mara"), "position": entity("lead"), "since": "2026-03-01"}
    check_preconditions(action("assign_position"), bound, snapshot, MODEL)
    check_preconditions(action("create_task"), {"title": "x"}, snapshot, MODEL)  # none declared


def test_every_failing_precondition_is_listed_with_the_operand_values() -> None:
    snapshot = DictSnapshot(
        position("lead", "abolished"), holder("mara", "lead"), holder("jon", "lead")
    )
    bound = {"person": entity("mara"), "position": entity("lead"), "since": "2026-03-01"}
    error = refusal(lambda: check_preconditions(action("assign_position"), bound, snapshot, MODEL))
    assert error.code == "action.precondition_failed"
    assert error.details["action"] == "assign_position"
    failed = error.details["failed"]
    assert [f["precondition"] for f in failed] == ["position_open", "position_vacant"]
    assert failed[0] == {
        "precondition": "position_open",
        "parameter": "position",
        "entity": entity("lead"),
        "summary": "The position is planned or active, not abolished.",
        "actual": None,
    }
    assert failed[1]["actual"] == [
        {
            "operand": {
                "count": {
                    "rel": "org:holds",
                    "direction": "in",
                    "where": {"record_status": ["active"]},
                }
            },
            "value": 2,
        },
        {"operand": {"const": 0}, "value": 0},
    ]
    assert "position_open" in error.message and "position_vacant" in error.message


def test_only_the_failing_preconditions_are_reported() -> None:
    snapshot = DictSnapshot(position("lead", "planned"), holder("mara", "lead"))
    bound = {"person": entity("jon"), "position": entity("lead"), "since": "2026-03-01"}
    error = refusal(lambda: check_preconditions(action("assign_position"), bound, snapshot, MODEL))
    assert [f["precondition"] for f in error.details["failed"]] == ["position_vacant"]
    assert error.details["failed"][0]["actual"][0]["value"] == 1


def test_a_missing_entity_is_a_caller_bug() -> None:
    bound = {"person": entity("mara"), "position": entity("lead"), "since": "2026-03-01"}
    error = refusal(
        lambda: check_preconditions(action("assign_position"), bound, DictSnapshot(), MODEL)
    )
    assert error.code == "internal.error"
    bound["position"] = "POS-0001"  # not resolved to a full id
    error = refusal(
        lambda: check_preconditions(
            action("assign_position"), bound, DictSnapshot(position("lead")), MODEL
        )
    )
    assert error.code == "internal.error"


# Expansion


def test_a_static_template_substitutes_values() -> None:
    bound = {"person": entity("mara"), "position": entity("lead"), "since": "2026-03-01"}
    cs = expand(
        action("assign_position"), bound, DictSnapshot(), reason="why", idempotency_key="k1"
    )
    (op,) = cs.ops
    assert isinstance(op, LinkOp)
    assert (str(op.id), op.edge.rel, str(op.edge.target)) == (
        entity("mara"),
        "org:holds",
        entity("lead"),
    )
    assert dict(op.edge.qualifiers) == {"since": "2026-03-01"}
    assert (cs.reason, cs.idempotency_key) == ("why", "k1")


def test_all_true_in_the_in_direction_unlinks_every_active_holder_exactly() -> None:
    snapshot = DictSnapshot(
        position("lead"),
        holder("mara", "lead", "other"),
        holder("jon", "lead"),
        revision(
            "ghost",
            "ppl:Employee",
            {"name": "g"},
            [edge("org:holds", "lead", name="ghost>lead")],
            status="retracted",
        ),
        holder("kim", "other"),
    )
    bound = {"position": entity("lead"), "person": entity("pat"), "since": "2026-03-01"}
    cs = expand(action("hand_over_position"), bound, snapshot)
    *unlinks, link = cs.ops
    assert all(isinstance(o, UnlinkOp) and o.rel is None and o.target is None for o in unlinks)
    assert [(str(o.id), o.edge_id) for o in unlinks if isinstance(o, UnlinkOp)] == sorted(
        [
            (entity("mara"), edge("org:holds", "lead", name="mara>lead").edge_id),
            (entity("jon"), edge("org:holds", "lead", name="jon>lead").edge_id),
        ],
        key=lambda pair: (str(pair[0]), str(pair[1])),
    )
    assert isinstance(link, LinkOp) and str(link.id) == entity("pat")


def test_all_true_with_one_edge_and_with_none() -> None:
    bound = {"position": entity("lead"), "person": entity("pat"), "since": "2026-03-01"}
    one = expand(
        action("hand_over_position"), bound, DictSnapshot(position("lead"), holder("mara", "lead"))
    )
    assert [type(o) for o in one.ops] == [UnlinkOp, LinkOp]
    none = expand(action("hand_over_position"), bound, DictSnapshot(position("lead")))
    assert [type(o) for o in none.ops] == [LinkOp]  # zero edges emit nothing


def test_all_true_in_the_out_direction_reads_the_entity_own_edges() -> None:
    employed = edge("leg:employed_by", "acme", name="mara>acme")
    mara = revision(
        "mara",
        "ppl:Employee",
        {"name": "Mara", "status": "active"},
        [
            edge("org:holds", "lead", name="mara>lead"),
            edge("org:holds", "deputy", name="mara>deputy"),
            employed,
        ],
    )
    snapshot = DictSnapshot(mara, position("lead"), position("deputy"))
    cs = expand(action("offboard_employee"), {"employee": entity("mara")}, snapshot, reason="left")
    kinds = [type(o) for o in cs.ops]
    assert kinds == [UnlinkOp, UnlinkOp, UnlinkOp, TransitionOp]
    unlinked = {o.edge_id for o in cs.ops if isinstance(o, UnlinkOp)}
    assert unlinked == {
        edge("org:holds", "lead", name="mara>lead").edge_id,
        edge("org:holds", "deputy", name="mara>deputy").edge_id,
        employed.edge_id,
    }
    assert all(isinstance(o, UnlinkOp) and str(o.id) == entity("mara") for o in cs.ops[:3])
    transition = cs.ops[3]
    assert isinstance(transition, TransitionOp) and transition.to == "left"


def test_an_action_that_only_unlinks_and_finds_nothing_has_nothing_to_do() -> None:
    document, problems = compile_actions(
        {
            "clear.yaml": "schema: graph_cli.action/1\nname: clear\nmodule: organization\n"
            "summary: Clear holders.\nuse_when: Testing.\nparams:\n  position:\n    type: ref\n"
            "    class: org:Position\n    summary: A position.\nops:\n"
            "  - {op: unlink, id: $position, rel: org:holds, direction: in, all: true}\n"
            "examples:\n  - request: Clear it.\n"
        },
        MODEL,
    )
    assert problems == []
    (entry,) = load_action_entries(document)
    bound = {"position": entity("lead")}
    error = refusal(lambda: expand(entry, bound, DictSnapshot(position("lead"))))
    assert error.code == "action.nothing_to_do"
    held = expand(entry, bound, DictSnapshot(position("lead"), holder("mara", "lead")))
    assert len(held.ops) == 1


def test_an_optional_parameter_that_is_not_given_drops_its_entry() -> None:
    bound = {"position": entity("lead"), "person": entity("pat")}
    cs = expand(action("hand_over_position"), bound, DictSnapshot(position("lead")))
    (link,) = cs.ops
    assert isinstance(link, LinkOp) and dict(link.edge.qualifiers) == {}
    assert changeset_to_json(cs)["ops"] == [
        {"op": "link", "id": entity("pat"), "rel": "org:holds", "target": entity("lead")}
    ]
    given = expand(
        action("hand_over_position"),
        {**bound, "since": "2026-03-01"},
        DictSnapshot(position("lead")),
    )
    assert dict(given.ops[0].edge.qualifiers) == {"since": "2026-03-01"}


def test_a_create_template_drops_an_absent_attribute_and_keeps_the_rest() -> None:
    bound = {"title": "Prepare the checklist", "assignee": entity("mara")}
    cs = expand(action("create_task"), bound, DictSnapshot())
    (op,) = cs.ops
    assert isinstance(op, CreateOp)
    assert dict(op.attrs) == {"title": "Prepare the checklist"}
    assert op.local == "task" and str(op.edges[0].target) == entity("mara")
    due = expand(action("create_task"), {**bound, "due": "2026-04-01"}, DictSnapshot())
    assert dict(due.ops[0].attrs) == {"title": "Prepare the checklist", "due_on": "2026-04-01"}


def test_a_missing_required_value_is_a_caller_bug() -> None:
    error = refusal(
        lambda: expand(action("assign_position"), {"person": entity("a")}, DictSnapshot())
    )
    assert error.code == "internal.error"


def test_a_reason_is_demanded_when_the_action_says_so() -> None:
    snapshot = DictSnapshot(holder("mara"))
    bound = {"employee": entity("mara")}
    for missing in (None, "", "  "):
        error = refusal(
            lambda m=missing: expand(action("offboard_employee"), bound, snapshot, reason=m)
        )
        assert error.code == "action.reason_required"
    assert (
        expand(action("offboard_employee"), bound, snapshot, reason="Resigned").reason == "Resigned"
    )


def test_values_reach_the_document_in_their_json_and_text_forms() -> None:
    source = (
        "schema: graph_cli.action/1\nname: tune\nmodule: operations\nsummary: Tune a task.\n"
        "use_when: Testing.\nparams:\n  task:\n    type: ref\n    class: ops:Task\n"
        "    summary: A task.\n"
        "  title:\n    type: string\n    summary: A title.\n  day:\n    type: date\n"
        "    summary: A day.\n    required: false\n"
        "ops:\n  - {op: update, id: $task, set: {title: $title}, set_text: {due_on: $day}}\n"
        "examples:\n  - request: Tune it.\n"
    )
    document, problems = compile_actions({"tune.yaml": source}, MODEL)
    assert problems == []
    (entry,) = load_action_entries(document)
    cs = expand(entry, {"task": entity("t"), "title": "Hi", "day": "2026-05-06"}, DictSnapshot())
    (op,) = cs.ops
    assert isinstance(op, UpdateOp)
    assert (dict(op.set), dict(op.set_text)) == ({"title": "Hi"}, {"due_on": "2026-05-06"})
    assert Decimal  # decimals are converted by json_value; see test_params
