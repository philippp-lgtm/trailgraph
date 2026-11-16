"""S: writes of a caller who does not hold a marking (field visibility plan F6, R6; leak catalogue
items 3 and 11), in process on ``support.marked_writes``.

The oracle: a write that names a hidden attribute answers exactly as the same write on the twin
graph, whose type does not declare the attribute, for the same caller (code, message, details;
dry runs and idempotent retries included). Three kinds of caller lack the marking: ``listed``
(a ``writes`` list that names hidden attributes), ``writer`` (``update: true``) and ``editor``
(level ``write``); ``holder`` holds it and gets the full answers. Integrity is judged on the raw
graph: an update keeps hidden values, and findings about them are concealed.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.graphs import pin_of, release_files
from support.marked_writes import (
    HIDDEN,
    LIMITED,
    MODULE,
    Pair,
    compiled,
    decisions_of,
    findings,
    markings_in,
    unknown,
)
from support.markings import Answer

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.changeset import ChangeSet
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.ontology_layer.data_validator import CONCEALED, CallerView, validate_write
from graph_cli.services.visibility import attribute_mask, filter_model
from graph_cli.services.write import ActionPlan, WriteRequest
from graph_cli.temporal_knowledge.normalize import normalize

PERSON = "ex:Person"


@pytest.fixture
def pair(tmp_path: Path) -> Pair:
    found = Pair(tmp_path)
    found.setup(
        {"op": "create", "type": "ex:Team", "local": "t1", "attrs": {"name": "Core"}},
        {
            "op": "create",
            "type": PERSON,
            "local": "p1",
            "attrs": {
                "name": "Ada",
                "phone": "+1",
                "email": "ada@private.example",
                "nicknames": ["Al"],
                "grade": "mid",
                "code": "C-1",
                "hours": 30,
            },
            "edges": [{"rel": "ex:member_of", "target": "@t1"}],
        },
        {"op": "create", "type": PERSON, "local": "p2", "attrs": {"name": "Bo"}},
        {
            "op": "create",
            "type": "ex:Tag",
            "local": "g1",
            "attrs": {"title": "Gamma", "private_name": "Zed"},
        },
    )
    return found


def update(ref: str, **parts: Any) -> dict[str, Any]:
    return {"op": "update", "id": ref, **parts}


def denied(answer: Answer, name: str, action: str = "write.update") -> None:
    assert not answer.ok and answer.body[0] == "access.denied", answer
    assert answer.body[2]["rule"] == "default.deny"
    assert answer.body[2]["action"] == action
    assert answer.body[2]["resource"].endswith(f"/type:{PERSON}/attr:{name}")


# ---- every verb, every caller: as for a name the type does not declare --------------------

# (op, the hidden name it writes, whether the ``listed`` caller's list names it for the verb)
GUESSES = [
    (update("@p1", set={"email": "eve@private.example"}), "email", True),
    (update("@p1", set={"email": "ada@private.example"}), "email", True),  # its current value
    (update("@p1", set_text={"hours": "31"}), "hours", True),
    (update("@p1", unset=["email"]), "email", True),  # present
    (update("@p2", unset=["email"]), "email", True),  # absent
    (update("@p1", add={"nicknames": ["Al"]}), "nicknames", True),  # present
    (update("@p1", add={"nicknames": ["Ally"]}), "nicknames", True),
    (update("@p1", remove={"nicknames": ["Al"]}), "nicknames", True),  # present
    (update("@p1", remove={"nicknames": ["Ally"]}), "nicknames", True),  # absent
    (update("@p1", set={"code": "C-1"}), "code", True),  # immutable, its current value
    (update("@p1", set={"code": "C-2"}), "code", True),  # immutable, changed
    (update("@p1", set={"phone": "+2", "grade": "high"}), "grade", True),  # with a visible one
    (
        {"op": "create", "type": PERSON, "local": "n", "attrs": {"name": "Cy", "email": "c@x"}},
        "email",
        True,
    ),
    ({"op": "transition", "id": "@p2", "attr": "code", "to": "done"}, "code", False),
]


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry_run"])
@pytest.mark.parametrize("caller", LIMITED)
def test_a_write_naming_a_hidden_attribute_answers_as_a_name_the_type_lacks(
    pair: Pair, caller: str, dry_run: bool
) -> None:
    before = {ref: pair.raw(ref) for ref in ("p1", "p2")}
    for op, name, listed in GUESSES:
        answer = pair.same(caller, op, dry_run=dry_run)
        if caller == "listed" and listed:
            action = "write.create" if op["op"] == "create" else "write.update"
            denied(answer, name, action)
            continue
        target: dict[str, Any] = {"local": "n"} if op["op"] == "create" else {}
        if op["op"] != "create":
            target = {"id": str(pair.ids[op["id"][1:]]), "local": op["id"][1:]}
        assert findings(answer) == [unknown(PERSON, name, **target)], (op, answer)
    # nothing changed: a guess of a hidden value never gets ``changed: false`` either
    assert {ref: pair.raw(ref) for ref in ("p1", "p2")} == before


@pytest.mark.parametrize("caller", LIMITED)
def test_hidden_and_undeclared_names_together_name_both(pair: Pair, caller: str) -> None:
    op = update("@p1", set={"email": "x@y", "favourite": "tea"}, unset=["hours"])
    answer = pair.same(caller, op)
    if caller == "listed":  # the first denial in the order of the decisions
        denied(answer, "email")
        return
    names = [f["target"]["path"] for f in findings(answer)]
    assert names == ["attrs.email", "attrs.favourite", "attrs.hours"]


def test_an_undeclared_name_is_refused_early_for_a_caller_with_an_attribute_view(
    pair: Pair,
) -> None:
    """The uniform path: for a caller with an attribute view a name the type does not declare is
    refused before normalize like a hidden one, so no other finding rides along (validation,
    the path of the holder, would add them)."""
    op = update("@p2", unset=["favourite"], set={"phone": "+9"})
    answer = pair.same("editor", op)
    assert findings(answer) == [unknown(PERSON, "favourite", id=str(pair.ids["p2"]), local="p2")]
    full, _ = pair.ask("holder", op)
    codes = sorted(f["code"] for f in findings(full))
    assert codes == ["data.rule.violated", "data.structural.unknown_attribute"]


def test_close_names_come_from_the_view(pair: Pair) -> None:
    # a name close to a hidden one gets no suggestion of it
    answer = pair.same("editor", update("@p1", set={"emial": "x"}))
    (found,) = findings(answer)
    assert "expected" not in found, found
    full, _ = pair.ask("holder", update("@p1", set={"emial": "x"}))
    assert findings(full)[0]["expected"] == ["email"]
    near, _ = pair.ask("holder", update("@p1", set={"phon": "x"}))
    seen = pair.same("editor", update("@p1", set={"phon": "x"}))
    assert findings(near) == findings(seen) and findings(seen)[0]["expected"] == ["phone"]


def test_the_holder_writes_hidden_attributes(pair: Pair) -> None:
    marked, _ = pair.services["holder"]
    marked.apply(pair.request(pair.marked, update("@p1", set={"email": "new@private.example"})))
    assert pair.raw("p1")["email"] == "new@private.example"
    # the usual path: the current value again is no change
    same = update("@p1", set={"email": "new@private.example"})
    unchanged = marked.apply(pair.request(pair.marked, same))
    assert unchanged.warnings[0].code == "entity.unchanged"


# ---- what a write keeps --------------------------------------------------------------------


@pytest.mark.parametrize("caller", LIMITED)
def test_an_update_keeps_the_hidden_attributes(pair: Pair, caller: str) -> None:
    before = pair.raw("p1")
    answer = pair.same(caller, update("@p1", set={"phone": "+2", "name": "Ada L."}))
    assert answer.ok, answer
    assert pair.raw("p1") == {**before, "phone": "+2", "name": "Ada L."}
    # the same values again: unchanged, as on the twin (the hidden values are compared too)
    again = pair.same(caller, update("@p1", set={"phone": "+2"}))
    assert again.ok and again.body["entities"][0]["changed"] is False


@pytest.mark.parametrize("caller", LIMITED)
def test_an_idempotent_retry_never_confirms_a_hidden_name(pair: Pair, caller: str) -> None:
    """The holder wrote with a key; the same request of a caller without the marking is the
    refusal of an unknown attribute, never the replay of the holder's result."""
    op = update("@p1", set={"phone": "+2", "email": "new@private.example"})
    pair.setup(op, idempotency_key="k-1")
    marked, _ = pair.services["holder"]
    replay = marked.apply(pair.request(pair.marked, op, idempotency_key="k-1"))
    assert replay.idempotent_replay
    answer = pair.same(caller, op, idempotency_key="k-1")
    assert not answer.ok and answer.body[0] in ("write.invalid", "access.denied"), answer
    other = pair.same(caller, update("@p1", set={"email": "x@y"}), idempotency_key="k-1")
    assert other.body[0] in ("write.invalid", "access.denied")  # not the key reuse conflict


