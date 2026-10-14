"""FileStore maintenance port (contracts B13.1, section 4.2; plan D10).

Imported only by graph_cli_admin (import-linter contract). A maintenance
session holds the graph lock but does not recover on open, so ``verify``
sees the files as they are. The ``WriteSession`` methods recover first, like
an application session. ``create_graph``, ``replace_manifest`` and
``install_release`` follow with the graph-admin flows (P1 item 5); until
then the classes do not claim the full ``GraphStoreMaintenance`` protocol.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import JsonValue, pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EntityId, GraphId, parse_id
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt
from graph_cli.kernel.ports.storage import CommitResult, Head, RecoveryReport, Snapshot
from graph_cli.kernel.records import TxRecord, revision_content_hash, revision_to_json
from graph_cli.storage.common.faults import FaultHook
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.files.durable import Durability, OsSyncer, Syncer
from graph_cli.storage.files.headfile import HeadFile, parse_head_file
from graph_cli.storage.files.layout import DEFAULT_SEGMENT_BYTES, REVISION_FILE_RE
from graph_cli.storage.files.locking import acquire
from graph_cli.storage.files.recovery import recover
from graph_cli.storage.files.segments import OpLog
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


def _entity_of(name: str) -> EntityId | None:
    try:
        return EntityId(parse_id(name))
    except GraphCliError:
        return None
