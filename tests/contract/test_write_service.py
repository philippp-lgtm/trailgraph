"""The write service on both backends x both fixtures (contracts B3, B2.9, B3.14, B5.2; plan W7).

Role-based: tests never name a type; they ask the fixture's roles file.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.roles import Roles, lifecycle_path, load_roles
from support.services import ServiceEnv
from support.trees import tree_hash

from graph_cli.kernel.canonical import canonical_bytes
from graph_cli.kernel.changeset import ChangeSet, UpdateOp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import AbbrevRef, EntityId, parse_id
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.write import WriteRequest
from graph_cli.storage.common.faults import CrashPoint


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    return ServiceEnv(backend, ontology, tmp_path / "root")


@pytest.fixture
def roles(ontology: str) -> Roles:
    return load_roles(ontology)


def created(result: Any, local: str = "x") -> str:
    return str(next(e.id for e in result.entities if e.local == local))


def results_by_status(events: list[Any]) -> list[str]:
    return [str(e["result"]["status"]) for e in events]  # type: ignore[index]


def test_create_commits_one_tx_after_its_intent(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    result = svc.create(cls)
    assert (result.seq, result.dry_run, result.idempotent_replay) == (1, False, False)
    mine = next(e for e in result.entities if e.local == "x")
    assert (mine.type, mine.rev, mine.ops) == (cls, 1, ("create",))
    prefix = dict(svc.entry.alias_prefixes).get(cls)  # type: ignore[call-overload]
    assert mine.alias == (f"{prefix}-0001" if prefix else None)
    (tx,) = svc.txs()
    events = svc.graph_events()
    assert results_by_status(events) == ["ok", "ok"]  # genesis, intent
    intent = events[-1]
    change = intent["change"]
    assert isinstance(change, dict)
    assert change["tx_id"] == str(tx.tx_id) and change["changeset_hash"] == tx.changeset_hash
    assert canonical_bytes(intent["actor"]) == canonical_bytes(tx.actor.to_json())  # B2.9
    assert [r.id for r in tx.revisions] == [e.id for e in result.entities]
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]


def test_last_op_invalid_changes_nothing(svc: ServiceEnv, roles: Roles) -> None:
    ops = svc.factory.create(roles.term("lifecycle_type"), "x")
    ops.append({"op": "update", "id": "@x", "set": {"no_such_attribute": "v"}})
    error = svc.refuse(*ops)
    assert error.code == "write.invalid"
    findings = error.details["findings"]
    assert isinstance(findings, list)
    assert [f["code"] for f in findings] == ["data.structural.unknown_attribute"]  # type: ignore[index]
    assert "id" not in findings[0]["target"] and findings[0]["target"]["local"] == "x"  # type: ignore[index]
    assert svc.head_seq() == 0 and svc.txs() == []
    refusal = svc.graph_events()[-1]
    assert refusal["result"] == {  # B5.8: code and target only
        "status": "invalid",
        "code": "write.invalid",
        "findings": [{"code": f["code"], "target": f["target"]} for f in findings],  # type: ignore[index]
    }


def test_dry_run_writes_nothing(svc: ServiceEnv, roles: Roles, tmp_path: Path) -> None:
    before = (tree_hash(tmp_path), len(svc.graph_events()), len(svc.installation_events()))
    result = svc.apply(*svc.factory.create(roles.term("lifecycle_type"), "x"), dry_run=True)
    assert result.dry_run and result.tx_id is None and result.seq is None
    assert all(e.id is None and e.alias is None for e in result.entities)
    error = svc.refuse(
        *svc.factory.create(roles.term("lifecycle_type"), "y"),
        {"op": "update", "id": "@y", "set": {"no_such_attribute": 1}},
        dry_run=True,
    )
    (finding,) = error.details["findings"]  # type: ignore[misc]
    assert "id" not in finding["target"] and finding["target"]["local"] == "y"
    after = (tree_hash(tmp_path), len(svc.graph_events()), len(svc.installation_events()))
    assert before == after and svc.head_seq() == 0


def test_idempotent_replay_and_key_reuse(svc: ServiceEnv, roles: Roles) -> None:
    ops = svc.factory.create(roles.term("lifecycle_type"), "x")
    first = svc.apply(*ops, idempotency_key="k-1")
    events = len(svc.graph_events())
    again = svc.apply(*ops, idempotency_key="k-1")
    assert again.idempotent_replay and (again.tx_id, again.seq) == (first.tx_id, first.seq)
    assert again.entities == first.entities and again.bindings == first.bindings
    assert svc.head_seq() == 1 and len(svc.graph_events()) == events
    other = svc.factory.create(roles.term("lifecycle_type"), "x")
    error = svc.refuse(*other, idempotency_key="k-1")
    assert error.code == "conflict.idempotency_key_reuse"
    assert results_by_status(svc.graph_events())[-1] == "conflict"
    assert svc.head_seq() == 1


def test_expect_seq_and_expect_rev(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    entity = created(svc.create(cls))
    assert svc.refuse(*svc.factory.create(cls, "y"), expect_seq=0).code == "conflict.seq_mismatch"
    name = svc.factory.attrs(cls)
    attr = next(n for n in name if isinstance(name[n], str))
    error = svc.refuse({"op": "update", "id": entity, "set": {attr: "new"}, "expect_rev": 2})
    assert error.code == "conflict.version_mismatch"
    assert error.details["expected"] == 2 and error.details["actual"] == 1
    assert results_by_status(svc.graph_events())[-2:] == ["conflict", "conflict"]
    assert svc.apply({"op": "update", "id": entity, "set": {attr: "new"}, "expect_rev": 1}).seq == 2
    assert svc.apply(*svc.factory.create(cls, "z"), expect_seq=2).seq == 3


def test_supersede_and_retract_need_a_reason(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("supersede_target_type")
    old = created(svc.create(cls, "old"), "old")
    new = created(svc.create(cls, "new"), "new")
    installation = len(svc.installation_events())
    supersede = {"op": "supersede", "by": new, "targets": [old]}
    error = svc.refuse(supersede, dry_run=True)
    assert error.code == "access.reason_required"
    assert len(svc.installation_events()) == installation  # dry-run denials are not audited
    assert svc.refuse(supersede).code == "access.reason_required"
    denial = svc.installation_events()[-1]
    assert denial["result"] == {"status": "denied", "code": "access.reason_required"}
    assert denial["graph"]["id"] == str(svc.entry.id)  # type: ignore[index]
    result = svc.apply(supersede, reason="replaced")
    assert [(str(e.id), e.ops) for e in result.entities] == [
        (new, ("supersede",)),
        (old, ("supersede",)),
    ]
    assert _status(svc, old) == "superseded"
    retractable = created(svc.create(roles.term("retractable_type"), "r"), "r")
    assert svc.refuse({"op": "retract", "id": retractable}).code == "access.reason_required"
    svc.apply({"op": "retract", "id": retractable}, reason="wrong")
    assert _status(svc, retractable) == "retracted"
    error = svc.refuse({"op": "retract", "id": retractable}, reason="again")
    assert error.code == "write.invalid"
    assert _codes(error) == ["data.temporal.record_not_active"]


def _status(svc: ServiceEnv, entity: str) -> str:
    current = svc.snapshot().get(EntityId(parse_id(entity)))
    assert current is not None
    return current.record_status


def _codes(error: GraphCliError) -> list[str]:
    findings = error.details["findings"]
    assert isinstance(findings, list)
    return [str(f["code"]) for f in findings]  # type: ignore[index, call-overload]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"state": "archived"}, "graph.archived"),
        ({"state": "readonly"}, "graph.readonly"),
    ],
)
def test_graph_state_refuses_writes(
    svc: ServiceEnv, roles: Roles, change: dict[str, Any], code: str
) -> None:
    svc.set_entry(**change)
    events = len(svc.graph_events())
    assert svc.refuse(*svc.factory.create(roles.term("lifecycle_type"))).code == code
    assert len(svc.graph_events()) == events


def test_tampered_artifact_refuses_writes(svc: ServiceEnv, roles: Roles) -> None:
    svc.tamper_artifact()
    error = svc.refuse(*svc.factory.create(roles.term("lifecycle_type")))
    assert error.code == "ontology.pin_mismatch"
    assert svc.head_seq() == 0


def test_failing_audit_sink_refuses_the_write(svc: ServiceEnv, roles: Roles) -> None:
    """P1 exit criterion (service level): no intent, no tx (B3.4)."""
    svc.faults.armed.add(CrashPoint.AUDIT_BEFORE_WRITE)
    error = svc.refuse(*svc.factory.create(roles.term("lifecycle_type")))
    assert error.code == "audit.unavailable"
    svc.faults.armed.clear()
    assert svc.head_seq() == 0 and svc.txs() == []
    assert results_by_status(svc.graph_events()) == ["ok"]  # genesis only


def test_unrecorded_refusal_keeps_the_error_and_warns(svc: ServiceEnv, roles: Roles) -> None:
    ops = svc.factory.create(roles.term("lifecycle_type"), "x")
    ops.append({"op": "update", "id": "@x", "set": {"no_such_attribute": 1}})
    svc.faults.armed.add(CrashPoint.AUDIT_BEFORE_WRITE)
    error = svc.refuse(*ops)
    assert error.code == "write.invalid"
    assert [w.code for w in error.warnings] == ["audit.refusal_not_recorded"]


def test_storage_warnings_are_passed_on(svc: ServiceEnv, roles: Roles) -> None:
    svc.faults.armed.add(CrashPoint.TX_AFTER_SYNC)
    result = svc.create(roles.term("lifecycle_type"))
    assert [w.code for w in result.warnings] == ["storage.materialization_deferred"]
    svc.faults.armed.clear()
    assert svc.head_seq() == 1


def test_lifecycle_and_guard_paths(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    attr = roles.term("lifecycle_attribute").rpartition(".")[2]
    guarded = roles.get("guarded_transition")
    assert isinstance(guarded, dict)
    lifecycle = svc.model.lifecycles[guarded["lifecycle"]]
    good, bad = created(svc.create(cls, "good"), "good"), created(svc.create(cls, "bad"), "bad")
    for entity in (good, bad):
        for state in lifecycle_path(
            svc.model, guarded["lifecycle"], lifecycle.initial[0], guarded["from"]
        ):
            svc.apply({"op": "transition", "id": entity, "to": state})
    breaking = roles.get("guard_breaking_update")
    assert isinstance(breaking, dict)
    svc.apply({"op": "update", "id": bad, "set": breaking})
    error = svc.refuse({"op": "transition", "id": bad, "to": guarded["to"]})
    (finding,) = error.details["findings"]  # type: ignore[misc]
    assert finding["code"] == "data.lifecycle.guard_failed" and finding["rule_id"]
    result = svc.apply({"op": "transition", "id": good, "to": guarded["to"]})
    assert result.entities[0].ops == ("transition",)
    current = svc.snapshot().get(result.entities[0].id)  # type: ignore[arg-type]
    assert current is not None and current.attrs[attr] == guarded["to"]
    error = svc.refuse({"op": "update", "id": good, "set": {attr: guarded["from"]}})
    assert _codes(error) == ["data.lifecycle.direct_update_of_lifecycle_attribute"]


def test_apply_batch_carries_locals_and_stops_at_the_first_failure(
    svc: ServiceEnv, roles: Roles
) -> None:
    cls = roles.term("lifecycle_type")
    attr = next(n for n, v in svc.factory.attrs(cls).items() if isinstance(v, str))
    documents = [
        svc.factory.create(cls, "a"),
        [{"op": "update", "id": "@a", "set": {attr: "renamed"}}],
        [{"op": "update", "id": "@a", "set": {"no_such_attribute": 1}}],
        svc.factory.create(cls, "never"),
    ]
    outcome = svc.batch(documents)
    assert [r.seq for r in outcome.results] == [1, 2]
    assert outcome.failed_index == 2 and outcome.error is not None
    assert outcome.error.code == "write.invalid"
    assert svc.head_seq() == 2
    a = outcome.results[0].bindings["a"]
    assert outcome.results[1].entities[0].id == a and outcome.results[1].entities[0].local == "a"


def test_dry_run_batch_sees_earlier_documents(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    attr = next(n for n, v in svc.factory.attrs(cls).items() if isinstance(v, str))
    outcome = svc.batch(
        [svc.factory.create(cls, "a"), [{"op": "update", "id": "@a", "set": {attr: "x"}}]],
        dry_run=True,
    )
    assert outcome.error is None and len(outcome.results) == 2
    assert outcome.results[1].entities[0].id is None
    assert svc.head_seq() == 0


def test_aliases_are_contiguous_across_txs(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    prefix = dict(svc.entry.alias_prefixes).get(cls)  # type: ignore[call-overload]
    if prefix is None:
        pytest.skip("no alias prefix for this type")
    aliases = [next(e.alias for e in svc.create(cls).entities if e.local == "x") for _ in range(3)]
    assert aliases == [f"{prefix}-0001", f"{prefix}-0002", f"{prefix}-0003"]
    renamed = svc.apply({"op": "set_alias", "id": aliases[0], "alias": f"{prefix}-0009"})
    assert renamed.entities[0].alias == f"{prefix}-0009"
    by_old = svc.apply({"op": "set_alias", "id": aliases[0], "alias": f"{prefix}-0010"})
    assert [w.code for w in by_old.warnings] == ["entity.alias_renamed"]
    assert next(e.alias for e in svc.create(cls).entities if e.local == "x") == f"{prefix}-0011"
    error = svc.refuse({"op": "set_alias", "id": aliases[1], "alias": f"{prefix}-0009"})
    assert _codes(error) == ["data.semantic.unique_violation"]


def test_abbreviated_cli_references(svc: ServiceEnv, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    entity = next(e.id for e in svc.create(cls).entities if e.local == "x")
    assert entity is not None
    attr = next(n for n, v in svc.factory.attrs(cls).items() if isinstance(v, str))
    request = WriteRequest(
        actor=svc.actor,
        graph=str(svc.entry.id),
        changeset=ChangeSet(ops=(UpdateOp(AbbrevRef(entity.hex[-12:]), {attr: "x"}),)),
        dry_run=False,
        request=RequestMeta("update", "sha256:" + "0" * 64),
        allow_abbrev=True,
    )
    result = svc.service.apply(request)
    assert result.entities[0].id == entity
    assert svc.txs()[-1].changeset["ops"] == [  # recorded with the full id
        {"op": "update", "id": str(entity), "set": {attr: "x"}}
    ]


def test_unknown_graph_and_limits(svc: ServiceEnv, roles: Roles) -> None:
    request = svc.request(*svc.factory.create(roles.term("lifecycle_type")))
    with pytest.raises(GraphCliError) as info:
        svc.service.apply(replace(request, graph="nope"))
    assert info.value.code == "graph.not_found"
    with pytest.raises(GraphCliError) as info:
        svc.make_service(max_ops=1).apply(request)
    assert info.value.code == "changeset.too_large"
