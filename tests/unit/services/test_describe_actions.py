"""describe: actions in the listings and in ``describe search`` (actions plan A10)."""

import json
from typing import Any

import pytest
from support.actions import company_model, edited, valid_sources
from support.fixtures import GOLDENS, assert_golden
from support.skills import graph_entry

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.services.describe import (
    describe_action,
    describe_actions,
    describe_module,
    describe_modules,
)
from graph_cli.services.schema_search import SEARCH_TERM_KINDS, search_model

MODEL = company_model()
ENTRY = graph_entry(MODEL)


def entries(sources: dict[str, str]) -> tuple[ActionEntry, ...]:
    document, problems = compile_actions(sources, MODEL)
    assert [p.to_json() for p in problems] == []
    return load_action_entries(document)


ACTIONS = entries(valid_sources())
# one deprecated action: create_task is replaced by assign_position, for the status tests only
DEPRECATED = entries(
    edited(
        "create_task.yaml",
        "ops:\n",
        "status: deprecated\nreplaced_by: assign_position\nops:\n",
    )
)


def names(body: dict[str, Any]) -> list[str]:
    return [a["name"] for a in body["actions"]]


def search(text: str, **options: Any) -> dict[str, Any]:
    options.setdefault("actions", ACTIONS)
    body, _ = search_model(ENTRY, MODEL, text, **options)
    return body


def hits(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(i["curie"], i["match"]["field"], i["match"]["how"]) for i in body["items"]]


def test_listing_hides_deprecated_and_filters_by_module() -> None:
    body = describe_actions(ENTRY, MODEL, ACTIONS)
    assert names(body) == [
        "assign_position",
        "create_task",
        "hand_over_position",
        "offboard_employee",
    ]
    assert "module" not in body and body["schema"] == "graph_cli.model/1"
    assert names(describe_actions(ENTRY, MODEL, DEPRECATED)) == [
        "assign_position",
        "hand_over_position",
        "offboard_employee",
    ]
    every = describe_actions(ENTRY, MODEL, DEPRECATED, include_deprecated=True)
    assert "create_task" in names(every)
    one = describe_actions(ENTRY, MODEL, DEPRECATED, module="ops", include_deprecated=True)
    assert names(one) == ["create_task"] and one["module"] == "operations"
    assert one["actions"][0]["status"] == "deprecated"
    org = describe_actions(ENTRY, MODEL, ACTIONS, module="org")
    assert org["actions"][0] == {
        "name": "assign_position",
        "module": "organization",
        "summary": "Record that a person takes over a position nobody holds, from a date.",
        "signature": "person: ref ppl:Person, position: ref org:Position, since: date",
        "status": "active",
    }
    with pytest.raises(GraphCliError) as info:
        describe_actions(ENTRY, MODEL, ACTIONS, module="no_such")
    assert info.value.code == "term.unknown"


def test_no_actions_lists_nothing() -> None:
    assert describe_actions(ENTRY, MODEL, None)["actions"] == []


def test_an_action_is_described_completely() -> None:
    body = describe_action(ENTRY, MODEL, ACTIONS, "offboard_employee")
    assert body["schema"] == "graph_cli.model/1"
    a = body["action"]
    assert a["reason"] == "required" and a["status"] == "active" and a["replaced_by"] is None
    assert a["params"][0]["class"] == "ppl:Employee"
    assert [p["id"] for p in a["preconditions"]] == ["not_left_yet"]
    assert [o["op"] for o in a["ops"]] == ["unlink", "unlink", "transition"]
    assert a["needs"] == [
        {"action": "write.transition", "type": "ppl:Employee"},
        {"action": "write.unlink", "type": "ppl:Employee"},
    ]
    assert a["touches"]["types"] == [{"curie": "ppl:Employee", "module": "people"}]
    assert a["examples"][0]["request"].startswith("A colleague")
    assert a["hash"].startswith("sha256:") and "use_when" in a and "signature" in a
    deprecated = describe_action(ENTRY, MODEL, DEPRECATED, "create_task")["action"]
    assert (deprecated["status"], deprecated["replaced_by"]) == ("deprecated", "assign_position")


def test_an_unknown_action_names_the_close_ones_among_the_given() -> None:
    with pytest.raises(GraphCliError) as info:
        describe_action(ENTRY, MODEL, ACTIONS, "assign_positions")
    assert info.value.code == "action.not_found"
    assert info.value.details["did_you_mean"][0] == "assign_position"
    visible = tuple(a for a in ACTIONS if a.name != "assign_position")
    with pytest.raises(GraphCliError) as info:
        describe_action(ENTRY, MODEL, visible, "assign_positions")
    assert "assign_position" not in info.value.details["did_you_mean"]
    with pytest.raises(GraphCliError) as info:
        describe_action(ENTRY, MODEL, None, "assign_position")
    assert info.value.details["did_you_mean"] == []


