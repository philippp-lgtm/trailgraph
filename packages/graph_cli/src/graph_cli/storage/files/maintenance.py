"""FileStore maintenance port (contracts B13.1, sections 4.2, 5, W.e, W.f).

Imported only by graph_cli_admin (import-linter contract). A maintenance
session holds the graph lock but does not recover on open, so ``verify``
sees the files as they are. The ``WriteSession`` methods recover first, like
an application session.

Maintenance effects are published by one rename each, which is their commit
point: ``graphs/.tmp-<id>`` -> ``graphs/<id>`` (graph create, under the
registry lock ``<root>/.lock``), ``ontology/.tmp-<version>`` ->
``ontology/<version>``, ``manifest.toml.tmp`` -> ``manifest.toml`` and
``skill/source.json.tmp`` -> ``skill/source.json``,
``functions/slot.json.tmp`` -> ``functions/slot.json`` (and the same for ``actions/``),
``policy.json.tmp`` ->
``policy.json`` (under the graph lock, after the intent event) and
``policy/installation.json.tmp`` -> ``policy/installation.json`` (under the
registry lock, after the installation event). A failure before the rename
leaves the staging state behind, as a kill -9 would; readers ignore
``.tmp-*`` entries.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import (
    JsonValue,
    hash_value,
    pretty_bytes,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EntityId, GraphId, format_id, parse_id
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditReceipt,
    graph_stream,
)
from graph_cli.kernel.ports.catalog import (
    BlobLocation,
    KineticSlot,
    ManifestData,
    SkillSlot,
    SlotKind,
    skill_hash,
    sources_hash,
)
from graph_cli.kernel.ports.storage import (
    BlobCopies,
    BlobVerification,
    CommitResult,
    CopyBudget,
    GraphStoreMaintenance,
    Head,
    MaintenanceSession,
    MisplacedCopies,
    RecordedStore,
    RecoveryReport,
    RegistrySession,
    Snapshot,
    StagedBlob,
)
from graph_cli.kernel.records import BlobInfo, TxRecord, revision_content_hash, revision_to_json
from graph_cli.storage.common.audit import audit_body
from graph_cli.storage.common.chain import GENESIS, link_of, seal
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files import blobs
from graph_cli.storage.files.blob_backend import BlobBackend, BlobBackends, blob_manifest
from graph_cli.storage.files.blob_move import copy_pass, lock_free_referenced
from graph_cli.storage.files.durable import (
    Durability,
    OsSyncer,
    Syncer,
    ensure_dir,
    write_temp,
    write_tree,
)
from graph_cli.storage.files.headfile import HeadFile, parse_head_file
from graph_cli.storage.files.index import INDEX_ERRORS, TxSpan, index_difference
from graph_cli.storage.files.kinetic_slot import read_slot
from graph_cli.storage.files.layout import (
    AUDIT_SEGMENT_PREFIX,
    DEFAULT_SEGMENT_BYTES,
    REVISION_FILE_RE,
    STAGING_PREFIX,
    GraphLayout,
    init_graph_dir,
)
from graph_cli.storage.files.locking import acquire
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.policy_files import (
    installation_policy_path,
    read_policy_file,
    write_policy_file,
)
from graph_cli.storage.files.recovery import recover
from graph_cli.storage.files.releases import (
    COMPILED_FILE,
    VERSION_RE,
    is_relative_name,
    read_manifest,
    read_release_file,
    release_hashes,
)
from graph_cli.storage.files.s3_blobs import ClientFactory
from graph_cli.storage.files.segments import OpLog, SegmentedLog
from graph_cli.storage.files.skill_slot import read_skill
from graph_cli.storage.files.store import FileSession, FileStore, parse_tx
from graph_cli.storage.files.store_format import check_store
from graph_cli.storage.files.token_files import (
    enrollments_path,
    read_tokens_file,
    tokens_path,
    write_tokens_file,
)
from graph_cli.storage.finding_codes import (
    BLOB_HASH_MISMATCH,
    BLOB_MISPLACED,
    BLOB_MISSING,
    HEAD_INCONSISTENT,
    INDEX_INCONSISTENT,
    PIN_MISMATCH,
    REVISION_HASH_MISMATCH,
    REVISION_MISSING,
    REVISION_UNEXPECTED,
    TX_CHAIN_BROKEN,
)
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.settings import ObjectStoreSettings

# How finding messages name a blob location.
_LOCATION_NAMES: Final = {"local": "the data root", "s3": "the object store"}


class FileStoreMaintenance:
    """Maintenance access to every graph below ``<data_root>/graphs/``."""

    def __init__(
        self,
        data_root: Path,
        *,
        durability: Durability = "full",
        syncer: Syncer | None = None,
        faults: FaultHook | None = None,
        segment_bytes: int = DEFAULT_SEGMENT_BYTES,
        oplog: OpLog | None = None,
        object_store: ObjectStoreSettings | None = None,
        object_store_problem: GraphCliError | None = None,
        client_factory: ClientFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.data_root = data_root
        self.syncer: Syncer = syncer if syncer is not None else OsSyncer(durability)
        self.faults = faults
        self.segment_bytes = segment_bytes
        self.oplog = oplog
        # Built by the admin wiring from ``[files.s3]`` and the environment; storage never
        # reads either. ``object_store_problem`` says why ``object_store`` is ``None``.
        self.object_store = object_store
        self.object_store_problem = object_store_problem
        # tests: the object store client (default ``ObjectStoreClient``) and the monotonic
        # clock of the store marker cache, as ``BlobBackends`` takes them
        self.client_factory = client_factory
        self.monotonic = monotonic

    @property
    def blob_backends(self) -> BlobBackends:
        return BlobBackends(
            self.object_store,
            self.object_store_problem,
            client_factory=self.client_factory,
            monotonic=self.monotonic,
        )

    def misplaced_blobs(
        self, graph: GraphId, *, recorded_store: RecordedStore | None = None
    ) -> MisplacedCopies | None:
        return misplaced_copies(self.store(graph), recorded_store)

    def copy_blobs(
        self,
        graph: GraphId,
        *,
        to: str,
        store_id: str | None,
        stop: CopyBudget | None = None,
        dry_run: bool = False,
    ) -> BlobCopies:
        store = self.store(graph)
        source, target = _move_backends(store, to, store_id, dry_run=dry_run)
        referenced = lock_free_referenced(store)
        return copy_pass(
            source, target, referenced, stop=stop, dry_run=dry_run, faults=store.faults
        )

    def object_store_client(self) -> ObjectStoreClient | None:
        """A client of the configured object store (``files store check``); ``None``
        without one."""
        if self.object_store is None:
            return None
        factory: ClientFactory = self.client_factory or ObjectStoreClient
        return factory(self.object_store)

    def store(self, graph: GraphId) -> FileStore:
        """The graph's store, after the checks ``FileCatalog.file_store`` makes
        (``store_format.check_store``): an unsupported manifest format, feature, backend or
        layout is refused; object store settings are checked on blob access. A manifest
        that cannot be read at all is left to the caller: ``verify`` reports it
        (``pin_mismatch``), a missing graph is ``backend.unavailable`` on first use."""
        root = self.data_root / "graphs" / str(graph)
        try:
            manifest = read_manifest(GraphLayout(root))[0]
        except GraphCliError as exc:
            if exc.code != "config.invalid":
                raise
        else:
            check_store(manifest)
        return FileStore(
            root,
            graph,
            syncer=self.syncer,
            faults=self.faults,
            segment_bytes=self.segment_bytes,
            oplog=self.oplog,
            blob_backends=self.blob_backends,
        )

    @property
    def graphs_dir(self) -> Path:
        return self.data_root / "graphs"

    def _require_root(self) -> None:
        if not self.data_root.is_dir():
            raise GraphCliError(
                "backend.unavailable",
                "the data root does not exist",
                details={"data_root": str(self.data_root)},
            )

    def graph_ids(self) -> Sequence[GraphId]:
        self._require_root()
        try:
            names = os.listdir(self.graphs_dir)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise _unavailable("cannot list graphs", exc) from exc
        return sorted(GraphId(parse_id(n)) for n in names if _is_graph_dir_name(n))

    @contextmanager
    def registry(self, *, timeout_s: float) -> Iterator[FileRegistrySession]:
        self._require_root()
        try:
            ensure_dir(self.graphs_dir, self.syncer)
        except OSError as exc:
            raise _unavailable("cannot create the graphs directory", exc) from exc
        with acquire(self.data_root / ".lock", timeout_s=timeout_s):
            session = FileRegistrySession(self)
            try:
                yield session
            finally:
                session.open = False

    @contextmanager
    def maintenance_session(
        self, graph: GraphId, *, timeout_s: float
    ) -> Iterator[FileMaintenanceSession]:
        store = self.store(graph)
        store.read_state()  # backend.unavailable for a missing graph, before locking
        with acquire(store.layout.lock, timeout_s=timeout_s):
            session = FileMaintenanceSession(store)
            try:
                yield session
            finally:
                session.close()


class FileRegistrySession:
    """Holds ``<root>/.lock``: graph creation and the name registry (W.a, W.f)."""

    def __init__(self, maintenance: FileStoreMaintenance) -> None:
        self._maintenance = maintenance
        self.open = True
        self._used: set[object] = set()

    def _check_open(self) -> None:
        if not self.open:
            raise GraphCliError("internal.error", "the registry session is closed")

    def manifests(self) -> Sequence[ManifestData]:
        self._check_open()
        maintenance = self._maintenance
        found = []
        for graph_id in maintenance.graph_ids():
            found.append(read_manifest(GraphLayout(maintenance.graphs_dir / str(graph_id)))[0])
        return found

    def read_installation_policy(self) -> bytes | None:
        """The installed installation policy as stored, read under the registry lock."""
        self._check_open()
        return read_policy_file(installation_policy_path(self._maintenance.data_root))

    def read_tokens(self) -> bytes | None:
        """The token file as stored, read under the registry lock (B14.11)."""
        self._check_open()
        return read_tokens_file(tokens_path(self._maintenance.data_root))

    def replace_tokens(self, document: Mapping[str, JsonValue], receipt: AuditReceipt) -> None:
        """Replace the token file; ``receipt`` is its installation event (B14.11)."""
        self._check_open()
        self._use(receipt, "tokens")
        write_tokens_file(
            tokens_path(self._maintenance.data_root), document, self._maintenance.syncer
        )

    def read_enrollments(self) -> bytes | None:
        """The enrollment file as stored, read under the registry lock (B14.21)."""
        self._check_open()
        return read_tokens_file(enrollments_path(self._maintenance.data_root))

    def replace_enrollments(self, document: Mapping[str, JsonValue], receipt: AuditReceipt) -> None:
        """Replace the enrollment file; ``receipt`` is its installation event (B14.21). An
        exchange changes both token files after one event."""
        self._check_open()
        self._use(receipt, "enrollments")
        write_tokens_file(
            enrollments_path(self._maintenance.data_root), document, self._maintenance.syncer
        )

    def _use(self, receipt: AuditReceipt, file: str) -> None:
        """Each file change needs an installation event of its own."""
        if receipt.stream != INSTALLATION_STREAM or (file, receipt.event_id) in self._used:
            raise GraphCliError(
                "internal.error",
                f"a {file} change needs its own installation event",
                details={"rule": "receipt_stream"},
            )
        self._used.add((file, receipt.event_id))

    def replace_installation_policy(
        self, document: Mapping[str, JsonValue], receipt: AuditReceipt
    ) -> None:
        """Install the installation policy; ``receipt`` is its installation event (B5.15)."""
        self._check_open()
        if receipt.stream != INSTALLATION_STREAM or receipt.event_id in self._used:
            raise GraphCliError(
                "internal.error",
                "a policy change needs its own installation event",
                details={"rule": "receipt_stream"},
            )
        self._used.add(receipt.event_id)
        maintenance = self._maintenance
        write_policy_file(
            installation_policy_path(maintenance.data_root),
            document,
            maintenance.syncer,
            maintenance.faults,
        )

    def create_graph(
        self,
        manifest: ManifestData,
        genesis: AuditEventDraft,
        *,
        version: str,
        files: Mapping[str, bytes],
        announce: Callable[[AuditReceipt], AuditReceipt],
    ) -> AuditReceipt:
        self._check_open()
        maintenance = self._maintenance
        graph_id = manifest.entry.id
        stream = graph_stream(str(graph_id))
        body, event_id = audit_body(genesis, stream)
        _check_release(version, files)
        target = maintenance.graphs_dir / str(graph_id)
        if target.exists():
            raise GraphCliError("internal.error", f"graph {graph_id} already exists")
        staging = maintenance.graphs_dir / f"{STAGING_PREFIX}{graph_id}"
        syncer = maintenance.syncer
        document, line = seal(body, GENESIS)
        try:
            if staging.exists():
                shutil.rmtree(staging)  # a leftover of an interrupted create with this id
            layout = init_graph_dir(staging, syncer)
            temp = write_temp(layout.manifest, format_manifest(manifest), syncer)
            os.replace(temp, layout.manifest)
            log = SegmentedLog(layout.audit, AUDIT_SEGMENT_PREFIX, maintenance.segment_bytes)
            pending = log.append(line, 1, None)
            try:
                pending.sync(syncer)
            finally:
                pending.close()
            write_tree(layout.release_dir(version), files, syncer)
            syncer.sync_dir(layout.ontology)
            syncer.sync_dir(staging)
            syncer.sync_dir(maintenance.graphs_dir)
            fire(maintenance.faults, CrashPoint.CREATE_BEFORE_ANNOUNCE)
        except OSError as exc:
            raise _unavailable("cannot stage the new graph", exc) from exc
        link = link_of(document)
        receipt = AuditReceipt(stream, event_id, link.seq, link.hash, object())
        announce(receipt)
        try:
            fire(maintenance.faults, CrashPoint.CREATE_BEFORE_RENAME)
            os.rename(staging, target)
            syncer.sync_dir(maintenance.graphs_dir)
        except OSError as exc:
            raise _unavailable("cannot publish the new graph", exc) from exc
        return receipt


class FileMaintenanceSession:
    """Lock held; no recovery on open. Write methods recover lazily."""

    def __init__(self, store: FileStore) -> None:
        self._store = store
        self._write: FileSession | None = None
        self._open = True

    def close(self) -> None:
        if self._write is not None:
            self._write.close()
        self._open = False

    def _writer(self) -> FileSession:
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")
        if self._write is None:
            self._write = FileSession(self._store)
        return self._write

    # ---- WriteSession --------------------------------------------------

    def snapshot(self) -> Snapshot:
        return self._writer().snapshot()

    def head(self) -> Head:
        return self._writer().head()

    def alias_counters(self) -> Mapping[str, int]:
        return self._writer().alias_counters()

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None:
        return self._writer().lookup_idempotency(key, principal_id)

    def append_audit(self, event: AuditEventDraft) -> AuditReceipt:
        return self._writer().append_audit(event)

    def commit(self, tx: AuthorizedTx, receipt: AuditReceipt) -> CommitResult:
        return self._writer().commit(tx, receipt)

    def migrate(self, tx: AuthorizedTx, receipt: AuditReceipt) -> CommitResult:
        return self._writer().migrate(tx, receipt)

    def publish_blob(self, staged: StagedBlob) -> BlobInfo:
        return self._writer().publish_blob(staged)

    # ---- migration marker ------------------------------------------------

    def migration_marker(self) -> MigrationMarker | None:
        self._check_open()
        return self._store.migration_marker()

    def write_migration_marker(self, marker: MigrationMarker) -> None:
        """Replace ``data/migration.json`` by one rename (the commit point)."""
        self._check_open()
        store = self._store
        layout = store.layout
        try:
            temp = write_temp(layout.migration_marker, pretty_bytes(marker.to_json()), store.syncer)
            fire(store.faults, CrashPoint.MIGRATION_MARKER_BEFORE_RENAME)
            os.replace(temp, layout.migration_marker)
            store.syncer.sync_dir(layout.data)
        except OSError as exc:
            raise _unavailable("cannot write the migration marker", exc) from exc

    def remove_migration_marker(self) -> None:
        """Remove ``data/migration.json`` (the commit point is the unlink); idempotent."""
        self._check_open()
        store = self._store
        layout = store.layout
        try:
            fire(store.faults, CrashPoint.MIGRATION_MARKER_BEFORE_REMOVE)
            layout.migration_marker.unlink(missing_ok=True)
            store.syncer.sync_dir(layout.data)
        except OSError as exc:
            raise _unavailable("cannot remove the migration marker", exc) from exc

    # ---- manifest and releases ------------------------------------------

    def _check_open(self) -> None:
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")

    def manifest(self) -> ManifestData:
        self._check_open()
        return read_manifest(self._store.layout)[0]

    def release_hashes(self) -> Mapping[str, str]:
        self._check_open()
        return release_hashes(self._store.layout)

    def read_release_file(self, version: str, name: str) -> bytes:
        self._check_open()
        return read_release_file(self._store.layout, version, name)

    def replace_manifest(self, manifest: ManifestData, receipt: AuditReceipt) -> None:
        store = self._store
        if manifest.entry.id != store.graph_id:
            raise GraphCliError("internal.error", "the manifest is for another graph")
        self._writer().claim_receipt(receipt)
        layout = store.layout
        try:
            temp = write_temp(layout.manifest, format_manifest(manifest), store.syncer)
            fire(store.faults, CrashPoint.MANIFEST_BEFORE_RENAME)
            os.replace(temp, layout.manifest)
            store.syncer.sync_dir(layout.root)
        except OSError as exc:
            raise _unavailable("cannot replace the manifest", exc) from exc
        if self._write is not None:  # the blob location may have changed
            self._write.rebind_blobs()

    def install_release(
        self, version: str, files: Mapping[str, bytes], receipt: AuditReceipt
    ) -> None:
        store = self._store
        _check_release(version, files)
        layout = store.layout
        target = layout.release_dir(version)
        if target.exists():
            raise GraphCliError("internal.error", f"release {version} is already installed")
        self._writer().claim_receipt(receipt)
        staging = layout.ontology / f"{STAGING_PREFIX}{version}"
        try:
            ensure_dir(layout.ontology, store.syncer)
            if staging.exists():
                shutil.rmtree(staging)
            write_tree(staging, files, store.syncer)
            store.syncer.sync_dir(layout.ontology)
            fire(store.faults, CrashPoint.RELEASE_BEFORE_RENAME)
            os.rename(staging, target)
            store.syncer.sync_dir(layout.ontology)
        except OSError as exc:
            raise _unavailable("cannot install the release", exc) from exc

    def read_skill(self) -> SkillSlot | None:
        self._check_open()
        return read_skill(self._store.layout)

    def replace_skill(self, slot: SkillSlot, receipt: AuditReceipt) -> None:
        """Publish ``skill/`` (D15 Q2): write the tree once under its hash, then switch
        ``source.json`` with one rename (the commit point); unreferenced trees go last."""
        store = self._store
        bad = sorted(name for name in slot.files if not is_relative_name(name))
        if bad or not slot.files or skill_hash(slot.files) != slot.hash:
            raise GraphCliError("internal.error", f"bad skill slot: {bad or 'hash or files'}")
        self._writer().claim_receipt(receipt)
        layout = store.layout
        tree = layout.skill_files(slot.hash)
        try:
            ensure_dir(tree.parent, store.syncer)
            if not tree.is_dir():
                staging = tree.parent / f"{STAGING_PREFIX}{tree.name}"
                if staging.exists():
                    shutil.rmtree(staging)
                write_tree(staging, slot.files, store.syncer)
                os.rename(staging, tree)
                store.syncer.sync_dir(tree.parent)
            temp = write_temp(layout.skill_source, pretty_bytes(slot.source_json()), store.syncer)
            fire(store.faults, CrashPoint.SKILL_BEFORE_RENAME)
            os.replace(temp, layout.skill_source)
            store.syncer.sync_dir(layout.skill)
            for name in sorted(os.listdir(tree.parent)):
                if name != tree.name:
                    shutil.rmtree(tree.parent / name)
        except OSError as exc:
            raise _unavailable("cannot publish the skill", exc) from exc

    def read_functions(self) -> KineticSlot | None:
        self._check_open()
        return read_slot(self._store.layout, "functions", sources=True)

    def replace_functions(self, slot: KineticSlot, receipt: AuditReceipt) -> None:
        self._replace_kinetic("functions", slot, receipt)

    def read_actions(self) -> KineticSlot | None:
        self._check_open()
        return read_slot(self._store.layout, "actions", sources=True)

    def replace_actions(self, slot: KineticSlot, receipt: AuditReceipt) -> None:
        self._replace_kinetic("actions", slot, receipt)

    def _replace_kinetic(self, kind: SlotKind, slot: KineticSlot, receipt: AuditReceipt) -> None:
        """Publish ``functions/`` (K6) or ``actions/`` (A8): write the sources tree and the
        compiled catalog once under their hashes, then switch ``slot.json`` with one rename
        (the commit point); unreferenced trees and compiled files go last."""
        store = self._store
        bad = sorted(name for name in slot.files if not _is_source_name(name))
        if (
            slot.kind != kind
            or bad
            or sources_hash(slot.files) != slot.hash
            or hash_value(dict(slot.compiled)) != slot.compiled_hash
        ):
            raise GraphCliError("internal.error", f"bad {kind} slot: {bad or 'hash or files'}")
        self._writer().claim_receipt(receipt)
        layout = store.layout
        tree = layout.kinetic_files(kind, slot.hash)
        compiled = layout.kinetic_compiled(kind, slot.compiled_hash)
        try:
            ensure_dir(tree.parent, store.syncer)
            ensure_dir(compiled.parent, store.syncer)
            if not tree.is_dir():
                staging = tree.parent / f"{STAGING_PREFIX}{tree.name}"
                if staging.exists():
                    shutil.rmtree(staging)
                write_tree(staging, slot.files, store.syncer)
                os.rename(staging, tree)
                store.syncer.sync_dir(tree.parent)
            if not compiled.is_file():
                temp = write_temp(compiled, pretty_bytes(slot.compiled), store.syncer)
                os.replace(temp, compiled)
                store.syncer.sync_dir(compiled.parent)
            temp = write_temp(
                layout.kinetic_slot(kind), pretty_bytes(slot.slot_json()), store.syncer
            )
            fire(store.faults, _BEFORE_RENAME[kind])
            os.replace(temp, layout.kinetic_slot(kind))
            store.syncer.sync_dir(layout.kinetic(kind))
            for keep, directory in ((tree, tree.parent), (compiled, compiled.parent)):
                for name in sorted(os.listdir(directory)):
                    if name != keep.name:
                        _remove(directory / name)
        except OSError as exc:
            raise _unavailable(f"cannot publish the {kind}", exc) from exc

    def read_policy(self) -> bytes | None:
        """The graph's installed policy as stored (B6.14)."""
        self._check_open()
        return read_policy_file(self._store.layout.policy)

    def replace_policy(self, document: Mapping[str, JsonValue], receipt: AuditReceipt) -> None:
        """Install the graph's policy; ``receipt`` is this session's intent (W.e, B5.15)."""
        self._writer().claim_receipt(receipt)
        store = self._store
        write_policy_file(store.layout.policy, document, store.syncer, store.faults)

    # ---- maintenance ---------------------------------------------------

    def recover(self) -> RecoveryReport:
        """Section 4.2 now; ``backend.corrupt`` on interior damage."""
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")
        self._write = None
        return recover(self._store, oplog=self._store.oplog).report

    def verify(self) -> Sequence[Finding]:
        """Full-history check of the tx log, revision files and ``head.json``."""
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")
        return sort_findings(Verifier(self._store).run())

    # ---- blobs ---------------------------------------------------------

    def _referenced(self) -> frozenset[str] | None:
        """Blob hashes named by any tx; ``None`` when the tx log cannot be read whole."""
        self._check_open()
        view, damage, _ = replay(self._store)
        if damage is not None:
            return None
        return frozenset(h for tx in view.at().txs_since(0, limit=sys.maxsize) for h in tx.blobs)

    def blobs(self) -> Iterator[BlobInfo]:
        self._check_open()
        return self._store.blob_backend().listing()

    def orphan_blobs(self) -> Sequence[BlobInfo]:
        referenced = self._referenced()
        if referenced is None:
            raise GraphCliError("backend.corrupt", "the tx log is damaged; gc needs repair")
        return self._store.blob_backend().collectable(referenced)

    def staging(self) -> Iterator[tuple[str, datetime]]:
        self._check_open()
        return self._store.blob_backend().staging()

    def verify_blobs(
        self, *, rehash: bool, recorded_store: RecordedStore | None = None
    ) -> BlobVerification:
        """Missing, damaged, orphan, staged and misplaced files; no orphans beyond tx-log
        damage. An object store that cannot be reached, or carries another store marker,
        is one ``blob_store_unavailable`` finding in place of everything else it would be
        asked (staging leftovers are still listed); misplaced copies are judged against
        the location's one listing (and, with ``rehash``, its damaged files). A local
        graph's copies in an object store that is not the one it was recorded to leave are
        reported as not its own."""
        referenced = self._referenced()
        layout = self._store.layout
        backend = self._store.blob_backend()
        try:
            checked = backend.verify(referenced, rehash=rehash)
        except GraphCliError as exc:
            finding = backend.unavailable(exc)
            if finding is None:
                raise
            findings = [finding, *blobs.staging_findings(layout)]
            return BlobVerification(tuple(sort_findings(findings)), None)
        held = {blob.hash: blob.size for blob in checked.stored or ()}
        found = _copies(self._store, recorded_store)
        misplaced: list[Finding] = []
        if found is not None:
            other, copies = found.other, found.copies
            where = _LOCATION_NAMES[backend.location]
            damaged = {f.expected for f in checked.findings if f.code == BLOB_HASH_MISMATCH}
            for copy in copies:
                if not found.owned:
                    message = (
                        f"a copy outside the graph's location ({where}), in an object store"
                        " that is not this graph's recorded store (no switch from it to the"
                        " data root is recorded, or the bucket's marker differs); files gc"
                        " keeps the copy"
                    )
                elif held.get(copy.hash) == copy.size and copy.hash not in damaged:
                    message = (
                        f"a copy outside the graph's location ({where}), which holds this"
                        " hash with the same size; files gc removes the copy once the bytes"
                        " there match their hash"
                    )
                else:
                    message = (
                        f"a copy outside the graph's location ({where}), which lacks this"
                        " file, holds another size or damaged bytes; files gc keeps the copy"
                    )
                target = {"path": other.target(copy.hash)}
                misplaced.append(Finding(Severity.INFO, BLOB_MISPLACED, target, message))
        findings = sort_findings([*checked.findings, *misplaced])
        return BlobVerification(tuple(findings), checked.stored)

    def remove_blobs(
        self,
        hashes: Sequence[str],
        staging: Sequence[str],
        receipt: AuditReceipt,
        *,
        misplaced: MisplacedCopies | None = None,
    ) -> None:
        """Refuses a referenced hash; refuses everything when the tx log is damaged.
        The removable misplaced copies (hash-confirmed before the lock,
        ``FileStoreMaintenance.misplaced_blobs``) go first, each only while one listing of
        the graph's location, made right before, still shows its hash with the same size
        (so an orphan this gc removes cannot be the confirmation), and an object store's
        objects only while its marker is still ``misplaced.store_id``. Nothing is read in
        full under the lock."""
        referenced = self._referenced()
        if referenced is None:
            raise GraphCliError("backend.corrupt", "the tx log is damaged; gc needs repair")
        writer = self._writer()
        writer.claim_receipt(receipt)
        backend = writer.blob_backend()
        if misplaced is not None and misplaced.removable:
            other = self._store.misplaced_backend()
            if other is None or other.location != misplaced.location:
                raise GraphCliError(
                    "internal.error",
                    "the graph's blob location changed since its copies were judged",
                )
            if other.location == "local" or (
                misplaced.store_id is not None and other.store_id() == misplaced.store_id
            ):
                held = {blob.hash: blob.size for blob in backend.listing()}
                present = {b.hash for b in misplaced.removable if held.get(b.hash) == b.size}
                other.remove_misplaced(sorted(present))
        backend.remove(hashes, staging, referenced=referenced)

    def copy_blobs(
        self, *, to: str, store_id: str | None, stop: CopyBudget | None = None
    ) -> BlobCopies:
        referenced = self._referenced()
        if referenced is None:
            raise GraphCliError(
                "backend.corrupt", "the tx log is damaged; a move needs it whole (repair first)"
            )
        store = self._store
        source, target = _move_backends(store, to, store_id, dry_run=False)
        assert target is not None
        return copy_pass(source, target, referenced, stop=stop, dry_run=False, faults=store.faults)

    def reindex(self, receipt: AuditReceipt | None = None) -> None:
        """Rebuild ``data/entities/``, ``head.json`` and the index from the tx log (B4.1).

        The index and ``head.json`` go first, so an interrupted reindex leaves a
        state that readers scan and the next writer rolls forward.
        """
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")
        if receipt is not None:
            self._writer().claim_receipt(receipt)
        store = self._store
        state = store.read_state()
        if state.scan.corruption is not None:
            raise GraphCliError("backend.corrupt", "the tx log is damaged; reindex needs repair")
        layout = store.layout
        try:
            if layout.index.parent.exists():
                shutil.rmtree(layout.index.parent)
            layout.head.unlink(missing_ok=True)
            store.syncer.sync_dir(layout.data)
            if layout.entities.exists():
                shutil.rmtree(layout.entities)
                store.syncer.sync_dir(layout.data)
        except OSError as exc:
            raise _unavailable("reindex failed", exc) from exc
        self.recover()
        try:
            store.rebuild_index(state.end.seq)
        except INDEX_ERRORS as exc:
            raise GraphCliError("backend.unavailable", f"the index rebuild failed: {exc}") from exc


