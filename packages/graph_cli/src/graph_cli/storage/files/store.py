"""FileStore: one graph in files (contracts B4, B13, sections 4 and 5). POSIX only.

Write path (one session holds the graph lock): audit intent append + sync,
tx append + sync (the commit point), revision files (create-only temp, sync,
rename, directory sync), then the ``head.json`` checkpoint. Failures after
the commit point do not fail the write (B3.5); the next writer rolls forward.

Read path (lock-free, B4.10, B13.7): the snapshot seq is the last complete,
hash-valid tx line. Point reads (``get``, ``get_revision``, ``history``)
use revision files with ``tx_seq <= S``, overlaid with the txs after the
checkpoint. Scans (``query``, ``incoming``, ``neighbors``, aliases,
suffixes, ``tx``) read the disposable index plus the txs after it
(``storage.files.index``); without a usable index, or before its seq, they
replay the tx log into a view that is cached per store instance and
extended incrementally (plan D5). Reads never write anything; writers
update the index after the checkpoint.

A sealed graph (store format 2, plan T16) reads and writes every record
through its ``RecordCodec``: the tx log, the revision files and the index
hold sealed records only. Snapshots and write sessions refresh the key ring
first, so a purge by another process is seen by the next read.

Writers read the index too, once the SHA-256 digest of the log up to the
index position matches the one recorded with it; they verify only the txs
after it (``WriterView``, plan "Incremental writer checks"). Without a
usable index, when the digest differs, and at least every
``FULL_CHECK_EVERY`` txs they replay and verify the whole log and rebuild
the index from it.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json, pretty_bytes
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TermRef
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt, graph_stream
from graph_cli.kernel.ports.storage import (
    CAPABILITY_BLOB_LOCAL_PATH,
    CAPABILITY_SNAPSHOT_AT_SEQ,
    EMPTY_HEAD,
    CommitResult,
    GraphStoreRead,
    GraphStoreWrite,
    Head,
    Snapshot,
    StagedBlob,
    WriteSession,
)
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.record_codec import RecordCodec
from graph_cli.kernel.records import (
    BlobInfo,
    EdgeRecord,
    Revision,
    RevisionMeta,
    TxRecord,
    revision_meta,
    revision_to_json,
)
from graph_cli.kernel.sealing import KeyRing
from graph_cli.storage.common.audit import audit_body
from graph_cli.storage.common.chain import GENESIS, ChainLink, link_of, seal
from graph_cli.storage.common.commit import check_receipt, check_tx, seal_tx
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.snapshot import ViewSnapshot, resolve_at_seq
from graph_cli.storage.common.view import GraphView, ViewAt
from graph_cli.storage.files import blobs
from graph_cli.storage.files.durable import Durability, OsSyncer, Syncer, ensure_dir, write_temp
from graph_cli.storage.files.headfile import HeadFile, parse_head_file
from graph_cli.storage.files.index import (
    FULL_CHECK_EVERY,
    INDEX_ERRORS,
    INDEX_FORMAT,
    IndexedView,
    IndexMeta,
    IndexReader,
    IndexUnusable,
    ScanView,
    TxSpan,
    extend_index,
    log_digest,
    rebuild_index,
)
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
    OpLog,
    PendingAppend,
    SegmentedLog,
)

_PENDING: Final = "storage.recovery_pending"
_INDEX_STALE: Final = "storage.index_stale"

T = TypeVar("T")


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


def parse_tx(document: Mapping[str, JsonValue], codec: RecordCodec) -> TxRecord:
    """A hash-valid tx line with a bad shape is damage, not a user error."""
    try:
        return codec.parse_tx(document)
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


@dataclass(slots=True)
class WriterIndex:
    """An index a writer checked: its meta, the view over it, the txs after it."""

    reader: IndexReader
    meta: IndexMeta
    view: IndexedView
    after: list[tuple[TxRecord, TxSpan]]  # the verified txs after the index


class _ViewCache:
    """The replay view of this store instance, extended as the log grows (plan D5)."""

    def __init__(self, txlog: SegmentedLog, codec: RecordCodec) -> None:
        self._txlog = txlog
        self._codec = codec
        self._lock = threading.Lock()
        self._view = GraphView()
        self._spans: list[TxSpan] = []  # where each tx of the view lies in the log
        self._link = GENESIS
        self._position: LogPosition | None = None

    def view_through(self, seq: int) -> GraphView:
        with self._lock:
            if self._view.seq < seq:
                self._extend(seq)
            return self._view

    def spans_through(self, seq: int) -> tuple[GraphView, list[TxSpan]]:
        """The view and the positions of its txs, at least through ``seq``."""
        with self._lock:
            if self._view.seq < seq:
                self._extend(seq)
            return self._view, list(self._spans)

    def _extend(self, seq: int) -> None:
        result = self._txlog.scan(self._link, at=self._position)
        if result.corruption is not None and not result.records:
            # The log changed under the cache (repair, tamper): rebuild from the start.
            self._view, self._spans, self._link, self._position = GraphView(), [], GENESIS, None
            result = self._txlog.scan()
        for record in result.records:
            self._view.apply(parse_tx(record.document, self._codec))
            self._spans.append(TxSpan(record.start, record.end))
        self._link, self._position = result.end, result.position
        if self._view.seq < seq:
            details = corruption_details(result.corruption) if result.corruption else {}
            raise _corrupt(f"the tx log is damaged before seq {seq}; repair is needed", **details)

    def committed(self, record: TxRecord, span: TxSpan) -> None:
        """A writer of this instance appended ``record``; skip re-reading it."""
        with self._lock:
            if self._view.seq == record.seq - 1:
                self._view.apply(record)
                self._spans.append(span)
                self._link, self._position = ChainLink(record.seq, record.hash), span.end


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
        oplog: OpLog | None = None,
        use_index: bool = True,
        full_check_every: int = FULL_CHECK_EVERY,
        codec: RecordCodec | None = None,
    ) -> None:
        self.layout = GraphLayout(graph_dir)
        self.codec = codec if codec is not None else RecordCodec(graph_id)
        self.use_index = use_index
        self.full_check_every = full_check_every
        self.oplog = oplog
        self.graph_id = graph_id
        self.stream = graph_stream(str(graph_id))
        self.syncer: Syncer = syncer if syncer is not None else OsSyncer(durability)
        self.faults = faults
        self.txlog = SegmentedLog(self.layout.txlog, TX_SEGMENT_PREFIX, segment_bytes)
        self.auditlog = SegmentedLog(self.layout.audit, AUDIT_SEGMENT_PREFIX, segment_bytes)
        self._cache = _ViewCache(self.txlog, self.codec)
        self._generation = self.codec.ring.generation

    # ---- read port -----------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        if self.codec.sealed:  # its blobs hold ciphertext: no path to hand out (T16)
            return frozenset({CAPABILITY_SNAPSHOT_AT_SEQ})
        return frozenset({CAPABILITY_SNAPSHOT_AT_SEQ, CAPABILITY_BLOB_LOCAL_PATH})

    def head(self) -> Head:
        return self.read_state().head

    def key_ring(self) -> KeyRing:
        return self.codec.ring

    def blob(self, digest: str) -> BlobInfo | None:
        if self.codec.sealed:
            return blobs.content_info(self.layout, digest)
        return blobs.info(self.layout, digest)

    def read_blob(self, digest: str) -> Iterator[bytes]:
        if self.codec.sealed:
            self._refresh_keys()
            return blobs.read_sealed(self.layout, digest, self.codec.ring)
        return blobs.read(self.layout, digest)

    def blob_local_path(self, digest: str) -> Path | None:
        if self.codec.sealed:
            return None
        return blobs.local_path(self.layout, digest)

    def snapshot(self, *, at_seq: int | None = None) -> FileSnapshot:
        self._refresh_keys()
        # The index is opened before the log is read: it is published after its txs
        # are committed, so it never runs ahead of the log read afterwards.
        reader, problem = self._open_index()
        state = self.read_state()
        seq = resolve_at_seq(at_seq, state.end.seq)
        index = None
        if reader is not None:
            try:
                index = self._bind_index(reader, state, seq)
            except IndexUnusable as exc:
                problem = str(exc)
            if index is None:
                reader.close()
        warnings = state.warnings
        if problem is not None:
            message = f"{problem}; reads replay the tx log (graph-admin graph reindex rebuilds it)"
            warnings = (*warnings, WarningRecord(_INDEX_STALE, message))
        return FileSnapshot(self, state, seq, index, warnings)

    def changes_since(self, seq: int, *, limit: int = 1000) -> Sequence[TxRecord]:
        end = self.read_state().end.seq
        return self.view_at(end).txs_since(seq, limit=limit)

    # ---- write port ----------------------------------------------------

    @contextmanager
    def session(self, *, timeout_s: float) -> Iterator[FileSession]:
        self._require_layout()
        lock = acquire(self.layout.lock, timeout_s=timeout_s)
        try:
            self._refresh_keys()
            session = FileSession(self)
            try:
                yield session
            finally:
                session.close()
        finally:
            lock.release()

    def stage_blob(self, chunks: Iterable[bytes], *, max_bytes: int) -> blobs.FileStagedBlob:
        """KB0: hashed into ``blobs/.tmp-*`` without the lock (an in-flight add)."""
        self._require_layout()
        return blobs.stage(
            self.layout,
            chunks,
            max_bytes=max_bytes,
            syncer=self.syncer,
            faults=self.faults,
            ring=self.codec.ring if self.codec.sealed else None,
        )

    # ---- shared internals ----------------------------------------------

    def _refresh_keys(self) -> None:
        """Pick up shreds; after one, the replay cache holds records opened under keys that
        are gone, so it is rebuilt from the log (T16)."""
        ring = self.codec.ring
        ring.refresh()
        if ring.generation != self._generation:
            self._cache = _ViewCache(self.txlog, self.codec)
            self._generation = ring.generation

    def view_at(self, seq: int) -> ViewAt:
        return self._cache.view_through(seq).at(seq)

    def note_commit(self, record: TxRecord, span: TxSpan) -> None:
        self._cache.committed(record, span)

    def rebuild_index(self, seq: int) -> IndexMeta | None:
        """Build the index for ``seq`` from the full replay (W.15); the caller holds the
        graph lock and ``seq`` is the end of the log."""
        view, spans = self._cache.spans_through(seq)
        if view.seq != seq:
            raise IndexUnusable(f"the log ends at seq {view.seq}, not {seq}")
        return rebuild_index(
            self.layout,
            self.graph_id,
            view,
            spans,
            self.txlog,
            syncer=self.syncer,
            faults=self.faults,
        )

    def open_index(self) -> IndexReader | None:
        """The index file read through this graph's codec; ``IndexUnusable`` when broken."""
        return IndexReader.open(self.layout.index, self.codec)

    def writer_index(self, state: LogState) -> tuple[WriterIndex | None, str | None]:
        """The index a writer may read at the end of the log, or ``None`` to replay.

        The second value says why an existing index failed its log digest (a warning
        for the result); the other reasons to replay are normal and silent.
        """
        if not self.use_index:
            return None, None
        try:
            reader = self.open_index()
        except IndexUnusable:
            return None, None
        if reader is None:
            return None, None
        try:
            meta = reader.meta
            if meta.format != INDEX_FORMAT or meta.full_verified_seq is None:
                raise IndexUnusable(f"the index format is not {INDEX_FORMAT}")
            if state.end.seq - meta.full_verified_seq >= self.full_check_every:
                raise IndexUnusable("a full check of the log is due")
            tail = self._index_tail(meta, state, state.end.seq)
            assert tail is not None  # the end is never before the index seq
            try:
                if log_digest(self.txlog, meta.position, ()) != meta.digest:
                    reader.close()
                    return None, "the log digest of the index does not match the tx log"
            except OSError as exc:
                raise _unavailable("cannot read the tx log", exc) from exc
        except IndexUnusable:
            reader.close()
            return None, None
        except BaseException:
            reader.close()
            raise
        txs = [tx for tx, _ in tail]
        view = IndexedView(reader, state.end.seq, txs, self.txlog, check_content=True)
        return WriterIndex(reader, meta, view, list(tail)), None

    def _open_index(self) -> tuple[IndexReader | None, str | None]:
        """The index file, or why an existing one cannot be used; a missing one is normal."""
        if not self.use_index:
            return None, None
        try:
            return self.open_index(), None
        except IndexUnusable as exc:
            return None, str(exc)

    def _bind_index(self, reader: IndexReader, state: LogState, seq: int) -> IndexedView | None:
        """The index plus the txs after it, as of ``seq``; ``None`` for a seq before it."""
        tail = self._index_tail(reader.meta, state, seq)
        if tail is None:
            return None
        return IndexedView(reader, seq, [tx for tx, _ in tail if tx.seq <= seq], self.txlog)

    def _index_tail(
        self, meta: IndexMeta, state: LogState, seq: int
    ) -> list[tuple[TxRecord, TxSpan]] | None:
        """The verified txs after the index and where they lie, after checking that the
        index binds to this log; ``None`` for a ``seq`` before the index."""
        if meta.graph_id != str(self.graph_id):
            raise IndexUnusable("the index belongs to another graph")
        if meta.seq > state.end.seq:
            raise IndexUnusable("the index is ahead of the tx log")
        if seq < meta.seq:
            return None  # an earlier state (--as-of): the replay answers it
        mismatch = IndexUnusable("the index does not match the tx log")
        base = state.base_seq
        # the scan records of the txs after the checkpoint (recovery may have moved it)
        after = state.scan.records[len(state.scan.records) - len(state.tail) :]
        if meta.seq >= base:
            if meta.seq == base:
                checkpoint = state.checkpoint
                if checkpoint is None or (checkpoint.link, checkpoint.txlog) != (
                    meta.link,
                    meta.position,
                ):
                    raise mismatch
            else:
                at = meta.seq - base - 1
                if (state.tail[at].hash, after[at].end) != (
                    meta.link.hash,
                    meta.position,
                ):
                    raise mismatch
            records = after[meta.seq - base :]
            tail = state.tail[meta.seq - base :]
        else:  # the index is behind the checkpoint (an older writer, a failed update)
            try:
                scan = self.txlog.scan(meta.link, at=meta.position)
            except OSError as exc:
                raise _unavailable("cannot read the tx log", exc) from exc
            if not scan.records or scan.end.seq < seq:
                raise mismatch
            records = scan.records
            tail = tuple(parse_tx(record.document, self.codec) for record in records)
        return [(tx, TxSpan(r.start, r.end)) for tx, r in zip(tail, records, strict=True)]

    def _require_layout(self) -> None:
        if not self.layout.txlog.is_dir() or not self.layout.audit.is_dir():
            raise GraphCliError(
                "backend.unavailable",
                "graph directory is missing or not initialized",
                details={"graph_id": str(self.graph_id)},
            )

    def read_checkpoint(self) -> tuple[HeadFile | None, str | None]:
        """The checkpoint, or ``None`` plus why it cannot be used."""
        try:
            data = self.layout.head.read_bytes()
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise _unavailable("cannot read head.json", exc) from exc
        try:
            return parse_head_file(data), None
        except ValueError as exc:
            return None, f"head.json is unreadable: {exc}"

    def read_state(self) -> LogState:
        """Checkpoint plus tx-log scan; falls back to a full scan when they disagree."""
        self._require_layout()
        checkpoint, problem = self.read_checkpoint()
        reasons = [problem] if problem else []
        try:
            scan = None
            if checkpoint is not None:
                scan = self.txlog.scan(checkpoint.link, at=checkpoint.txlog)
                if scan.corruption is not None:
                    reasons.append("head.json does not match the tx log")
                    checkpoint, scan = None, None
            if scan is None:
                scan = self.txlog.scan()
                if not reasons and scan.records:
                    reasons.append("head.json is missing")
        except OSError as exc:
            raise _unavailable("cannot read the tx log", exc) from exc
        details: dict[str, JsonValue] = {}
        if scan.corruption is not None:
            reasons.append("the tx log is damaged; reads stop before the damage")
            details = corruption_details(scan.corruption)
        warnings = (WarningRecord(_PENDING, "; ".join(reasons), details),) if reasons else ()
        tail = tuple(parse_tx(record.document, self.codec) for record in scan.records)
        return LogState(checkpoint, tail, scan, warnings)

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
            revision = self.codec.parse_revision(as_object(parse_json(data), "revision"))
        except (CanonicalError, JsonShapeError) as exc:
            raise _corrupt(f"revision file is unreadable: {exc}", entity=str(entity)) from None
        if (revision.id, revision.rev) != (entity, rev):
            raise _corrupt("revision file does not match its path", entity=str(entity), rev=rev)
        return revision


