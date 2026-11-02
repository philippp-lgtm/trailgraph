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
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditReceipt,
    graph_stream,
)
from graph_cli.kernel.ports.catalog import (
    KineticSlot,
    ManifestData,
    SkillSlot,
    SlotKind,
    skill_hash,
    sources_hash,
)
from graph_cli.kernel.ports.storage import (
    CommitResult,
    GraphStoreMaintenance,
    Head,
    MaintenanceSession,
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
from graph_cli.storage.files.segments import OpLog, SegmentedLog
from graph_cli.storage.files.skill_slot import read_skill
from graph_cli.storage.files.store import FileSession, FileStore, parse_tx
from graph_cli.storage.files.token_files import (
    enrollments_path,
    read_tokens_file,
    tokens_path,
    write_tokens_file,
)
from graph_cli.storage.finding_codes import (
    BLOB_MISSING,
    HEAD_INCONSISTENT,
    INDEX_INCONSISTENT,
    PIN_MISMATCH,
    REVISION_HASH_MISMATCH,
    REVISION_MISSING,
    REVISION_UNEXPECTED,
    TX_CHAIN_BROKEN,
)


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
    ) -> None:
        self.data_root = data_root
        self.syncer: Syncer = syncer if syncer is not None else OsSyncer(durability)
        self.faults = faults
        self.segment_bytes = segment_bytes
        self.oplog = oplog

    def store(self, graph: GraphId) -> FileStore:
        return FileStore(
            self.data_root / "graphs" / str(graph),
            graph,
            syncer=self.syncer,
            faults=self.faults,
            segment_bytes=self.segment_bytes,
            oplog=self.oplog,
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

    def publish_blob(self, staged: StagedBlob) -> BlobInfo:
        return self._writer().publish_blob(staged)

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
        return blobs.listing(self._store.layout)

    def orphan_blobs(self) -> Sequence[BlobInfo]:
        referenced = self._referenced()
        if referenced is None:
            raise GraphCliError("backend.corrupt", "the tx log is damaged; gc needs repair")
        return [b for b in blobs.listing(self._store.layout) if b.hash not in referenced]

    def staging(self) -> Iterator[tuple[str, datetime]]:
        self._check_open()
        return blobs.staging(self._store.layout)

    def verify_blobs(self, *, rehash: bool) -> Sequence[Finding]:
        """Missing, damaged, orphan and staged files; no orphans beyond tx-log damage."""
        referenced = self._referenced()
        return sort_findings(blobs.verify(self._store.layout, referenced, rehash_all=rehash))

    def remove_blobs(
        self, hashes: Sequence[str], staging: Sequence[str], receipt: AuditReceipt
    ) -> None:
        """Refuses a referenced hash; refuses everything when the tx log is damaged."""
        referenced = self._referenced()
        if referenced is None:
            raise GraphCliError("backend.corrupt", "the tx log is damaged; gc needs repair")
        self._writer().claim_receipt(receipt)
        store = self._store
        blobs.remove(store.layout, hashes, staging, referenced=referenced, syncer=store.syncer)

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
        """Every blob a verified tx names is stored (the full check is ``verify_blobs``)."""
        layout = self.store.layout
        referenced = {h for tx in view.at().txs_since(0, limit=sys.maxsize) for h in tx.blobs}
        try:
            absent = blobs.missing(layout, referenced)
        except OSError as exc:
            raise _unavailable("cannot read the stored files", exc) from exc
        for digest in absent:
            target = {"path": blobs.relative(layout, digest)}
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