def test_a_dry_run_batch_answers_alike(pair: Pair) -> None:
    documents = [
        [{"op": "create", "type": PERSON, "local": "n", "attrs": {"name": "Cy"}}],
        [update("@n", set={"phone": "+2"})],
        [update("@n", set={"hours": 3})],
    ]
    answers = []
    for env, service in zip((pair.marked, pair.twin), pair.services["editor"], strict=True):
        requests = [pair.request(env, *doc, dry_run=True) for doc in documents]
        answers.append(Answer.of(lambda s=service, r=requests: s.apply_batch(r)))  # type: ignore[misc]
    marked, twin = answers
    assert marked == twin
    done = pair.services["editor"][0].apply_batch(
        [pair.request(pair.marked, *doc, dry_run=True) for doc in documents]
    )
    assert done.failed_index == 2 and done.error is not None
    (found,) = done.error.details["findings"]  # type: ignore[misc]
    assert found == unknown(PERSON, "hours", local="n")  # no placeholder id


def test_do_with_an_op_on_a_hidden_attribute_answers_alike(pair: Pair) -> None:
    """An action that sets a hidden attribute is hidden by the compile check over the view
    (``action.not_found``); should one reach the write service anyway, its expansion is refused
    as a change set naming an unknown attribute."""
    source = """\
schema: graph_cli.action/1
name: set_email
summary: Set the e-mail.
use_when: Set the e-mail.
params:
  who: {type: ref, class: ex:Person, summary: The person.}
  value: {type: string, summary: The address.}
ops:
  - {op: update, id: $who, set: {email: $value}}
examples:
  - request: Set the e-mail.
"""
    document, problems = compile_actions({"set_email.yaml": source}, pair.marked.model)
    assert problems == []
    (entry,) = load_action_entries(document)
    plan = ActionPlan(entry, {"who": str(pair.ids["p1"]), "value": "x@y"})
    for dry_run in (False, True):
        answers = []
        for env, service in zip((pair.marked, pair.twin), pair.services["writer"], strict=True):
            request = WriteRequest(
                env.actor,
                env.name,
                ChangeSet(ops=(), reason="test"),
                dry_run,
                RequestMeta("do", hash_value({"action": "set_email"})),
                plan=plan,
            )
            answers.append(Answer.of(lambda s=service, r=request: s.apply(r)))  # type: ignore[misc]
        assert answers[0] == answers[1]
        # the plan request binds no locals: the entity is named by its id
        assert findings(answers[0]) == [unknown(PERSON, "email", id=str(pair.ids["p1"]))]


