"""Shape of graph_cli.audit/1 events (contracts B5.2, B5.4-B5.6, B5.11, V.4)."""

import copy
from collections.abc import Callable

import pytest
from support.txbuilder import actor

from graph_cli.audit.schema import (
    event_problems,
    is_intent,
    is_write_intent,
    unknown_actions,
)
from graph_cli.kernel.canonical import JsonValue

GRAPH = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"
ENTITY = "0192f6d4-2a10-7b43-a5f6-0718293a4b5c"
H1, H2, H3 = ("sha256:" + c * 64 for c in "123")

Event = dict[str, JsonValue]


def intent() -> Event:
    """The contract's B5 example: a create intent in a graph stream."""
    return {
        "schema": "graph_cli.audit/1",
        "stream": f"graph:{GRAPH}",
        "seq": 57,
        "event_id": "0192f6d4-2a10-7b42-8d4e-6f708192a3b4",
        "ts": "2026-10-14T19:21:05.123Z",
        "actor": actor().to_json(),
        "graph": {
            "id": GRAPH,
            "name": "example_graph",
            "ontology": {"bundle": "inventory", "version": "1.1.0", "artifact_hash": H1},
        },
        "env": {"data_root": "<root>", "root_source": "default"},
        "actions": ["write.create"],
        "authz": {
            "policy_hash": H2,
            "decisions": [
                {
                    "action": "write.create",
                    "resource": f"graph:{GRAPH}/type:inv:Item",
                    "effect": "allow",
                    "rule": "builtin.allow_app",
                }
            ],
        },
        "target": {"entities": [ENTITY], "types": ["inv:Item"]},
        "result": {"status": "ok"},
        "change": {
            "tx_id": "0192f6d4-2a10-7b41-9c3d-5e6f70819a2b",
            "tx_seq": 42,
            "changeset_hash": H3,
            "entities": [{"id": ENTITY, "rev": 1, "after": H1}],
        },
        "reason": "initial stock",
        "request": {"command": "create", "args_digest": H2},
        "prev_hash": H1,
        "hash": H2,
    }


def denial() -> Event:
    event = intent()
    for key in ("graph", "change", "target", "reason", "env"):
        del event[key]
    event["stream"] = "installation"
    event["actions"] = ["maint.repair"]
    event["authz"] = {
        "policy_hash": H2,
        "decisions": [
            {
                "action": "maint.repair",
                "resource": f"graph:{GRAPH}",
                "effect": "deny",
                "rule": "ceiling.app_no_maint",
                "code": "access.ceiling",
            }
        ],
    }
    event["result"] = {"status": "denied", "code": "access.ceiling"}
    return event


def test_valid_events_have_no_problems() -> None:
    assert event_problems(intent()) == []
    assert event_problems(denial()) == []


def test_unknown_keys_are_allowed() -> None:
    event = intent()
    event["future_key"] = {"anything": 1}
    assert event_problems(event) == []


def test_drafts_are_checked_without_chain_fields() -> None:
    event = intent()
    for key in ("seq", "prev_hash", "hash"):
        del event[key]
    assert event_problems(event, sealed=False) == []
    assert event_problems(event) == [
        "missing key 'seq'",
        "missing key 'prev_hash'",
        "missing key 'hash'",
    ]


def _set(path: str, value: JsonValue) -> Callable[[Event], None]:
    def apply(event: Event) -> None:
        *parents, last = path.split(".")
        node: JsonValue = event
        for part in parents:
            if isinstance(node, list):
                node = node[int(part)]
            else:
                assert isinstance(node, dict)
                node = node[part]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            assert isinstance(node, dict)
            if value is DELETE:
                del node[last]
            else:
                node[last] = value

    return apply


DELETE: JsonValue = "<delete>"

