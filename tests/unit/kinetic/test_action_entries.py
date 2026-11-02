"""The entries of a compiled action catalog: typed reads, listing and detail shapes."""

import pytest
from support.actions import company_model, valid_sources

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import find_action, load_action_entries
from graph_cli.kinetic.entries import signature

DOCUMENT, PROBLEMS = compile_actions(valid_sources(), company_model())
ENTRIES = load_action_entries(DOCUMENT)
BY_NAME = {e.name: e for e in ENTRIES}


def test_the_catalog_reads_back_in_order() -> None:
    assert PROBLEMS == []
    assert [e.name for e in ENTRIES] == sorted(n.removesuffix(".yaml") for n in valid_sources())
    raw = {e["name"]: e for e in DOCUMENT["actions"]}
    for entry in ENTRIES:
        assert entry.hash == raw[entry.name]["hash"]
        assert entry.defn.to_json()["summary"] == raw[entry.name]["summary"]


def test_an_entry_has_ops_preconditions_touches_and_needs() -> None:
    entry = BY_NAME["hand_over_position"]
    assert entry.module == "organization"
    assert [o["op"] for o in entry.ops] == ["unlink", "link"]
    assert entry.ops[0]["rel"] == "org:holds" and entry.ops[0]["direction"] == "in"
    assert [(p.id, p.on) for p in entry.preconditions] == [("position_held", "position")]
    assert [t.curie for t in entry.relations] == ["org:holds"]
    assert [(n.action, n.type) for n in entry.needs] == [
        ("write.link", "ppl:Person"),
        ("write.unlink", "ppl:Person"),
    ]


def test_the_signature_marks_optional_parameters() -> None:
    entry = BY_NAME["hand_over_position"]
    assert entry.signature == "position: ref org:Position, person: ref ppl:Person, since?: date"
    assert entry.signature == signature(entry.defn.params)


def test_summary_and_detail_shapes() -> None:
    entry = BY_NAME["offboard_employee"]
    summary = entry.summary_json()
    assert list(summary) == ["name", "module", "summary", "signature", "status"]
    assert summary["signature"] == "employee: ref ppl:Employee"
    detail = entry.detail_json()
    assert detail["reason"] == "required"
    assert detail["hash"] == entry.hash
    assert [p["id"] for p in detail["preconditions"]] == ["not_left_yet"]
    assert detail["needs"] == [n.to_json() for n in entry.needs]
    assert detail["ops"][2] == {"op": "transition", "id": "$employee", "to": "left"}
    assert set(detail["touches"]) == {"types", "relations"}
    assert detail["examples"] == [e.to_json() for e in entry.defn.examples]


def test_find_action_suggests_close_names() -> None:
    assert find_action(ENTRIES, "create_task").name == "create_task"
    with pytest.raises(GraphCliError) as caught:
        find_action(ENTRIES, "assign_postion")
    assert caught.value.code == "action.not_found"
    assert caught.value.details["did_you_mean"][0] == "assign_position"
    with pytest.raises(GraphCliError) as empty:
        find_action(None, "x")
    assert empty.value.details["did_you_mean"] == []


def test_a_document_of_another_shape_is_refused() -> None:
    with pytest.raises(JsonShapeError):
        load_action_entries({"schema": "graph_cli.action_catalog/1"})
