"""T: maintenance effects on files, judged by storage verify plus the audit verifier.

A graph created with real events (genesis, installation event with anchor),
then release 1.0.1 and a pin switch to it, each as intent, effect and
installation companion (W.e, W.f). Contracts B5.6, B5.12, R.6; plan M3, M4.
The ``integrity.audit.pin_mismatch`` scenario lives here because it needs a
manifest and releases (``test_audit_verify`` covers the other audit codes).
"""

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


SCENARIOS: dict[str, Callable[[AuditedScene, GraphId], None]] = {
    audit_codes.PIN_MISMATCH: pin_back_to_the_genesis_release,
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
