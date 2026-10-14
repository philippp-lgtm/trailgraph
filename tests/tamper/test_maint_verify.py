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
from support.audit_events import ENV, REQUEST, all_txs, allow, maint_actor
from support.fixtures import compiled_model
from support.graphs import Installation, manifest, pin_of, release_files, with_pin

from graph_cli.audit import events as builders
from graph_cli.audit import finding_codes as audit_codes
from graph_cli.audit.verify import (
    MaintEffects,
    check_anchors,
    effect_anchors,
    verify_graph,
    verify_stream,
)
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditReceipt,
    graph_stream,
)
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData
from graph_cli.storage import finding_codes as storage_codes
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.store import FileStore


class Scene(Installation):
    def maint(self, action: str, resource: str, **extra: object) -> AuditEventDraft:
        return builders.maintenance(
            actor=maint_actor(),
            decisions=[allow(action, resource)],
            request=REQUEST,
            ts=self.clock.now(),
            event_id=self.event_id(),
            env=ENV,
            **extra,  # type: ignore[arg-type]
        )

    def create_audited(self, version: str = "1.0.0") -> GraphId:
        model = compiled_model("inventory", version)
        graph_id = GraphId(self.ids.new_id())
        data = manifest(graph_id, "example_graph", pin_of(model))
        genesis = builders.graph_genesis(
            actor=maint_actor(),
            decisions=[allow("maint.graph_create", "installation")],
            request=REQUEST,
            ts=self.clock.now(),
            event_id=self.event_id(),
            env=ENV,
            graph=data.entry,
        )

        def announce(receipt: AuditReceipt) -> AuditReceipt:
            event = self.maint(
                "maint.graph_create", "installation", graph=data.entry, anchors=[receipt]
            )
            return self.audit.append_installation(event)

        with self.maintenance.registry(timeout_s=5) as registry:
            registry.create_graph(
                data, genesis, version=version, files=release_files(model), announce=announce
            )
        return graph_id

    def release_and_pin(self, graph_id: GraphId, version: str) -> None:
        model = compiled_model("inventory", version)
        resource = f"graph:{graph_id}"
        with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            current = session.manifest()
            entry = current.entry
            change = builders.release_change(version, model.artifact_hash, self.ids.new_id())
            receipt = session.append_audit(
                self.maint("maint.ontology_release", resource, graph=entry, change=change)
            )
            session.install_release(version, release_files(model), receipt)
            self.companion("maint.ontology_release", resource, entry, receipt)
            change = builders.pin_change(entry.pin, pin_of(model))
            receipt = session.append_audit(
                self.maint("maint.ontology_pin", resource, graph=entry, change=change)
            )
            session.replace_manifest(with_pin(current, pin_of(model)), receipt)
            self.companion("maint.ontology_pin", resource, entry, receipt)

    def companion(
        self, action: str, resource: str, entry: GraphEntry, receipt: AuditReceipt
    ) -> None:
        self.audit.append_installation(self.maint(action, resource, graph=entry, anchors=[receipt]))

    def verify(self) -> list[Finding]:
        """Storage verify plus the audit verifier, as ``graph-admin audit verify`` composes them."""
        installation = verify_stream(
            self.audit.read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM
        )
        anchored = effect_anchors(installation.events)
        findings = list(installation.findings)
        graph_events = {}
        for graph_id in self.maintenance.graph_ids():
            with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
                storage = list(session.verify())
                pin_broken = any(f.code == storage_codes.PIN_MISMATCH for f in storage)
                effects = MaintEffects(
                    manifest_pin=None if pin_broken else session.manifest().entry.pin,
                    releases=session.release_hashes(),
                    anchored=anchored.get(str(graph_id), frozenset()),
                )
            store = FileStore(self.root / "graphs" / str(graph_id), graph_id, syncer=self.syncer)
            report = verify_graph(
                self.audit.read_lines(graph_stream(str(graph_id))),
                graph_id=graph_id,
                txs=all_txs(store),
                effects=effects,
            )
            findings += storage + list(report.findings)
            graph_events[str(graph_id)] = report.events
        return findings + check_anchors(installation.events, graph_events)

    def codes(self) -> list[str]:
        return sorted(f.code for f in self.verify())

    def manifest_of(self, graph_id: GraphId) -> ManifestData:
        with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            return session.manifest()

    def write_manifest(self, graph_id: GraphId, data: ManifestData) -> None:
        path = self.root / "graphs" / str(graph_id) / "manifest.toml"
        path.write_bytes(format_manifest(data))


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path / "root")


def test_create_release_and_pin_verify_clean(scene: Scene) -> None:
    graph_id = scene.create_audited()
    scene.release_and_pin(graph_id, "1.0.1")
    assert scene.manifest_of(graph_id).entry.pin == pin_of(compiled_model("inventory", "1.0.1"))
    assert scene.codes() == []


def pin_back_to_the_genesis_release(scene: Scene, graph_id: GraphId) -> None:
    """The anchored pin switch to 1.0.1 is undone by hand; 1.0.0 is still intact."""
    old = pin_of(compiled_model("inventory", "1.0.0"))
    scene.write_manifest(graph_id, with_pin(scene.manifest_of(graph_id), old))


SCENARIOS: dict[str, Callable[[Scene, GraphId], None]] = {
    audit_codes.PIN_MISMATCH: pin_back_to_the_genesis_release,
}


@pytest.mark.parametrize("code", sorted(SCENARIOS))
def test_tamper_scenario_triggers_only_its_code(scene: Scene, code: str) -> None:
    graph_id = scene.create_audited()
    scene.release_and_pin(graph_id, "1.0.1")
    SCENARIOS[code](scene, graph_id)
    assert scene.codes() == [code]


def test_storage_pin_mismatch_suppresses_the_audit_pin_check(scene: Scene) -> None:
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
    scene: Scene, point: CrashPoint
) -> None:
    graph_id = scene.create_audited()
    scene.faults.armed.add(point)
    with pytest.raises(Exception, match="injected fault"):
        scene.release_and_pin(graph_id, "1.0.1")
    scene.faults.armed.clear()
    assert scene.codes() == [audit_codes.INTENT_WITHOUT_EFFECT]


def test_interrupted_create_is_an_intent_without_effect(scene: Scene) -> None:
    scene.create_audited()
    scene.faults.armed.add(CrashPoint.CREATE_BEFORE_RENAME)
    with pytest.raises(Exception, match="injected fault"):
        scene.create_audited()
    scene.faults.armed.clear()
    findings = scene.verify()
    assert [(f.code, f.target["stream"]) for f in findings] == [
        (audit_codes.INTENT_WITHOUT_EFFECT, INSTALLATION_STREAM)
    ]
