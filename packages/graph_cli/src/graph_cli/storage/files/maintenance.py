"""FileStore maintenance port (contracts B13.1, sections 4.2, 5, W.e, W.f).

Imported only by graph_cli_admin (import-linter contract). A maintenance
session holds the graph lock but does not recover on open, so ``verify``
sees the files as they are. The ``WriteSession`` methods recover first, like
an application session.

Maintenance effects are published by one rename each, which is their commit
point: ``graphs/.tmp-<id>`` -> ``graphs/<id>`` (graph create, under the
registry lock ``<root>/.lock``), ``ontology/.tmp-<version>`` ->
``ontology/<version>`` and ``manifest.toml.tmp`` -> ``manifest.toml``
(under the graph lock, after the intent event). A failure before the rename
leaves the staging state behind, as a kill -9 would; readers ignore
``.tmp-*`` entries.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import (
    CanonicalError,
    JsonValue,
    hash_value,
    parse_json,
    pretty_bytes,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EntityId, GraphId, format_id, parse_id
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt, graph_stream
from graph_cli.kernel.ports.catalog import ManifestData
from graph_cli.kernel.ports.storage import (
    CommitResult,
    GraphStoreMaintenance,
    Head,
    MaintenanceSession,
    RecoveryReport,
    RegistrySession,
    Snapshot,
)
from graph_cli.kernel.records import TxRecord, revision_content_hash, revision_to_json
from graph_cli.storage.common.audit import audit_body
from graph_cli.storage.common.chain import GENESIS, link_of, seal
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files.durable import (
    Durability,
    OsSyncer,
    Syncer,
    ensure_dir,
    write_temp,
    write_tree,
)
from graph_cli.storage.files.headfile import HeadFile, parse_head_file
from graph_cli.storage.files.layout import (
    AUDIT_SEGMENT_PREFIX,
    DEFAULT_SEGMENT_BYTES,
    REVISION_FILE_RE,
    STAGING_PREFIX,
    GraphLayout,
    init_graph_dir,
)
from graph_cli.storage.files.locking import acquire
from graph_cli.storage.files.manifest import format_manifest, parse_manifest
from graph_cli.storage.files.recovery import recover
from graph_cli.storage.files.segments import OpLog, SegmentedLog
from graph_cli.storage.files.store import FileSession, FileStore, parse_tx
from graph_cli.storage.finding_codes import (
    HEAD_INCONSISTENT,
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

    def _check_open(self) -> None:
        if not self.open:
            raise GraphCliError("internal.error", "the registry session is closed")

    def manifests(self) -> Sequence[ManifestData]:
        self._check_open()
        maintenance = self._maintenance
        found = []
        for graph_id in maintenance.graph_ids():
            found.append(read_manifest(GraphLayout(maintenance.graphs_dir / str(graph_id))))
        return found

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

    # ---- manifest and releases ------------------------------------------

    def _check_open(self) -> None:
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")

    def manifest(self) -> ManifestData:
        self._check_open()
        return read_manifest(self._store.layout)

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

    def reindex(self) -> None:
        """Rebuild ``data/entities/`` and ``head.json`` from the tx log (B4.1).

        ``head.json`` goes first, so an interrupted reindex leaves a state that
        readers scan and the next writer rolls forward.
        """
        if not self._open:
            raise GraphCliError("internal.error", "the maintenance session is closed")
        store = self._store
        state = store.read_state()
        if state.scan.corruption is not None:
            raise GraphCliError("backend.corrupt", "the tx log is damaged; reindex needs repair")
        layout = store.layout
        try:
            layout.head.unlink(missing_ok=True)
            store.syncer.sync_dir(layout.data)
            if layout.entities.exists():
                shutil.rmtree(layout.entities)
                store.syncer.sync_dir(layout.data)
        except OSError as exc:
            raise GraphCliError(
                "backend.unavailable", f"reindex failed: {exc.strerror or exc}"
            ) from exc
        self.recover()


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
        view = self._tx_log()
        complete = not self.findings
        checkpoint = self._checkpoint()
        materialized_seq = checkpoint.seq if checkpoint is not None else 0
        self._revisions(view, materialized_seq, complete=complete)
        if complete:  # beyond damage nothing can be judged; no cascades
            self._head(view, checkpoint)
        return self.findings

    def _tx_log(self) -> GraphView:
        """Replay the whole log; damage ends the replay with one finding."""
        result = self.store.txlog.scan()
        view = GraphView()
        for record in result.records:
            try:
                view.apply(parse_tx(record.document))
            except (GraphCliError, JsonShapeError) as exc:
                self.add(
                    TX_CHAIN_BROKEN,
                    {"path": f"data/txlog/{record.start.segment}"},
                    f"tx line has a bad shape: {exc}",
                    actual={"offset": record.start.offset},
                )
                return view
        if result.corruption is not None:
            damage = result.corruption
            self.add(
                TX_CHAIN_BROKEN,
                {"path": f"data/txlog/{damage.segment}"},
                f"the tx log is damaged: {damage.reason}",
                expected={"seq": damage.expected_seq},
                actual={"offset": damage.offset},
            )
        return view

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
        audit_hashes = {
            record.document.get("seq"): record.document.get("hash")
            for record in self.store.auditlog.scan().records
        }
        if checkpoint.audit_link.seq and (
            audit_hashes.get(checkpoint.audit_link.seq) != checkpoint.audit_link.hash
        ):
            self.add(HEAD_INCONSISTENT, target, "head.json audit position does not match")


_VERSION_RE: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMPILED_FILE: Final = "compiled.json"


def read_manifest(layout: GraphLayout) -> ManifestData:
    """The graph's manifest; warnings about unknown keys are the catalog's to report."""
    source = f"graphs/{layout.root.name}/manifest.toml"
    try:
        data = layout.manifest.read_bytes()
    except FileNotFoundError:
        raise GraphCliError(
            "config.invalid", f"{source} is missing", details={"file": source, "key": ""}
        ) from None
    except OSError as exc:
        raise _unavailable(f"cannot read {source}", exc) from exc
    return parse_manifest(data, source=source)[0]


def release_hashes(layout: GraphLayout) -> dict[str, str]:
    """Installed versions and the C.2 hash of their ``compiled.json`` (unreadable: left out)."""
    try:
        names = os.listdir(layout.ontology)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise _unavailable("cannot list releases", exc) from exc
    found = {}
    for name in sorted(names):
        if not _VERSION_RE.match(name):
            continue
        try:
            document = parse_json((layout.release_dir(name) / COMPILED_FILE).read_bytes())
        except (OSError, CanonicalError):
            continue
        found[name] = hash_value(document)
    return found


def read_release_file(layout: GraphLayout, version: str, name: str) -> bytes:
    if not _VERSION_RE.match(version) or not _is_relative_name(name):
        raise GraphCliError("internal.error", f"bad release file reference {version}/{name}")
    path = layout.release_dir(version).joinpath(*PurePosixPath(name).parts)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise GraphCliError(
            "ontology.artifact_missing",
            f"release {version} has no file {name}",
            details={"version": version, "file": name},
        ) from None
    except OSError as exc:
        raise _unavailable("cannot read a release file", exc) from exc


def _check_release(version: str, files: Mapping[str, bytes]) -> None:
    if not _VERSION_RE.match(version):
        raise GraphCliError(
            "internal.error", f"release version {version!r} is not MAJOR.MINOR.PATCH"
        )
    bad = sorted(name for name in files if not _is_relative_name(name))
    if bad or COMPILED_FILE not in files:
        raise GraphCliError(
            "internal.error", f"bad release file names: {bad or 'no compiled.json'}"
        )


def _is_relative_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (
        bool(parts)
        and not name.startswith("/")
        and "\\" not in name
        and all(part not in ("", ".", "..") for part in parts)
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