def _move_backends(
    store: FileStore, to: str, store_id: str | None, *, dry_run: bool
) -> tuple[BlobBackend, BlobBackend | None]:
    """The location the graph's manifest names and the location ``to`` of a move; the
    target is ``None`` for a dry run to an object store without a marker yet."""
    manifest = blob_manifest(store.layout)
    if manifest is None:
        raise GraphCliError(
            "config.invalid",
            "the graph's manifest cannot be read; its stored files are not moved",
            details={"graph_id": str(store.graph_id)},
        )
    if to not in _LOCATION_NAMES or to == manifest.store.blob_location:
        raise GraphCliError("internal.error", f"cannot move the graph's stored files to {to!r}")
    source = store.blob_backend()
    if to == "s3" and store_id is None:
        if not dry_run:
            raise GraphCliError("internal.error", "a move to an object store needs its id")
        return source, None
    location: BlobLocation = "s3" if to == "s3" else "local"
    target = store.blob_backends.at(
        store.layout, manifest, location, store_id, syncer=store.syncer, faults=store.faults
    )
    return source, target


@dataclass(frozen=True, slots=True)
class _Copies:
    other: BlobBackend
    copies: list[BlobInfo]
    owned: bool  # the graph may clean ``other``
    store_id: str | None  # ``other``'s store when it is an object store the graph owns


