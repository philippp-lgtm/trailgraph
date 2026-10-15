"""Graphs created through the storage maintenance port (P1 item 5).

Storage tests create graphs with minimal audit drafts (``support.drafts``);
the manifest pins a fixture model whose ``compiled.json`` is the only
release file the storage checks need.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from graph_cli.kernel.canonical import pretty_bytes
from graph_cli.kernel.ids import EventId, GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditReceipt, graph_stream
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.records import OntologyPin
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.durable import write_tree
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.manifest import format_manifest
from support.clock import StepClock
from support.drafts import draft
from support.fixtures import compiled_model
from support.ids import SeededIdProvider
from support.stores import FaultSwitch, RecordingSyncer

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def pin_of(model: CompiledModel) -> OntologyPin:
    return OntologyPin(model.bundle_id, model.bundle_version, model.artifact_hash)


def release_files(model: CompiledModel) -> dict[str, bytes]:
    return {"compiled.json": pretty_bytes(model.to_json())}


def manifest(graph_id: GraphId, name: str, pin: OntologyPin) -> ManifestData:
    entry = GraphEntry(graph_id, name, "active", pin, {}, ())
    return ManifestData(entry, CREATED_AT, StoreInfo("files", 1))


def with_pin(data: ManifestData, pin: OntologyPin) -> ManifestData:
    return replace(data, entry=replace(data.entry, pin=pin))


def publish_ontology(
    graph_dir: Path, graph_id: GraphId, ontology: str, syncer: RecordingSyncer
) -> CompiledModel:
    """Give a test graph directory its manifest and pinned release without the port."""
    model = compiled_model(ontology)
    (graph_dir / "manifest.toml").write_bytes(
        format_manifest(manifest(graph_id, "example_graph", pin_of(model)))
    )
    write_tree(graph_dir / "ontology" / model.bundle_version, release_files(model), syncer)
    return model


@dataclass
class Installation:
    """A data root with the maintenance port, the audit log and seeded ids."""

    root: Path
    faults: FaultSwitch = field(default_factory=FaultSwitch)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = StepClock()
        self.ids = SeededIdProvider(self.clock, 7)
        self.syncer = RecordingSyncer()
        self.maintenance = FileStoreMaintenance(self.root, syncer=self.syncer, faults=self.faults)
        self.audit = FileAuditLog(self.root, syncer=self.syncer, faults=self.faults)
        self.announced: list[AuditReceipt] = []

    def announce(self, receipt: AuditReceipt) -> AuditReceipt:
        """The installation event of a graph create (minimal draft, no anchor)."""
        self.announced.append(receipt)
        event = draft(
            INSTALLATION_STREAM, self.event_id(), self.clock.now(), actions=("maint.graph_create",)
        )
        return self.audit.append_installation(event)

    def event_id(self) -> EventId:
        return EventId(self.ids.new_id())

    def create(
        self,
        name: str = "example_graph",
        *,
        ontology: str = "inventory",
        version: str | None = None,
        graph_id: GraphId | None = None,
        files: Mapping[str, bytes] | None = None,
    ) -> GraphId:
        model = compiled_model(ontology, version)
        graph_id = graph_id or GraphId(self.ids.new_id())
        with self.maintenance.registry(timeout_s=5) as registry:
            registry.create_graph(
                manifest(graph_id, name, pin_of(model)),
                draft(
                    graph_stream(str(graph_id)),
                    self.event_id(),
                    self.clock.now(),
                    actions=("maint.graph_create",),
                ),
                version=model.bundle_version,
                files=files if files is not None else release_files(model),
                announce=self.announce,
            )
        return graph_id
