"""Installed policy files: replaced by the maintenance port after their event
(contracts B5.15, B6.14; section 5 ``policy/``; crash point ``policy.before_rename``)."""

import json
from pathlib import Path

import pytest
from support.drafts import draft
from support.graphs import Installation

from graph_cli.kernel.canonical import JsonValue, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditReceipt, graph_stream
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileMaintenanceSession

DOCUMENT: dict[str, JsonValue] = {"schema": "graph_cli.policy/1", "id": "installation", "rules": []}


@pytest.fixture
def inst(tmp_path: Path) -> Installation:
    return Installation(tmp_path / "root")


def installation_event(inst: Installation) -> AuditReceipt:
    event = draft(
        INSTALLATION_STREAM, inst.event_id(), inst.clock.now(), actions=("maint.policy_change",)
    )
    return inst.audit.append_installation(event)


def graph_intent(
    inst: Installation, session: FileMaintenanceSession, graph_id: GraphId
) -> AuditReceipt:
    event = draft(
        graph_stream(str(graph_id)),
        inst.event_id(),
        inst.clock.now(),
        actions=("maint.policy_change",),
    )
    return session.append_audit(event)


def test_installation_policy(inst: Installation) -> None:
    path = inst.root / "policy" / "installation.json"
    with inst.maintenance.registry(timeout_s=5) as registry:
        assert registry.read_installation_policy() is None
        receipt = installation_event(inst)
        registry.replace_installation_policy(DOCUMENT, receipt)
        assert registry.read_installation_policy() == pretty_bytes(DOCUMENT)
        with pytest.raises(GraphCliError) as reused:
            registry.replace_installation_policy(DOCUMENT, receipt)  # one event, one effect
        assert reused.value.code == "internal.error"
    assert json.loads(path.read_bytes()) == DOCUMENT
    assert sorted(p.name for p in path.parent.iterdir()) == ["installation.json"]


def test_installation_policy_needs_an_installation_event(inst: Installation) -> None:
    graph_id = inst.create()
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        receipt = graph_intent(inst, session, graph_id)
    with inst.maintenance.registry(timeout_s=5) as registry, pytest.raises(GraphCliError):
        registry.replace_installation_policy(DOCUMENT, receipt)
    assert not (inst.root / "policy").exists()


def test_graph_policy(inst: Installation) -> None:
    graph_id = inst.create()
    layer: dict[str, JsonValue] = {**DOCUMENT, "id": "graph", "layer": "graph"}
    with inst.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
        assert session.read_policy() is None
        session.replace_policy(layer, graph_intent(inst, session, graph_id))
        assert session.read_policy() == pretty_bytes(layer)
        with pytest.raises(GraphCliError):  # the receipt must be this session's intent
            session.replace_policy(layer, installation_event(inst))
    assert json.loads((inst.root / "graphs" / str(graph_id) / "policy.json").read_bytes()) == layer


def test_interrupted_apply_keeps_the_old_policy(inst: Installation) -> None:
    newer: dict[str, JsonValue] = {**DOCUMENT, "id": "newer"}
    with inst.maintenance.registry(timeout_s=5) as registry:
        registry.replace_installation_policy(DOCUMENT, installation_event(inst))
        inst.faults.armed.add(CrashPoint.POLICY_BEFORE_RENAME)
        with pytest.raises(GraphCliError) as caught:
            registry.replace_installation_policy(newer, installation_event(inst))
        assert caught.value.code == "backend.unavailable"
        assert registry.read_installation_policy() == pretty_bytes(DOCUMENT)
        inst.faults.armed.clear()
        registry.replace_installation_policy(newer, installation_event(inst))
        assert registry.read_installation_policy() == pretty_bytes(newer)