class FileSnapshot:
    """A lock-free snapshot at seq S (B4.10)."""

    def __init__(
        self,
        store: FileStore,
        state: LogState,
        seq: int,
        index: IndexedView | None = None,
        warnings: tuple[WarningRecord, ...] | None = None,
    ) -> None:
        self._store = store
        self._seq = seq
        self._index = index
        self._warnings = state.warnings if warnings is None else warnings
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

    def _replay(self) -> ViewAt:
        if self._view is None:
            self._view = self._store.view_at(self._seq)
        return self._view

    def _scan(self, read: Callable[[ScanView], T]) -> T:
        """``read`` on the index view; on the replay when there is none or it fails."""
        if self._index is not None:
            try:
                return read(self._index)
            except IndexUnusable:
                self._index = None  # damaged after the bind check: replay from now on
        return read(self._replay())

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

    def tx(self, seq: int) -> TxRecord | None:
        return self._scan(lambda view: view.tx(seq)) if 1 <= seq <= self._seq else None

    # scans: the index view at S, else the replay view at S

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self._scan(lambda view: view.incoming(entity, rel))

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        return self._scan(lambda view: view.resolve_alias(alias))

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        return self._scan(lambda view: view.match_suffix(hex_suffix, limit=limit))

    def query(self, q: Query) -> QueryPage:
        return self._scan(lambda view: run_query(view, q))

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        return self._scan(lambda view: run_neighbors(view, q))