def _copies(store: FileStore, recorded_store: RecordedStore | None) -> _Copies | None:
    """The other location, the copies of blobs it holds (``.tmp-`` staging entries are not
    blobs) and whether the graph may clean it: always the data root; a local graph's
    object store only when its marker is the store the graph's audit trail records it
    left (else report only: a cloned or restored installation may share the bucket and
    the graph id). A local graph's object store is optional: when
    it cannot be reached or is no graph_cli store, there are no copies to judge."""
    other = store.misplaced_backend()
    if other is None:
        return None
    try:
        copies = list(other.listing())
        if other.location == "local":
            return _Copies(other, copies, True, None)
        recorded = recorded_store() if recorded_store is not None and copies else None
        owned = recorded is not None and other.store_id() == recorded
    except GraphCliError as exc:
        if other.unavailable(exc) is None:
            raise
        return None
    return _Copies(other, copies, owned, recorded if owned else None)


def misplaced_copies(
    store: FileStore, recorded_store: RecordedStore | None
) -> MisplacedCopies | None:
    """``FileStoreMaintenance.misplaced_blobs`` for one store, without its lock: a copy is
    removable when the graph may clean its location and the named location lists the
    same hash and size and its bytes, read in full (a verified streamed GET in an object
    store, a rehash locally), have that hash. A file that is damaged, unreadable or absent
    there confirms nothing."""
    found = _copies(store, recorded_store)
    if found is None:
        return None
    copies = found.copies
    confirmed: set[str] = set()
    if found.owned and copies:
        named = store.blob_backend()
        held = {blob.hash: blob.size for blob in named.listing()}
        wanted = sorted({copy.hash for copy in copies if held.get(copy.hash) == copy.size})
        confirmed = {digest for digest in wanted if named.hash_of(digest) == digest}
    return MisplacedCopies(
        found.other.location,
        tuple(copy for copy in copies if copy.hash in confirmed),
        tuple(copy for copy in copies if copy.hash not in confirmed),
        found.store_id,
    )