# ---- creates and defaults --------------------------------------------------------------------


@pytest.mark.parametrize("caller", ("writer", "editor"))
def test_a_required_hidden_attribute_with_a_default_is_filled_in(pair: Pair, caller: str) -> None:
    answer = pair.same(caller, {"op": "create", "type": "ex:Badge", "local": "b", "attrs": {}})
    assert answer.ok, answer
    assert pair.raw(answer.body["id"])["level"] == "low"


@pytest.mark.parametrize("caller", ("writer", "editor"))
def test_a_required_hidden_attribute_without_a_default_blocks_the_create(
    pair: Pair, caller: str
) -> None:
    op = {"op": "create", "type": "ex:Card", "local": "c", "attrs": {"number": 1}}
    marked, twin = pair.ask(caller, op)
    assert twin.ok  # an accepted residual: the twin does not have the attribute
    assert findings(marked) == [
        {
            "severity": "error",
            "code": "data.structural.missing_required",
            "target": {"type": "ex:Card", "path": "attrs", "local": "c"},
            "message": CONCEALED,
        }
    ]
    full, _ = pair.ask("holder", op)
    assert findings(full)[0]["target"]["path"] == "attrs.holder"


def test_a_unique_hidden_attribute_set_by_a_default_is_concealed(pair: Pair) -> None:
    first, _ = pair.ask("editor", {"op": "create", "type": "ex:Token", "local": "a", "attrs": {}})
    assert first.ok
    marked, twin = pair.ask(
        "editor", {"op": "create", "type": "ex:Token", "local": "b", "attrs": {"number": 2}}
    )
    assert twin.ok
    assert findings(marked) == [
        {
            "severity": "error",
            "code": "data.semantic.unique_violation",
            "target": {"type": "ex:Token", "path": "attrs", "local": "b"},
            "message": CONCEALED,
        }
    ]
    full, _ = pair.ask(
        "holder", {"op": "create", "type": "ex:Token", "local": "c", "attrs": {"number": 3}}
    )
    (found,) = findings(full)
    assert found["target"]["path"] == "attrs.serial" and "already used" in found["message"]


