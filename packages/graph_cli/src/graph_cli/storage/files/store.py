"""FileStore: one graph in files (contracts B4, B13, sections 4 and 5). POSIX only.

Write path (one session holds the graph lock): audit intent append + sync,
tx append + sync (the commit point), revision files (create-only temp, sync,
rename, directory sync), then the ``head.json`` checkpoint. Failures after
the commit point do not fail the write (B3.5); the next writer rolls forward.

Read path (lock-free, B4.10, B13.7): the snapshot seq is the last complete,
hash-valid tx line. Point reads (``get``, ``get_revision``, ``history``)
use revision files with ``tx_seq <= S``, overlaid with the txs after the
checkpoint. Scans (``query``, ``incoming``, ``neighbors``, aliases,
suffixes) replay the tx log into a view that is cached per store instance
and extended incrementally (plan D5). Reads never write anything.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json, pretty_bytes
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TermRef
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt, graph_stream
from graph_cli.kernel.ports.storage import (
    CAPABILITY_SNAPSHOT_AT_SEQ,
    EMPTY_HEAD,
    CommitResult,
    GraphStoreRead,
    GraphStoreWrite,
    Head,
    Snapshot,
    WriteSession,
)
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.records import (
    EdgeRecord,
    Revision,
    RevisionMeta,
    TxRecord,
    revision_from_json,
    revision_meta,
    revision_to_json,
    tx_from_json,
)
from graph_cli.storage.common.audit import audit_body
from graph_cli.storage.common.chain import GENESIS, ChainLink, link_of, seal
from graph_cli.storage.common.commit import check_receipt, check_tx, seal_tx
from graph_cli.storage.common.evaluate import run_neighbors, run_query
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.snapshot import ViewSnapshot, resolve_at_seq
from graph_cli.storage.common.view import GraphView, ViewAt
from graph_cli.storage.files.durable import Durability, OsSyncer, Syncer, ensure_dir, write_temp
from graph_cli.storage.files.headfile import HeadFile, parse_head_file
from graph_cli.storage.files.layout import (
    AUDIT_SEGMENT_PREFIX,
    DEFAULT_SEGMENT_BYTES,
    REVISION_FILE_RE,
    TX_SEGMENT_PREFIX,
    GraphLayout,
)
from graph_cli.storage.files.locking import acquire
from graph_cli.storage.files.segments import (
    LogCorruption,
    LogPosition,
    LogScan,
    PendingAppend,
    SegmentedLog,
)

_PENDING: Final = "storage.recovery_pending"


def _corrupt(message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError("backend.corrupt", message, details=details)


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")


def corruption_details(damage: LogCorruption) -> dict[str, JsonValue]:
    return {
        "segment": damage.segment,
        "offset": damage.offset,
        "expected_seq": damage.expected_seq,
        "reason": damage.reason,
    }


def parse_tx(document: Mapping[str, JsonValue]) -> TxRecord:
    """A hash-valid tx line with a bad shape is damage, not a user error."""
    try:
        return tx_from_json(document)
    except JsonShapeError as exc:
        raise _corrupt(f"tx record has a bad shape: {exc}") from None


@dataclass(frozen=True, slots=True)
class LogState:
    """What a reader or writer finds: checkpoint, txs after it, and the tx-log scan."""

    checkpoint: HeadFile | None
    tail: tuple[TxRecord, ...]  # txs after the checkpoint, in seq order
    scan: LogScan
    warnings: tuple[WarningRecord, ...]

    @property
    def end(self) -> ChainLink:
        return self.scan.end

    @property
    def base_seq(self) -> int:
        """Revisions of txs up to here are materialized."""
        return self.checkpoint.seq if self.checkpoint is not None else 0

    @property
    def head(self) -> Head:
        if self.tail:
            last = self.tail[-1]
            return Head(last.seq, last.tx_id, last.hash, last.recorded_at)
        if self.checkpoint is not None:
            return self.checkpoint.head()
        return EMPTY_HEAD


class _ViewCache:
    """The replay view of this store instance, extended as the log grows (plan D5)."""

    def __init__(self, txlog: SegmentedLog) -> None:
        self._txlog = txlog
        self._lock = threading.Lock()
        self._view = GraphView()
        self._link = GENESIS
        self._position: LogPosition | None = None

    def view_through(self, seq: int) -> GraphView:
        with self._lock:
            if self._view.seq < seq:
                self._extend(seq)
            return self._view

    def _extend(self, seq: int) -> None:
        result = self._txlog.scan(self._link, at=self._position)
        if result.corruption is not None and not result.records:
            # The log changed under the cache (repair, tamper): rebuild from the start.
            self._view, self._link, self._position = GraphView(), GENESIS, None
            result = self._txlog.scan()
        for record in result.records:
            self._view.apply(parse_tx(record.document))
        self._link, self._position = result.end, result.position
        if self._view.seq < seq:
            raise _corrupt(f"tx log ends at seq {self._view.seq}, expected {seq}")

    def committed(self, record: TxRecord, position: LogPosition) -> None:
        """A writer of this instance appended ``record``; skip re-reading it."""
        with self._lock:
            if self._view.seq == record.seq - 1:
                self._view.apply(record)
                self._link, self._position = ChainLink(record.seq, record.hash), position


class FileStore:
    """Read and write ports of one graph directory (``graphs/<graph_id>/``)."""

    def __init__(
        self,
        graph_dir: Path,
        graph_id: GraphId,
        *,
        durability: Durability = "full",
        syncer: Syncer | None = None,
        faults: FaultHook | None = None,
        segment_bytes: int = DEFAULT_SEGMENT_BYTES,
    ) -> None:
        self.layout = GraphLayout(graph_dir)
        self.graph_id = graph_id
        self.stream = graph_stream(str(graph_id))
        self.syncer: Syncer = syncer if syncer is not None else OsSyncer(durability)
        self.faults = faults
        self.txlog = SegmentedLog(self.layout.txlog, TX_SEGMENT_PREFIX, segment_bytes)
        self.auditlog = SegmentedLog(self.layout.audit, AUDIT_SEGMENT_PREFIX, segment_bytes)
        self._cache = _ViewCache(self.txlog)

    # ---- read port -----------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_SNAPSHOT_AT_SEQ})

    def head(self) -> Head:
        return self.read_state().head

    def snapshot(self, *, at_seq: int | None = None) -> FileSnapshot:
        state = self.read_state()
        return FileSnapshot(self, state, resolve_at_seq(at_seq, state.end.seq))

    def changes_since(self, seq: int, *, limit: int = 1000) -> Sequence[TxRecord]:
        end = self.read_state().end.seq
        return self.view_at(end).txs_since(seq, limit=limit)

    # ---- write port ----------------------------------------------------

    @contextmanager
    def session(self, *, timeout_s: float) -> Iterator[FileSession]:
        self._require_layout()
        lock = acquire(self.layout.lock, timeout_s=timeout_s)
        try:
            session = FileSession(self)
            try:
                yield session
            finally:
                session.close()
        finally:
            lock.release()

    # ---- shared internals ----------------------------------------------

    def view_at(self, seq: int) -> ViewAt:
        return self._cache.view_through(seq).at(seq)

    def note_commit(self, record: TxRecord, position: LogPosition) -> None:
        self._cache.committed(record, position)

    def _require_layout(self) -> None:
        if not self.layout.txlog.is_dir() or not self.layout.audit.is_dir():
            raise GraphCliError(
                "backend.unavailable",
                "graph directory is missing or not initialized",
                details={"graph_id": str(self.graph_id)},
            )

    def read_checkpoint(self) -> tuple[HeadFile | None, list[WarningRecord]]:
        try:
            data = self.layout.head.read_bytes()
        except FileNotFoundError:
            return None, []
        except OSError as exc:
            raise _unavailable("cannot read head.json", exc) from exc
        try:
            return parse_head_file(data), []
        except ValueError as exc:
            return None, [WarningRecord(_PENDING, f"head.json is unreadable: {exc}")]

    def read_state(self) -> LogState:
        """Checkpoint plus tx-log scan; falls back to a full scan when they disagree."""
        self._require_layout()
        checkpoint, warnings = self.read_checkpoint()
        try:
            scan = None
            if checkpoint is not None:
                scan = self.txlog.scan(checkpoint.link, at=checkpoint.txlog)
                if scan.corruption is not None:
                    scan = None
                    full = self.txlog.scan()
                    if full.corruption is None:
                        warnings.append(
                            WarningRecord(_PENDING, "head.json does not match the tx log")
                        )
                    checkpoint = None
                    scan = full
            if scan is None:
                scan = self.txlog.scan()
                if checkpoint is None and scan.records and not warnings:
                    warnings.append(WarningRecord(_PENDING, "head.json is missing"))
        except OSError as exc:
            raise _unavailable("cannot read the tx log", exc) from exc
        if scan.corruption is not None:
            warnings.append(
                WarningRecord(
                    _PENDING,
                    "the tx log is damaged; reads stop before the damage",
                    details=corruption_details(scan.corruption),
                )
            )
        tail = tuple(parse_tx(record.document) for record in scan.records)
        return LogState(checkpoint, tail, scan, tuple(warnings))

    def scan_audit(self, checkpoint: HeadFile | None) -> LogScan:
        try:
            if checkpoint is not None and checkpoint.audit is not None:
                result = self.auditlog.scan(checkpoint.audit_link, at=checkpoint.audit)
                if result.corruption is None:
                    return result
            return self.auditlog.scan()
        except OSError as exc:
            raise _unavailable("cannot read the audit stream", exc) from exc

    def revision_numbers(self, entity: EntityId) -> list[int]:
        """Revision numbers with a file, highest first."""
        try:
            names = os.listdir(self.layout.entity_dir(entity))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise _unavailable("cannot list revisions", exc) from exc
        numbers = []
        for name in names:
            match = REVISION_FILE_RE.match(name)
            if match:
                numbers.append(int(match.group(1)))
        return sorted(numbers, reverse=True)

    def read_revision(self, entity: EntityId, rev: int) -> Revision | None:
        path = self.layout.revision_path(entity, rev)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _unavailable("cannot read a revision file", exc) from exc
        try:
            revision = revision_from_json(as_object(parse_json(data), "revision"))
        except (CanonicalError, JsonShapeError) as exc:
            raise _corrupt(f"revision file is unreadable: {exc}", entity=str(entity)) from None
        if (revision.id, revision.rev) != (entity, rev):
            raise _corrupt("revision file does not match its path", entity=str(entity), rev=rev)
        return revision


class FileSnapshot:
    """A lock-free snapshot at seq S (B4.10)."""

    def __init__(self, store: FileStore, state: LogState, seq: int) -> None:
        self._store = store
        self._seq = seq
        self._warnings = state.warnings
        self._overlay: dict[EntityId, list[Revision]] = {}
        for tx in state.tail:
            if tx.seq > seq:
                break
            for revision in tx.revisions:
                self._overlay.setdefault(revision.id, []).append(revision)
        self._view: ViewAt | None = None

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return self._warnings

    def _scan_view(self) -> ViewAt:
        if self._view is None:
            self._view = self._store.view_at(self._seq)
        return self._view

    # point reads: revision files with tx_seq <= S, overlaid with the tail

    def get(self, entity: EntityId) -> Revision | None:
        overlay = self._overlay.get(entity)
        if overlay:
            return overlay[-1]
        for rev in self._store.revision_numbers(entity):
            revision = self._store.read_revision(entity, rev)
            if revision is not None and revision.tx_seq <= self._seq:
                return revision
        return None

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        found = {}
        for entity in entities:
            revision = self.get(entity)
            if revision is not None:
                found[entity] = revision
        return found

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        for revision in self._overlay.get(entity, ()):
            if revision.rev == rev:
                return revision
        found = self._store.read_revision(entity, rev)
        return found if found is not None and found.tx_seq <= self._seq else None

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]:
        metas: dict[int, RevisionMeta] = {}
        for rev in self._store.revision_numbers(entity):
            revision = self._store.read_revision(entity, rev)
            if revision is not None and revision.tx_seq <= self._seq:
                metas[rev] = revision_meta(revision)
        for revision in self._overlay.get(entity, ()):
            metas[revision.rev] = revision_meta(revision)
        return [metas[rev] for rev in sorted(metas)]

    # scans: replay view at S

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self._scan_view().incoming(entity, rel)

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        return self._scan_view().resolve_alias(alias)

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        return self._scan_view().match_suffix(hex_suffix, limit=limit)

    def query(self, q: Query) -> QueryPage:
        return run_query(self._scan_view(), q)

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        return run_neighbors(self._scan_view(), q)


class FileSession:
    """``WriteSession`` of FileStore; exists only while the graph lock is held."""

    def __init__(self, store: FileStore) -> None:
        self._store = store
        self._token = object()
        self._used: set[EventId] = set()
        self._open = True
        self._broken = False
        state = store.read_state()
        if state.scan.corruption is not None:
            raise _corrupt(
                "the tx log is damaged; writes are refused until repair",
                **corruption_details(state.scan.corruption),
            )
        audit = store.scan_audit(state.checkpoint)
        if audit.corruption is not None:
            raise _corrupt(
                "the graph audit stream is damaged; writes are refused until repair",
                **corruption_details(audit.corruption),
            )
        if state.scan.tail != "clean" or audit.tail != "clean":
            raise GraphCliError("backend.unavailable", "the graph needs recovery")
        self._end = state.end
        self._position = state.scan.position
        self._head = state.head
        self._audit_link = audit.end
        self._audit_position = audit.position
        self._pending: list[TxRecord] = list(state.tail)
        self._view_seq = state.end.seq
        store.view_at(self._view_seq)
        if self._pending or (state.checkpoint is None and state.end.seq > 0):
            self._roll_forward()

    # ---- session state -------------------------------------------------

    def close(self) -> None:
        self._open = False

    def _check_usable(self) -> None:
        if not self._open:
            raise GraphCliError("internal.error", "the write session is closed")
        if self._broken:
            raise GraphCliError(
                "backend.unavailable", "an earlier failure left this session unusable"
            )

    def _view(self) -> ViewAt:
        return self._store.view_at(self._view_seq)

    def snapshot(self) -> ViewSnapshot:
        self._check_usable()
        return ViewSnapshot(self._view())

    def head(self) -> Head:
        self._check_usable()
        return self._head

    def alias_counters(self) -> Mapping[str, int]:
        self._check_usable()
        return self._view().alias_counters()

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None:
        self._check_usable()
        return self._view().lookup_idempotency(key, principal_id)

    # ---- audit ---------------------------------------------------------

    def append_audit(self, event: AuditEventDraft) -> AuditReceipt:
        self._check_usable()
        store = self._store
        body, event_id = audit_body(event, store.stream)
        document, line = seal(body, self._audit_link)
        pending: PendingAppend | None = None
        try:
            fire(store.faults, CrashPoint.AUDIT_BEFORE_WRITE)
            pending = store.auditlog.append(line, self._audit_link.seq + 1, self._audit_position)
            fire(store.faults, CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC)
            pending.sync(store.syncer)
            fire(store.faults, CrashPoint.AUDIT_AFTER_SYNC)
        except OSError as exc:
            self._undo(pending)
            raise GraphCliError(
                "audit.unavailable",
                f"audit append failed: {exc.strerror or exc}",
                details={"stream": store.stream},
            ) from exc
        pending.close()
        self._audit_link = link_of(document)
        self._audit_position = pending.end
        return AuditReceipt(
            store.stream, event_id, self._audit_link.seq, self._audit_link.hash, self._token
        )

    def _undo(self, pending: PendingAppend | None) -> None:
        """Remove an unacknowledged line; if that fails, nothing more is written here."""
        if pending is None:
            return
        try:
            pending.rollback(self._store.syncer)
        except OSError:
            pending.close()
            self._broken = True

    # ---- commit --------------------------------------------------------

    def commit(self, tx: AuthorizedTx, receipt: AuditReceipt) -> CommitResult:
        self._check_usable()
        store = self._store
        check_receipt(
            receipt, tx=tx, stream=store.stream, session_token=self._token, used_events=self._used
        )
        check_tx(tx, graph_id=store.graph_id, head=self._head, view=self._view())
        record, line = seal_tx(tx, self._end)
        pending: PendingAppend | None = None
        try:
            fire(store.faults, CrashPoint.TX_BEFORE_WRITE)
            pending = store.txlog.append(line, record.seq, self._position)
            fire(store.faults, CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC)
            pending.sync(store.syncer)
        except OSError as exc:
            self._undo(pending)
            raise _unavailable("tx append failed", exc) from exc
        pending.close()
        # ---- commit point: the tx is durable ----
        self._used.add(receipt.event_id)
        self._end = ChainLink(record.seq, record.hash)
        self._position = pending.end
        self._head = Head(record.seq, record.tx_id, record.hash, record.recorded_at)
        self._pending.append(record)
        store.note_commit(record, pending.end)
        self._view_seq = record.seq
        warnings: list[WarningRecord] = []
        try:
            fire(store.faults, CrashPoint.TX_AFTER_SYNC)
            self._roll_forward()
        except (OSError, GraphCliError) as exc:
            if isinstance(exc, GraphCliError) and exc.code != "backend.corrupt":
                raise
            warnings.append(
                WarningRecord(
                    "storage.materialization_deferred",
                    f"the tx is committed; derived files are not updated yet: {exc}",
                )
            )
        return CommitResult(record.seq, record.tx_id, record.hash, tuple(warnings))

    # ---- derived files -------------------------------------------------

    def _roll_forward(self) -> None:
        """Materialize every pending tx, then write the checkpoint (section 4.2 steps 2-3)."""
        materialize(self._store, self._pending)
        self._pending.clear()
        if self._end.seq > 0:
            self._write_head()

    def _write_head(self) -> None:
        store = self._store
        assert self._position is not None
        checkpoint = HeadFile(
            seq=self._head.seq,
            tx_id=self._head.tx_id,  # type: ignore[arg-type]
            hash=self._head.hash,
            recorded_at=self._head.recorded_at,  # type: ignore[arg-type]
            txlog=self._position,
            audit_link=self._audit_link,
            audit=self._audit_position,
            alias_counters=self._view().alias_counters(),
        )
        write_head(store, checkpoint)


def materialize(store: FileStore, txs: Sequence[TxRecord]) -> list[str]:
    """Write the missing revision files of ``txs`` (W.14); return the files written.

    Existing files must be byte-equal to the tx copy, else ``backend.corrupt``.
    """
    layout, syncer = store.layout, store.syncer
    directories: set[Path] = set()
    written: list[str] = []
    for tx in txs:
        for revision in tx.revisions:
            path = layout.revision_path(revision.id, revision.rev)
            data = pretty_bytes(revision_to_json(revision))
            if path.exists():
                if path.read_bytes() != data:
                    raise _corrupt(
                        "an existing revision file differs from the tx log",
                        entity=str(revision.id),
                        rev=revision.rev,
                    )
                continue
            ensure_dir(path.parent, syncer)
            temp = write_temp(path, data, syncer)
            os.replace(temp, path)
            directories.add(path.parent)
            written.append(str(path.relative_to(layout.root)))
            if len(written) == 1:
                fire(store.faults, CrashPoint.MATERIALIZE_AFTER_FIRST_FILE)
    if written:
        fire(store.faults, CrashPoint.MATERIALIZE_BEFORE_DIR_SYNC)
    for directory in sorted(directories):
        syncer.sync_dir(directory)
    return written


def write_head(store: FileStore, checkpoint: HeadFile) -> None:
    """Replace ``head.json`` atomically (W.15)."""
    layout = store.layout
    temp = write_temp(layout.head, checkpoint.to_bytes(), store.syncer)
    fire(store.faults, CrashPoint.HEAD_BEFORE_RENAME)
    os.replace(temp, layout.head)
    fire(store.faults, CrashPoint.HEAD_AFTER_RENAME)
    store.syncer.sync_dir(layout.data)


if TYPE_CHECKING:  # static proof that the adapter implements the ports

    def _conforms(
        store: FileStore, session: FileSession, snapshot: FileSnapshot
    ) -> tuple[GraphStoreRead, GraphStoreWrite, WriteSession, Snapshot]:
        return store, store, session, snapshot