def test_modules_and_module_carry_actions_only_when_given() -> None:
    plain = describe_modules(ENTRY, MODEL)
    assert all("actions" not in m for m in plain["modules"])
    assert "actions" not in describe_module(ENTRY, MODEL, "org")["module"]
    counted = {
        m["id"]: m["actions"] for m in describe_modules(ENTRY, MODEL, None, ACTIONS)["modules"]
    }
    assert counted == {
        "company": 0,
        "core": 0,
        "legal": 0,
        "operations": 1,
        "organization": 2,
        "people": 1,
    }
    assert all(
        "functions" not in m for m in describe_modules(ENTRY, MODEL, None, ACTIONS)["modules"]
    )
    deprecated = describe_modules(ENTRY, MODEL, None, DEPRECATED)["modules"]
    assert {m["id"]: m["actions"] for m in deprecated}["operations"] == 0
    section = describe_module(ENTRY, MODEL, "org", None, ACTIONS)["module"]["actions"]
    assert [s["name"] for s in section] == ["assign_position", "hand_over_position"]
    assert set(section[0]) == {"name", "summary", "signature"}
    assert describe_module(ENTRY, MODEL, "company", None, ACTIONS)["module"]["actions"] == []
    keys = list(describe_modules(ENTRY, MODEL, (), ACTIONS)["modules"][0])
    assert keys.index("actions") == keys.index("functions") + 1


def test_action_is_the_last_search_kind() -> None:
    assert SEARCH_TERM_KINDS[-2:] == ("function", "action")  # existing tie-breaks stay


def test_name_and_summary_tiers() -> None:
    assert hits(search("assign position", kinds=("action",)))[0] == (
        "assign_position",
        "name",
        "exact",
    )
    assert hits(search("hand over", kinds=("action",)))[0] == (
        "hand_over_position",
        "name",
        "prefix",
    )
    assert hits(search("over position", kinds=("action",)))[0] == (
        "hand_over_position",
        "name",
        "word",
    )
    assert hits(search("holder of a position", kinds=("action",))) == [
        ("hand_over_position", "summary", "phrase")
    ]


def test_use_when_examples_and_parameters_are_tier_five() -> None:
    assert hits(search("a job, a role", kinds=("action",))) == [
        ("assign_position", "use_when", "phrase")
    ]
    # an example request, and a parameter name
    assert hits(search("onboarding checklist", kinds=("action",))) == [
        ("create_task", "use_when", "phrase")
    ]
    assert ("create_task", "use_when", "phrase") in hits(search("assignee", kinds=("action",)))


def test_names_labels_and_summaries_of_touched_terms_match() -> None:
    # nothing in the actions' own text says "outlives"; the summary of org:Position does
    found = hits(search("outlives the holder", kinds=("action",)))
    assert found == [
        ("assign_position", "touches", "phrase"),
        ("hand_over_position", "touches", "phrase"),
    ]
    # the inverse label of a touched relation
    assert ("offboard_employee", "touches", "phrase") in hits(search("held by", kinds=("action",)))


def test_an_action_hit_has_the_shape_of_the_others() -> None:
    item = search("a job, a role", kinds=("action",))["items"][0]
    assert item == {
        "kind": "action",
        "curie": "assign_position",
        "of": None,
        "module": "organization",
        "summary": "Record that a person takes over a position nobody holds, from a date.",
        "deprecated": False,
        "match": {"field": "use_when", "how": "phrase"},
    }
    old = search("create task", kinds=("action",), actions=DEPRECATED)["items"][0]
    assert old["deprecated"] is True


def test_actions_follow_the_module_filter_and_come_after_terms_of_a_tier() -> None:
    org = search("position", kinds=("action",), module="organization")
    assert org["items"] and all(i["module"] == "organization" for i in org["items"])
    kinds = [i["kind"] for i in search("position")["items"]]
    assert kinds.index("type") < kinds.index("action")


def test_searching_without_actions_is_unchanged() -> None:
    without, total = search_model(ENTRY, MODEL, "position")
    assert all(i["kind"] != "action" for i in without["items"])
    assert search("position", actions=None)["items"] == without["items"]
    assert search("a job, a role", actions=None)["items"] == []
    assert total == len(without["items"])


def test_goldens() -> None:
    def golden(name: str, document: dict[str, Any]) -> None:
        data = json.loads(json.dumps(document))
        del data["graph"]["id"]
        assert_golden(
            GOLDENS / "describe" / f"{name}.json", (json.dumps(data, indent=2) + "\n").encode()
        )

    golden("actions_company_1.1.0", describe_actions(ENTRY, MODEL, ACTIONS))
    golden(
        "action_company_1.1.0_hand_over_position",
        describe_action(ENTRY, MODEL, ACTIONS, "hand_over_position"),
    )
    golden("modules_company_1.1.0_actions", describe_modules(ENTRY, MODEL, None, ACTIONS))
    golden(
        "module_company_organization_1.1.0_actions",
        describe_module(ENTRY, MODEL, "organization", None, ACTIONS),
    )
    golden("search_company_1.1.0_onboarding", search("onboarding checklist"))