def test_a_deprecated_hidden_attribute_set_by_a_default_warns_concealed(pair: Pair) -> None:
    marked, twin = pair.ask(
        "editor", {"op": "create", "type": "ex:Ticket", "local": "k", "attrs": {"number": 1}}
    )
    assert marked.ok and twin.ok and twin.body["findings"] == []
    (found,) = marked.body["findings"]
    assert (found["severity"], found["code"], found["message"]) == (
        "warning",
        "data.structural.deprecated_term",
        CONCEALED,
    )
    assert found["target"]["path"] == "attrs" and "legacy" not in str(found)


# ---- findings about hidden values --------------------------------------------------------


def release(pair: Pair, source: str, version: str) -> None:
    """Pin the marked graph to another version of the module (no migration: old values stay)."""
    model = compiled(source.replace("version: 1.0.0", f"version: {version}"))
    env = pair.marked
    assert isinstance(env.catalog.files, dict)
    for name, data in release_files(model).items():
        env.catalog.files[(env.entry.id, model.bundle_version, name)] = data  # type: ignore[union-attr]
    env.set_entry(pin=pin_of(model))


def test_findings_on_hidden_values_kept_from_the_raw_state_are_concealed(pair: Pair) -> None:
    """After a release that drops ``mid`` from the grades and makes ``hours`` required, an update
    of a visible attribute meets the old hidden values: the findings keep their code and
    severity, their path is cut to ``attrs``."""
    v2 = MODULE.replace("      mid: {summary: Middle.}\n", "").replace(
        "hours: {summary: Weekly hours., type: integer,",
        "hours: {summary: Weekly hours., type: integer, required: true,",
    )
    release(pair, v2, "2.0.0")
    marked, _ = pair.services["editor"]
    holder, _ = pair.services["holder"]
    expected = {
        "p1": ("data.structural.enum_value_unknown", "attrs.grade"),
        "p2": ("data.structural.missing_required", "attrs.hours"),
    }
    for ref, (code, path) in expected.items():
        with pytest.raises(GraphCliError) as caught:
            marked.apply(pair.request(pair.marked, update(f"@{ref}", set={"phone": "+2"})))
        (found,) = caught.value.details["findings"]  # type: ignore[misc]
        assert found == {
            "severity": "error",
            "code": code,
            "target": {"id": str(pair.ids[ref]), "type": PERSON, "path": "attrs", "local": ref},
            "message": CONCEALED,
        }
        with pytest.raises(GraphCliError) as full:
            holder.apply(pair.request(pair.marked, update(f"@{ref}", set={"phone": "+2"})))
        (shown,) = full.value.details["findings"]  # type: ignore[misc]
        assert (shown["code"], shown["target"]["path"]) == (code, path)
        assert shown["message"] != CONCEALED


def test_a_rule_over_a_hidden_attribute_is_concealed(pair: Pair) -> None:
    """``contact`` reads the hidden e-mail: its refusal is concealed (an accepted residual: the
    twin has no such rule, so the same write passes there)."""
    marked, twin = pair.ask("editor", update("@p1", unset=["phone"]))
    assert twin.ok
    assert findings(marked) == [
        {
            "severity": "error",
            "code": "data.rule.violated",
            "target": {"id": str(pair.ids["p1"]), "type": PERSON, "local": "p1"},
            "message": CONCEALED,
        }
    ]
    full, _ = pair.ask("holder", update("@p1", unset=["phone"]))
    assert findings(full)[0]["rule_id"] == "ex.contact"