BROKEN: dict[str, tuple[Callable[[], Event], Callable[[Event], None], str]] = {
    "missing_key": (intent, _set("request", DELETE), "missing key 'request'"),
    "schema": (intent, _set("schema", "graph_cli.audit/2"), "schema is not graph_cli.audit/1"),
    "stream": (intent, _set("stream", "graph:x"), "stream is neither"),
    "seq": (intent, _set("seq", 0), "seq is not a positive integer"),
    "seq_bool": (intent, _set("seq", True), "seq is not a positive integer"),
    "hash": (intent, _set("hash", "md5:00"), "hash is not a sha256 hash"),
    "event_id": (intent, _set("event_id", "0192F6D4-2A10-7B42-8D4E-6F708192A3B4"), "event_id"),
    "ts_offset": (intent, _set("ts", "2026-10-14T21:21:05.123+02:00"), "ts is not"),
    "ts_precision": (intent, _set("ts", "2026-10-14T19:21:05Z"), "ts is not"),
    "actor_shape": (intent, _set("actor.client", DELETE), "actor:"),
    "actor_assurance": (intent, _set("actor.principal.assurance", "asserted"), "not verified"),
    "actions_empty": (intent, _set("actions", []), "actions is not a non-empty list"),
    "actions_type": (intent, _set("actions", [1]), "actions holds a non-string"),
    "actions_order": (
        intent,
        _set("actions", ["write.update", "write.create"]),
        "actions are not sorted",
    ),
    "status": (intent, _set("result.status", "maybe"), "result.status 'maybe'"),
    "code_on_ok": (intent, _set("result.code", "x.y"), "result.code must be present"),
    "code_missing": (denial, _set("result.code", DELETE), "result.code must be present"),
    "findings_on_ok": (intent, _set("result.findings", []), "only belong to invalid"),
    "policy_hash": (intent, _set("authz.policy_hash", "x"), "authz.policy_hash"),
    "decision_shape": (intent, _set("authz.decisions.0.rule", DELETE), "authz.decisions are"),
    "decision_cover": (
        intent,
        _set("actions", ["write.create", "write.link"]),
        "do not cover write.link",
    ),
    "deny_on_ok": (intent, _set("authz.decisions", denial()["authz"]["decisions"]), "only denied"),  # type: ignore[index]
    "denial_code": (denial, _set("result.code", "access.denied"), "needs a deny decision"),
    "request": (intent, _set("request.args_digest", "abc"), "request is not"),
    "graph_missing": (intent, _set("graph", DELETE), "needs graph"),
    "graph_other": (intent, _set("graph.id", ENTITY), "graph.id differs"),
    "graph_shape": (intent, _set("graph.ontology", DELETE), "graph is not"),
    "denial_in_graph": (denial, _set("stream", f"graph:{GRAPH}"), "denials belong"),
    "write_in_installation": (intent, _set("stream", "installation"), "write events belong"),
    "change_missing": (intent, _set("change", DELETE), "a write intent needs change"),
    "change_shape": (intent, _set("change.tx_seq", 0), "change is not"),
    "change_before": (intent, _set("change.entities.0.rev", 2), "change is not"),
    "change_on_denial": (denial, _set("change", {}), "only ok events carry change"),
    "anchors_in_graph": (intent, _set("anchors", []), "anchors belong"),
    "anchors_shape": (
        denial,
        _set("anchors", [{"graph_id": GRAPH, "seq": 0, "hash": H1}]),
        "anchors are not",
    ),
    "target": (intent, _set("target.types", "inv:Item"), "target is not"),
    "reason": (intent, _set("reason", 1), "reason is not a string"),
    "env": (intent, _set("env.root_source", DELETE), "env is not"),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_each_shape_rule(name: str) -> None:
    make, mutate, expected = BROKEN[name]
    event = copy.deepcopy(make())
    mutate(event)
    problems = event_problems(event)
    assert any(expected in p for p in problems), problems


def test_intent_classification() -> None:
    assert is_write_intent(intent()) and is_intent(intent())
    assert not is_intent(denial())
    maint = denial()
    maint["result"] = {"status": "ok"}
    assert is_intent(maint) and not is_write_intent(maint)
    for action, expected in (("maint.audit_verify", False), ("ext.notes.state", True)):
        maint["actions"] = [action]
        assert is_intent(maint) is expected


def test_unknown_actions() -> None:
    event = intent()
    event["actions"] = ["ext.notes.state", "ext.notes.tag", "read.teleport", "write.create"]
    assert unknown_actions(event) == ["ext.notes.tag", "read.teleport"]
    assert unknown_actions(event, ["ext.notes.tag"]) == ["read.teleport"]