class WriterView:
    """What a write session reads at the head: the checked index plus the txs after it,
    or the replay view of the whole log.

    It starts on the index when ``FileStore.writer_index`` accepts one, and moves to
    the replay for good when the index answers ``IndexUnusable`` (``fell_back`` says
    why). Point reads of earlier revisions use the revision files, as snapshots do.
    """

    def __init__(self, store: FileStore, seq: int, index: WriterIndex | None) -> None:
        self._store = store
        self.seq = seq
        self.index = index
        self.fell_back: str | None = None
        self._overlay: dict[EntityId, list[Revision]] = {}
        if index is not None:
            for tx, _ in index.after:
                self._note(tx)

    def _note(self, tx: TxRecord) -> None:
        for revision in tx.revisions:
            self._overlay.setdefault(revision.id, []).append(revision)

    def close(self) -> None:
        if self.index is not None:
            self.index.reader.close()

    def add(self, record: TxRecord, span: TxSpan) -> None:
        """This session committed ``record`` at ``span``."""
        self.seq = record.seq
        self._store.note_commit(record, span)
        if self.index is not None:
            self.index.view.add(record)
            self._note(record)

    def _replay(self) -> ViewAt:
        return self._store.view_at(self.seq)

    def _read(self, read: Callable[[IndexedView], T], replay: Callable[[ViewAt], T]) -> T:
        if self.index is not None:
            try:
                return read(self.index.view)
            except IndexUnusable as exc:
                self._fall_back(str(exc))
        return replay(self._replay())

    def _fall_back(self, reason: str) -> None:
        assert self.index is not None
        self.index.reader.close()
        self.index = None
        self.fell_back = reason

    def confirm(self, entities: Iterable[EntityId]) -> None:
        """Check the index rows the commit checks rely on against their tx lines."""
        if self.index is None:
            return
        try:
            for entity in entities:
                self.index.view.confirm(entity)
        except IndexUnusable as exc:
            self._fall_back(str(exc))

    # what commit checks, services and the checkpoint read

    def current(self, entity: EntityId) -> Revision | None:
        return self._read(lambda v: v.current(entity), lambda v: v.current(entity))

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        return self._read(lambda v: v.get_many(entities), lambda v: v.get_many(entities))

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterable[Revision]:
        return self._read(
            lambda v: list(v.current_revisions(types)), lambda v: list(v.current_revisions(types))
        )

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self._read(lambda v: v.incoming(entity, rel), lambda v: v.incoming(entity, rel))

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        return self._read(lambda v: v.resolve_alias(alias), lambda v: v.resolve_alias(alias))

    def alias_bound(self, alias: str) -> bool:
        return self._read(lambda v: v.alias_bound(alias), lambda v: v.alias_bound(alias))

    def alias_counters(self) -> dict[str, int]:
        return self._read(lambda v: v.alias_counters(), lambda v: v.alias_counters())

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None:
        return self._read(
            lambda v: v.lookup_idempotency(key, principal_id),
            lambda v: v.lookup_idempotency(key, principal_id),
        )

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        return self._read(
            lambda v: v.match_suffix(hex_suffix, limit=limit),
            lambda v: v.match_suffix(hex_suffix, limit=limit),
        )

    def tx(self, seq: int) -> TxRecord:
        return self._read(lambda v: v.tx(seq), lambda v: v.tx(seq))

    def revision(self, entity: EntityId, rev: int) -> Revision | None:
        if self.index is None:
            return self._replay().revision(entity, rev)
        for revision in self._overlay.get(entity, ()):
            if revision.rev == rev:
                return revision
        found = self._store.read_revision(entity, rev)
        return found if found is not None and found.tx_seq <= self.seq else None

    def history(self, entity: EntityId) -> list[RevisionMeta]:
        if self.index is None:
            return self._replay().history(entity)
        metas: dict[int, RevisionMeta] = {}
        for rev in self._store.revision_numbers(entity):
            revision = self._store.read_revision(entity, rev)
            if revision is not None and revision.tx_seq <= self.seq:
                metas[rev] = revision_meta(revision)
        for revision in self._overlay.get(entity, ()):
            metas[revision.rev] = revision_meta(revision)
        return [metas[rev] for rev in sorted(metas)]