def test_an_aggregate_over_a_hidden_attribute_is_concealed_without_its_values(
    pair: Pair,
) -> None:
    pair.setup(update("@p2", set={"hours": 40, "phone": "+2"}))
    link = {"op": "link", "id": "@p2", "rel": "ex:member_of", "target": "@t1"}
    marked, twin = pair.ask("editor", link)
    assert twin.ok
    (found,) = findings(marked)
    assert found == {
        "severity": "error",
        "code": "data.rule.violated",
        "target": {"id": str(pair.ids["t1"]), "type": "ex:Team", "local": "t1"},
        "message": CONCEALED,
    }
    full, _ = pair.ask("holder", link)
    assert findings(full)[0]["actual"][0]["value"] == 70


def test_a_guard_over_a_hidden_attribute_is_concealed(pair: Pair) -> None:
    move = {"op": "transition", "id": "@p2", "to": "done"}
    marked, twin = pair.ask("editor", move)
    assert twin.ok
    assert findings(marked) == [
        {
            "severity": "error",
            "code": "data.lifecycle.guard_failed",
            "target": {
                "id": str(pair.ids["p2"]),
                "type": PERSON,
                "path": "attrs.stage",
                "local": "p2",
            },
            "message": CONCEALED,
        }
    ]


def test_a_rule_and_an_enum_the_view_keeps_answer_in_full(pair: Pair) -> None:
    answer = pair.same("editor", update("@p1", set={"phone": "+9"}))
    assert findings(answer)[0]["rule_id"] == "ex.switchboard"
    kind = pair.same("editor", update("@t1", set={"kind": "field"}))
    (found,) = findings(kind)
    assert (found["code"], found["expected"]) == (
        "data.structural.enum_value_unknown",
        ["line", "staff"],
    )


def test_a_possible_duplicate_compares_only_the_names_the_caller_sees(pair: Pair) -> None:
    hidden = {"op": "create", "type": "ex:Tag", "local": "h", "attrs": {"title": "Zed"}}
    answer = pair.same("editor", hidden)
    assert answer.ok and answer.body["findings"] == []
    full, _ = pair.ask("holder", {**hidden, "local": "h2"})
    (found,) = full.body["findings"]
    assert found["actual"][0]["attr"] == "private_name"
    shown = pair.same(
        "editor", {"op": "create", "type": "ex:Tag", "local": "v", "attrs": {"title": "Gamma"}}
    )
    (seen,) = shown.body["findings"]
    assert seen["code"] == "data.semantic.possible_duplicate"
    assert [a["attr"] for a in seen["actual"]] == ["title"]


def test_explain_operands_naming_a_hidden_attribute_are_dropped(pair: Pair) -> None:
    """Defence in depth: a rule the view keeps reads no hidden attribute (the view drops every
    rule that does). Should one be kept, its values are dropped when an operand reads one."""
    env = pair.marked
    model = env.model
    view = ReadView(None, attribute_mask(model, frozenset()))
    shown = replace(filter_model(model, None, view.attributes), rules=model.rules)
    pair.setup(update("@p2", set={"hours": 40, "phone": "+2"}))
    snapshot = env.snapshot()
    link = env.changeset(
        {
            "op": "link",
            "id": str(pair.ids["p2"]),
            "rel": "ex:member_of",
            "target": str(pair.ids["t1"]),
        }
    )
    working = normalize(
        link.ops,
        snapshot,
        model,
        refs={},
        new_id=env.ids.new_id,
        recorded_at=env.clock.now(),
        tx_id=env.ids.new_id(),  # type: ignore[arg-type]
        tx_seq=snapshot.seq + 1,
        pin_version="1.0.0",
        alias_counters={},
        alias_prefixes={},
    )
    found = validate_write(
        model,
        snapshot,
        working,
        touched=tuple(working.revisions),
        effects=working.effects,
        view=CallerView(shown, view, lambda _: True),
    )
    (rule,) = [f for f in found if f.rule_id == "ex.team_hours"]
    assert rule.actual is None and rule.message != CONCEALED


# ---- the audit -------------------------------------------------------------------------------


