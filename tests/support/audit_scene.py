"""An installation with real audit events for the verification tests (contracts B5.6, B5.12).

``AuditedScene`` creates graphs with a genesis and an installation event, installs releases and
switches pins as intent, effect and installation companion (W.e, W.f), writes data with real
write intents and runs a migration the way the release will (plan M3: marker, ``maint.migrate``
intent, ``migrate``, pin intent with the alias prefixes, manifest switch, marker removed).
``verify`` composes storage verify and the audit verifier like ``graph-admin audit verify``.
"""

import dataclasses
from collections.abc import Callable, Mapping

from graph_cli.audit import events as builders
from graph_cli.audit.verify import (
    MaintEffects,
    blob_store_state,
    check_anchors,
    effect_anchors,
    verify_graph,
    verify_stream,
)
from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import EntityId, GraphId, TxId
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditReceipt,
    graph_stream,
)
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData
from graph_cli.kernel.query_ast import Query
from graph_cli.storage import finding_codes as storage_codes
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.store import FileStore
from support import audit_events
from support.audit_events import ENV, REQUEST, all_txs, allow, maint_actor
from support.fixtures import compiled_model
from support.graphs import Installation, manifest, pin_of, release_files, with_pin
from support.migrations import MigrationDraft, begin_migration, migration_record
from support.stores import ALL_STATUSES, snapshot_state
from support.txbuilder import TxBuilder

EVERYTHING = Query(record_status=ALL_STATUSES, limit=10**9)


def change_migration(change: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The mutable ``migration`` object of a ``migration_change``."""
    record = change["migration"]
    assert isinstance(record, dict)
    return record


class AuditedScene(Installation):
    """An installation whose graph creation, releases, pins and writes carry real audit events
    (W.e, W.f), with the verification ``graph-admin audit verify`` composes."""

    def maint(self, action: str, resource: str, **extra: object) -> AuditEventDraft:
        extra.setdefault("event_id", self.event_id())
        return builders.maintenance(
            actor=maint_actor(),
            decisions=[allow(action, resource)],
            request=REQUEST,
            ts=self.clock.now(),
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

    def switch_blob_store(self, graph_id: GraphId, store_id: str) -> None:
        """Switch a graph that holds no stored file to the object store ``store_id`` as
        ``graph create`` does (intent with ``change.blob_store``, manifest, companion)."""
        resource = f"graph:{graph_id}"
        with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            current = session.manifest()
            change = builders.blob_store_change("local", "s3", store_id, blobs=0, size=0)
            receipt = session.append_audit(
                self.maint("maint.graph_configure", resource, graph=current.entry, change=change)
            )
            store = dataclasses.replace(current.store, blob_location="s3", store_id=store_id)
            session.replace_manifest(dataclasses.replace(current, store=store), receipt)
            self.companion("maint.graph_configure", resource, current.entry, receipt)

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
            store = FileStore(self.root / "graphs" / str(graph_id), graph_id, syncer=self.syncer)
            with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
                storage = list(session.verify())
                pin_broken = any(f.code == storage_codes.PIN_MISMATCH for f in storage)
                located = None if pin_broken else session.manifest().store
                effects = MaintEffects(
                    manifest_pin=None if pin_broken else session.manifest().entry.pin,
                    releases=session.release_hashes(),
                    anchored=anchored.get(str(graph_id), frozenset()),
                    migration_marker=store.migration_marker(),
                    blob_store=None
                    if located is None
                    else blob_store_state(located.blob_location, located.store_id),
                )
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

    def write_entities(
        self, graph_id: GraphId, count: int, *, version: str = "1.0.0"
    ) -> list[EntityId]:
        """One audited write tx that creates ``count`` entities under ``version``."""
        builder = self.builder(graph_id, version)
        created: list[EntityId] = []
        with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            draft = builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            created.extend(draft.create() for _ in range(count))
            tx = draft.build()
            receipt = session.append_audit(audit_events.intent(tx))
            session.commit(tx, receipt)
        return created

    def builder(self, graph_id: GraphId, version: str) -> TxBuilder:
        return TxBuilder(
            compiled_model("inventory", version), graph_id=graph_id, ids=self.ids, clock=self.clock
        )

    def migrate(
        self,
        graph_id: GraphId,
        to_version: str = "2.0.0",
        *,
        edit: Callable[[MigrationDraft], object] | None = None,
        reason: str = "example release",
        install: bool = True,
        marker: bool = True,
        pin: bool = True,
        remove_marker: bool = True,
        prefixes: Mapping[str, JsonValue] | None = None,
        intent_edit: Callable[[dict[str, JsonValue]], object] | None = None,
    ) -> AuthorizedTx:
        """A release with a migration, step by step as ``release.write`` will do it: install
        the target release, write the marker, append the ``maint.migrate`` intent, ``migrate``,
        append the pin intent (with ``prefixes`` as ``change.alias_prefixes``), switch the
        manifest, remove the marker. The flags stop the sequence at a crash point or skip a
        step (``pin=False`` leaves the committed migration unpinned). By default every entity
        is revised once without a content change; ``edit`` revises as it likes; ``intent_edit``
        changes the ``change.migration`` the intent records (a writer that records it wrong).
        """
        resource = f"graph:{graph_id}"
        model = compiled_model("inventory", to_version)
        target = pin_of(model)
        with self.maintenance.maintenance_session(graph_id, timeout_s=5) as session:
            current = session.manifest()
            entry = current.entry
            from_version = entry.pin.version
            record = migration_record(from_version=from_version, to_version=to_version)
            migration_hash = record["migration_hash"]
            assert isinstance(migration_hash, str)
            if install:
                change = builders.release_change(
                    to_version, model.artifact_hash, self.ids.new_id(), migration_hash
                )
                receipt = session.append_audit(
                    self.maint("maint.ontology_release", resource, graph=entry, change=change)
                )
                session.install_release(to_version, release_files(model), receipt)
                self.companion("maint.ontology_release", resource, entry, receipt)
            snapshot = session.snapshot()
            draft = begin_migration(
                self.builder(graph_id, to_version), snapshot_state(snapshot), session.head()
            )
            if edit is None:
                for revision in snapshot.query(EVERYTHING).items:
                    draft.revise(revision.id)
            else:
                edit(draft)
            changeset: dict[str, JsonValue] = {
                "schema": "graph_cli.migration/1",
                "ops": [],
                "migration": record,
            }
            tx = draft.build(changeset=changeset)
            if marker:
                session.write_migration_marker(
                    MigrationMarker(
                        seq=tx.seq,
                        tx_id=TxId(tx.tx_id),
                        from_version=from_version,
                        to_version=to_version,
                        artifact_hash=model.artifact_hash,
                        migration_hash=migration_hash,
                    )
                )
            change = builders.migration_change(
                tx,
                revisions_hash=builders.revisions_hash(tx.revisions),
                counts={"entities": len(tx.revisions), "edges": 0},
            )
            if intent_edit is not None:
                intent_edit(change_migration(change))
            receipt = session.append_audit(
                self.maint(
                    "maint.migrate",
                    resource,
                    graph=entry,
                    change=change,
                    event_id=tx.event_id,
                    reason=reason,
                )
            )
            session.migrate(tx, receipt)
            if pin:
                change = builders.pin_change(entry.pin, target, prefixes)
                receipt = session.append_audit(
                    self.maint("maint.ontology_pin", resource, graph=entry, change=change)
                )
                session.replace_manifest(with_pin(current, target), receipt)
                self.companion("maint.ontology_pin", resource, entry, receipt)
            if marker and remove_marker and pin:
                session.remove_migration_marker()
        return tx
