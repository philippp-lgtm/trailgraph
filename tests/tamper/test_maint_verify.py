"""T: maintenance effects on files, judged by storage verify plus the audit verifier.

A graph created with real events (genesis, installation event with anchor),
then release 1.0.1 and a pin switch to it, each as intent, effect and
installation companion (W.e, W.f). Contracts B5.6, B5.12, R.6; plan M3, M4.
The ``integrity.audit.pin_mismatch`` scenario lives here because it needs a
manifest and releases (``test_audit_verify`` covers the other audit codes).
"""

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from support.audit_scene import AuditedScene
from support.fixtures import compiled_model
from support.graphs import pin_of, with_pin

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage import finding_codes as storage_codes
from graph_cli.storage.common.faults import CrashPoint

STORE_ID = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"


@pytest.fixture
def scene(tmp_path: Path) -> AuditedScene:
    return AuditedScene(tmp_path / "root")


def test_create_release_and_pin_verify_clean(scene: AuditedScene) -> None:
    graph_id = scene.create_audited()
    scene.release_and_pin(graph_id, "1.0.1")
    assert scene.manifest_of(graph_id).entry.pin == pin_of(compiled_model("inventory", "1.0.1"))
    assert scene.codes() == []


def pin_back_to_the_genesis_release(scene: AuditedScene, graph_id: GraphId) -> None:
    """The anchored pin switch to 1.0.1 is undone by hand; 1.0.0 is still intact."""
    old = pin_of(compiled_model("inventory", "1.0.0"))
    scene.write_manifest(graph_id, with_pin(scene.manifest_of(graph_id), old))


def blob_location_edited_by_hand(scene: AuditedScene, graph_id: GraphId) -> None:
    """The manifest is rewritten to name an object store; no switch was recorded."""
    data = scene.manifest_of(graph_id)
    store = dataclasses.replace(data.store, blob_location="s3", store_id=STORE_ID)
    scene.write_manifest(graph_id, dataclasses.replace(data, store=store))


SCENARIOS: dict[str, Callable[[AuditedScene, GraphId], None]] = {
    audit_codes.PIN_MISMATCH: pin_back_to_the_genesis_release,
    audit_codes.BLOB_STORE_MISMATCH: blob_location_edited_by_hand,
}


@pytest.mark.parametrize("code", sorted(SCENARIOS))
def test_tamper_scenario_triggers_only_its_code(scene: AuditedScene, code: str) -> None:
    graph_id = scene.create_audited()
    scene.release_and_pin(graph_id, "1.0.1")
    SCENARIOS[code](scene, graph_id)
    assert scene.codes() == [code]


def test_storage_pin_mismatch_suppresses_the_audit_pin_check(scene: AuditedScene) -> None:
    graph_id = scene.create_audited()
    scene.release_and_pin(graph_id, "1.0.1")
    data = scene.manifest_of(graph_id)
    broken = with_pin(data, pin_of(compiled_model("inventory", "1.1.0")))  # not installed
    scene.write_manifest(graph_id, broken)
    assert scene.codes() == [storage_codes.PIN_MISMATCH]


@pytest.mark.parametrize(
    "point",
    [CrashPoint.RELEASE_BEFORE_RENAME, CrashPoint.MANIFEST_BEFORE_RENAME],
    ids=str,
)
def test_interrupted_release_or_pin_is_an_intent_without_effect(
    scene: AuditedScene, point: CrashPoint
) -> None:
    graph_id = scene.create_audited()
    scene.faults.armed.add(point)
    with pytest.raises(Exception, match="injected fault"):
        scene.release_and_pin(graph_id, "1.0.1")
    scene.faults.armed.clear()
    assert scene.codes() == [audit_codes.INTENT_WITHOUT_EFFECT]


def test_interrupted_create_is_an_intent_without_effect(scene: AuditedScene) -> None:
    scene.create_audited()
    scene.faults.armed.add(CrashPoint.CREATE_BEFORE_RENAME)
    with pytest.raises(Exception, match="injected fault"):
        scene.create_audited()
    scene.faults.armed.clear()
    findings = scene.verify()
    assert [(f.code, f.target["stream"]) for f in findings] == [
        (audit_codes.INTENT_WITHOUT_EFFECT, INSTALLATION_STREAM)
    ]


def test_a_recorded_blob_store_switch_verifies_clean(scene: AuditedScene) -> None:
    """Genesis means local; the switch's intent, manifest and anchor agree (O7)."""
    graph_id = scene.create_audited()
    scene.switch_blob_store(graph_id, STORE_ID)
    assert scene.manifest_of(graph_id).store.store_id == STORE_ID
    assert scene.codes() == []


def test_a_manifest_switched_back_by_hand_is_a_blob_store_mismatch(scene: AuditedScene) -> None:
    graph_id = scene.create_audited()
    scene.switch_blob_store(graph_id, STORE_ID)
    data = scene.manifest_of(graph_id)
    local = dataclasses.replace(data.store, blob_location="local", store_id=None)
    scene.write_manifest(graph_id, dataclasses.replace(data, store=local))
    (finding,) = scene.verify()
    assert finding.code == audit_codes.BLOB_STORE_MISMATCH
    assert finding.target["path"] == f"graphs/{graph_id}/manifest.toml"
    assert finding.expected == {"location": "s3", "store_id": STORE_ID}
    assert finding.actual == {"location": "local"}


def test_an_interrupted_blob_store_switch_is_an_intent_without_effect(
    scene: AuditedScene,
) -> None:
    """The intent is written, the manifest rename fails: the graph stays local with every
    file, as an interrupted pin switch leaves its pin (info, not an error), and the finding
    says what to do."""
    graph_id = scene.create_audited()
    scene.faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    with pytest.raises(Exception, match="injected fault"):
        scene.switch_blob_store(graph_id, STORE_ID)
    scene.faults.armed.clear()
    (finding,) = scene.verify()
    assert finding.code == audit_codes.INTENT_WITHOUT_EFFECT
    assert finding.severity.value == "info"
    assert "interrupted switch" in finding.message
    assert "in the data root" in finding.message
    assert "files move " in finding.message and "--to s3" in finding.message