def test_a_denied_write_records_the_marking_decision(pair: Pair) -> None:
    """``listed``'s list names ``email``, but a list never makes a hidden name writable: the
    answer is the decision on a resource no rule names; the event records it, the real decision
    and the denied read of the marking."""
    marked_events, twin_events = (len(e.installation_events()) for e in (pair.marked, pair.twin))
    pair.same("listed", update("@p1", set={"email": "x@y"}))
    (event,) = pair.marked.installation_events()[marked_events:]
    (twin,) = pair.twin.installation_events()[twin_events:]
    assert event["result"]["status"] == twin["result"]["status"] == "denied"
    graph = str(pair.marked.entry.id)
    resource = f"graph:{graph}/type:{PERSON}/attr:email"
    decisions = decisions_of(event)
    assert ("write.update", resource, "allow", "grant.1.update") in decisions
    assert ("write.update", resource, "deny", "default.deny") in decisions
    assert markings_in([event]) == [
        ("read.get", f"marking:{graph}/ex:personal", "deny", "default.deny")
    ]
    assert markings_in([twin]) == []
    lines = pair.marked.audit.read_lines(INSTALLATION_STREAM)  # type: ignore[attr-defined]
    assert verify_stream(lines, stream=INSTALLATION_STREAM).findings == ()


@pytest.mark.parametrize("caller", ("writer", "editor"))
def test_an_invalid_answer_to_a_hidden_name_is_a_denied_event(pair: Pair, caller: str) -> None:
    marked_before = len(pair.marked.installation_events()), len(pair.marked.graph_events())
    twin_before = len(pair.twin.installation_events()), len(pair.twin.graph_events())
    answer = pair.same(caller, update("@p1", set={"email": "x@y"}))
    assert answer.body[0] == "write.invalid"
    installation = pair.marked.installation_events()[marked_before[0] :]
    assert [e["result"]["status"] for e in installation] == ["denied"]
    assert pair.marked.graph_events()[marked_before[1] :] == []
    graph = str(pair.marked.entry.id)
    assert markings_in(installation) == [
        ("read.get", f"marking:{graph}/ex:personal", "deny", "default.deny")
    ]
    # the twin: an ``invalid`` event of an unknown attribute, no marking decision
    (invalid,) = pair.twin.graph_events()[twin_before[1] :]
    assert invalid["result"]["status"] == "invalid" and markings_in([invalid]) == []
    assert pair.twin.installation_events()[twin_before[0] :] == []
    lines = pair.marked.audit.read_lines(INSTALLATION_STREAM)  # type: ignore[attr-defined]
    assert verify_stream(lines, stream=INSTALLATION_STREAM).findings == ()


def test_successful_and_invalid_events_carry_no_marking_decision(pair: Pair) -> None:
    before = len(pair.marked.graph_events()), len(pair.marked.installation_events())
    pair.same("writer", update("@p1", set={"phone": "+2"}))  # ok
    pair.same("writer", update("@p1", set={"favourite": "tea"}))  # undeclared only: invalid
    pair.same("writer", update("@p1", set={"phone": "+9"}))  # a visible rule: invalid
    events = pair.marked.graph_events()[before[0] :]
    assert [e["result"]["status"] for e in events] == ["ok", "invalid", "invalid"]
    assert markings_in(events) == []
    assert pair.marked.installation_events()[before[1] :] == []
    # the same events as on the twin, but for the pinned ontology
    twin = pair.twin.graph_events()[-3:]
    assert [decisions_of(e) for e in events] == [decisions_of(e) for e in twin]
    assert pair.marked.verify().findings == ()


def test_dry_runs_are_not_audited(pair: Pair) -> None:
    before = len(pair.marked.installation_events()) + len(pair.marked.graph_events())
    for caller in LIMITED:
        pair.same(caller, update("@p1", set={"email": "x@y"}), dry_run=True)
    assert len(pair.marked.installation_events()) + len(pair.marked.graph_events()) == before


def test_every_hidden_attribute_of_the_module_is_refused(pair: Pair) -> None:
    """Every hidden attribute of every type, created by the caller with ``update: true``."""
    for type_, names in HIDDEN.items():
        for name in names:
            op = {"op": "create", "type": type_, "local": "x", "attrs": {name: 1}}
            if type_ == PERSON:
                op["attrs"]["name"] = "X"
            answer = pair.same("writer", op)
            assert unknown(type_, name, local="x") in findings(answer), (type_, name)