class Verifier:
    """Produces ``integrity.storage.*`` findings; never raises for damage it can describe."""

    def __init__(self, store: FileStore) -> None:
        self.store = store
        self.findings: list[Finding] = []

    def add(
        self,
        code: str,
        target: Mapping[str, JsonValue],
        message: str,
        *,
        expected: JsonValue = None,
        actual: JsonValue = None,
        severity: Severity = Severity.ERROR,
    ) -> None:
        self.findings.append(Finding(severity, code, target, message, expected, actual))

    def run(self) -> list[Finding]:
        view, spans = self._tx_log()
        complete = not self.findings
        checkpoint = self._checkpoint()
        materialized_seq = checkpoint.seq if checkpoint is not None else 0
        self._revisions(view, materialized_seq, complete=complete)
        if complete:  # beyond damage nothing can be judged; no cascades
            self._head(view, checkpoint)
            self._index(view, spans)
        self._blobs(view)
        self._pin()
        return self.findings

    def _blobs(self, view: GraphView) -> None:
        """Every blob a verified tx names is stored (the full check is ``verify_blobs``);
        an object store answers from one listing. A store that cannot be reached is a
        ``blob_store_unavailable`` finding. With an unreadable manifest the graph's blob
        location is unknown: ``pin_mismatch`` reports the manifest, and no blob is judged
        against a location that may not be the graph's."""
        layout = self.store.layout
        if layout.manifest.exists() and blob_manifest(layout) is None:
            return
        backend = self.store.blob_backend()
        referenced = {h for tx in view.at().txs_since(0, limit=sys.maxsize) for h in tx.blobs}
        try:
            absent = backend.absent(referenced)
        except OSError as exc:
            raise _unavailable("cannot read the stored files", exc) from exc
        except GraphCliError as exc:
            finding = backend.unavailable(exc)
            if finding is None:
                raise
            self.findings.append(finding)
            return
        for digest in absent:
            target = {"path": backend.target(digest)}
            self.add(BLOB_MISSING, target, "a referenced file is missing")

    def _pin(self) -> None:
        """The manifest pins a release whose ``compiled.json`` has the pinned hash (B7.6)."""
        layout = self.store.layout
        try:
            pin = read_manifest(layout)[0].entry.pin
        except GraphCliError as exc:
            self.add(PIN_MISMATCH, {"path": "manifest.toml"}, f"the pin cannot be read: {exc}")
            return
        path = f"ontology/{pin.version}/{COMPILED_FILE}"
        actual = release_hashes(layout).get(pin.version)
        if actual is None:
            self.add(PIN_MISMATCH, {"path": path}, "the pinned release is missing or unreadable")
        elif actual != pin.artifact_hash:
            self.add(
                PIN_MISMATCH,
                {"path": path},
                "the pinned release differs from the manifest's artifact hash",
                expected=pin.artifact_hash,
                actual=actual,
            )

    def _index(self, view: GraphView, spans: Sequence[TxSpan]) -> None:
        """The index, if present, holds exactly the state at its seq (disposable, B4.1)."""
        layout = self.store.layout
        difference = index_difference(
            layout.index, self.store.graph_id, view, spans, self.store.txlog
        )
        if difference is not None:
            target = {"path": str(layout.index.relative_to(layout.root))}
            self.add(INDEX_INCONSISTENT, target, difference)

    def _tx_log(self) -> tuple[GraphView, list[TxSpan]]:
        """Replay the whole log; damage ends the replay with one finding."""
        view, damage, spans = replay(self.store)
        if damage is not None:
            self.add(
                TX_CHAIN_BROKEN,
                {"path": damage.path},
                damage.message,
                expected=damage.expected,
                actual=damage.actual,
            )
        return view, spans

    def _checkpoint(self) -> HeadFile | None:
        try:
            data = self.store.layout.head.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return parse_head_file(data)
        except (ValueError, GraphCliError) as exc:
            self.add(HEAD_INCONSISTENT, {"path": "data/head.json"}, f"unreadable: {exc}")
            return None

    def _revisions(self, view: GraphView, materialized_seq: int, *, complete: bool) -> None:
        layout = self.store.layout
        at = view.at()
        known: set[tuple[EntityId, int]] = set()
        for entity in at.entity_ids():
            for meta in at.history(entity):
                revision = at.revision(entity, meta.rev)
                assert revision is not None
                known.add((entity, meta.rev))
                target: dict[str, JsonValue] = {"id": str(entity), "rev": meta.rev}
                if revision_content_hash(revision) != revision.content_hash:
                    self.add(REVISION_HASH_MISMATCH, target, "content_hash in the tx log is wrong")
                path = layout.revision_path(entity, meta.rev)
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    if revision.tx_seq <= materialized_seq:
                        self.add(REVISION_MISSING, target, "revision file is missing")
                    continue
                if data != pretty_bytes(revision_to_json(revision)):
                    self.add(
                        REVISION_HASH_MISMATCH, target, "revision file differs from the tx log"
                    )
        if not complete or not layout.entities.is_dir():
            return
        for path in sorted(layout.entities.glob("*/*/*")):
            match = REVISION_FILE_RE.match(path.name)
            if match is None:
                continue
            owner = _entity_of(path.parent.name)
            if owner is None or (owner, int(match.group(1))) not in known:
                self.add(
                    REVISION_UNEXPECTED,
                    {"path": str(path.relative_to(layout.root))},
                    "revision file has no copy in the tx log",
                )

    def _head(self, view: GraphView, checkpoint: HeadFile | None) -> None:
        if checkpoint is None:
            return
        target: dict[str, JsonValue] = {"path": "data/head.json"}
        if checkpoint.seq > view.seq:
            self.add(
                HEAD_INCONSISTENT,
                target,
                "head.json is ahead of the tx log",
                expected=view.seq,
                actual=checkpoint.seq,
            )
            return
        if checkpoint.seq == 0:
            self.add(HEAD_INCONSISTENT, target, "head.json names seq 0")
            return
        tx = view.at().tx(checkpoint.seq)
        if (tx.tx_id, tx.hash, tx.recorded_at) != (
            checkpoint.tx_id,
            checkpoint.hash,
            checkpoint.recorded_at,
        ):
            self.add(HEAD_INCONSISTENT, target, "head.json does not match its tx")
            return
        counters = view.at(checkpoint.seq).alias_counters()
        if dict(checkpoint.alias_counters) != counters:
            self.add(
                HEAD_INCONSISTENT,
                target,
                "alias counters differ from the tx log",
                expected=dict(counters),
                actual=dict(checkpoint.alias_counters),
            )
        resumed = self.store.txlog.scan(checkpoint.link, at=checkpoint.txlog)
        if resumed.corruption is not None:
            self.add(HEAD_INCONSISTENT, target, "head.json points into the wrong place")
        audit = self.store.auditlog.scan()
        if audit.corruption is not None:
            return  # a damaged audit stream is the audit verifier's finding (no cascades)
        audit_hashes = {
            record.document.get("seq"): record.document.get("hash") for record in audit.records
        }
        if checkpoint.audit_link.seq and (
            audit_hashes.get(checkpoint.audit_link.seq) != checkpoint.audit_link.hash
        ):
            self.add(HEAD_INCONSISTENT, target, "head.json audit position does not match")