class FileSession:
    """``WriteSession`` of FileStore; exists only while the graph lock is held."""

    def __init__(self, store: FileStore) -> None:
        self._store = store
        self._token = object()
        self._used: set[EventId] = set()
        self._open = True
        self._broken = False
        # Recovery needs the store's primitives, so it is imported here (no import cycle).
        from graph_cli.storage.files.recovery import recover

        recovered = recover(store, oplog=store.oplog)
        self.recovery = recovered.report
        state, audit = recovered.state, recovered.audit
        self._end = state.end
        self._position = state.scan.position
        self._head = state.head
        self._audit_link = audit.end
        self._audit_position = audit.position
        self._pending: list[TxRecord] = []
        index, self._index_warning = store.writer_index(state)
        self._writer = WriterView(store, state.end.seq, index)
        if index is None:
            store.view_at(state.end.seq)  # verify the whole log before anything is written
        # what the next index update starts from: an index and the txs after it
        self._index_base = index.meta if index is not None else None
        self._index_txs = list(index.after) if index is not None else []

    # ---- session state -------------------------------------------------

    def close(self) -> None:
        if self._open and self._index_doubt() is not None and self._store.use_index:
            # the index failed a check and no commit rebuilt it
            with contextlib.suppress(*INDEX_ERRORS, GraphCliError):
                self._store.rebuild_index(self._end.seq)
        self._writer.close()
        self._open = False

    def _index_doubt(self) -> str | None:
        """Why the index failed a check in this session, until it is rebuilt."""
        return self._index_warning or self._writer.fell_back

    def _check_usable(self) -> None:
        if not self._open:
            raise GraphCliError("internal.error", "the write session is closed")
        if self._broken:
            raise GraphCliError(
                "backend.unavailable", "an earlier failure left this session unusable"
            )

    def _view(self) -> WriterView:
        return self._writer

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

    def claim_receipt(self, receipt: AuditReceipt) -> None:
        """A maintenance effect consumes the receipt of its intent (B5.7)."""
        self._check_usable()
        check_receipt(
            receipt,
            event_id=None,
            stream=self._store.stream,
            session_token=self._token,
            used_events=self._used,
        )
        self._used.add(receipt.event_id)

    # ---- blobs ---------------------------------------------------------

    def publish_blob(self, staged: StagedBlob) -> BlobInfo:
        self._check_usable()
        store = self._store
        return blobs.publish(store.layout, staged, syncer=store.syncer, faults=store.faults)

    # ---- commit --------------------------------------------------------

    def commit(self, tx: AuthorizedTx, receipt: AuditReceipt) -> CommitResult:
        self._check_usable()
        store = self._store
        check_receipt(
            receipt,
            event_id=tx.event_id,
            stream=store.stream,
            session_token=self._token,
            used_events=self._used,
        )
        self._writer.confirm(_relied_on(tx))
        check_tx(
            tx,
            graph_id=store.graph_id,
            head=self._head,
            view=self._view(),
            blob_exists=lambda digest: blobs.info(store.layout, digest) is not None,
            codec=store.codec,
        )
        record, line = seal_tx(tx, self._end, store.codec)
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
        span = TxSpan(pending.start, pending.end)
        self._writer.add(record, span)
        self._index_txs.append((record, span))
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
        else:
            warnings += self._update_index()
        return CommitResult(record.seq, record.tx_id, record.hash, tuple(warnings))

    # ---- derived files -------------------------------------------------

    def _roll_forward(self) -> None:
        """Materialize the txs committed in this session, then write the checkpoint."""
        materialize(self._store, self._pending)
        self._pending.clear()
        if self._end.seq > 0:
            self._write_head()

    def _update_index(self) -> list[WarningRecord]:
        """Best effort (W.15): a failure leaves the old index, which readers still bind.

        On the checked index only this session's txs (and those after the index) are
        applied; after a full replay the index is rebuilt from it.
        """
        store = self._store
        if not store.use_index:
            return []
        doubt = self._index_doubt()
        if doubt is not None:
            self._index_base = None  # rebuild from the replay
        try:
            if self._index_base is None:
                self._index_base = store.rebuild_index(self._end.seq)
            else:
                self._index_base = extend_index(
                    store.layout,
                    store.graph_id,
                    self._index_base,
                    self._index_txs,
                    store.txlog,
                    syncer=store.syncer,
                    faults=store.faults,
                )
        except INDEX_ERRORS as exc:
            message = f"the tx is committed; the index is not updated yet: {exc}"
            return [WarningRecord("storage.materialization_deferred", message)]
        self._index_txs = []
        if doubt is None:
            return []
        self._index_warning = self._writer.fell_back = None
        message = f"{doubt}; the index was rebuilt from the tx log"
        return [WarningRecord(_INDEX_STALE, message)]

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


def _relied_on(tx: AuthorizedTx) -> list[EntityId]:
    """The entities whose current revision ``check_tx`` compares against."""
    entities = {revision.id for revision in tx.revisions}
    entities.update(edge.target for revision in tx.revisions for edge in revision.edges)
    entities.update(entity for entity, _ in tx.alias_allocations)
    return sorted(entities)


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