@dataclass(frozen=True, slots=True)
class ReplayDamage:
    """Where a full replay of the tx log stopped, as a ``tx_chain_broken`` finding."""

    path: str
    message: str
    expected: JsonValue
    actual: JsonValue


def replay(store: FileStore) -> tuple[GraphView, ReplayDamage | None, list[TxSpan]]:
    """The whole tx log as a view; the damage that ended the replay, if any; tx positions."""
    result = store.txlog.scan()
    view = GraphView()
    spans: list[TxSpan] = []
    for record in result.records:
        try:
            view.apply(parse_tx(record.document))
        except (GraphCliError, JsonShapeError) as exc:
            path = f"data/txlog/{record.start.segment}"
            message = f"tx line has a bad shape: {exc}"
            return view, ReplayDamage(path, message, None, {"offset": record.start.offset}), spans
        spans.append(TxSpan(record.start, record.end))
    if result.corruption is not None:
        damage = result.corruption
        return (
            view,
            ReplayDamage(
                f"data/txlog/{damage.segment}",
                f"the tx log is damaged: {damage.reason}",
                {"seq": damage.expected_seq},
                {"offset": damage.offset},
            ),
            spans,
        )
    return view, None, spans


_BEFORE_RENAME: Final = {
    "functions": CrashPoint.FUNCTIONS_BEFORE_RENAME,
    "actions": CrashPoint.ACTIONS_BEFORE_RENAME,
}


def _is_source_name(name: str) -> bool:
    """``<function name>.yaml``: a flat file name, the sources have no directories."""
    return name.endswith(".yaml") and "/" not in name and is_relative_name(name)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _check_release(version: str, files: Mapping[str, bytes]) -> None:
    if not VERSION_RE.match(version):
        raise GraphCliError(
            "internal.error", f"release version {version!r} is not MAJOR.MINOR.PATCH"
        )
    bad = sorted(name for name in files if not is_relative_name(name))
    if bad or COMPILED_FILE not in files:
        raise GraphCliError(
            "internal.error", f"bad release file names: {bad or 'no compiled.json'}"
        )


def _is_graph_dir_name(name: str) -> bool:
    try:
        return format_id(parse_id(name)) == name
    except GraphCliError:
        return False


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")


def _entity_of(name: str) -> EntityId | None:
    try:
        return EntityId(parse_id(name))
    except GraphCliError:
        return None


if TYPE_CHECKING:  # static proof that the adapter implements the maintenance ports

    def _conforms(
        maintenance: FileStoreMaintenance,
        registry: FileRegistrySession,
        session: FileMaintenanceSession,
    ) -> tuple[GraphStoreMaintenance, RegistrySession, MaintenanceSession]:
        return maintenance, registry, session
