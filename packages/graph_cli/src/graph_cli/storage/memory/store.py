"""InMemoryStore: read and write ports over one live replay view (contracts B4, B13).

With a key ring the store holds sealed records (store format 2, plan T16), as a
FileStore of a sealed graph does; the view keeps the decoded records.
"""

from __future__ import annotations

import hashlib
import io
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import parse_json
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EventId, GraphId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt, AuditSink, graph_stream
from graph_cli.kernel.ports.storage import (
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
from graph_cli.kernel.record_codec import RecordCodec
from graph_cli.kernel.records import BlobInfo, TxRecord
from graph_cli.kernel.sealing import BlobInvalid, KeyRing, KeyUnavailable
from graph_cli.storage.common.chain import ChainLink
from graph_cli.storage.common.commit import check_receipt, check_tx, seal_tx
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.sealed_blobs import sealed_blob_error
from graph_cli.storage.common.snapshot import ViewSnapshot, resolve_at_seq
from graph_cli.storage.common.view import GraphView
from graph_cli.storage.memory.audit_log import InMemoryAuditLog


class InMemoryStore:
    """One graph in memory. Thread-safe: one writer session at a time, lock-free readers."""

    def __init__(
        self,
        graph_id: GraphId,
        *,
        audit: InMemoryAuditLog | None = None,
        faults: FaultHook | None = None,
        ring: KeyRing | None = None,
    ) -> None:
        self.graph_id = graph_id
        self.codec = RecordCodec(graph_id, ring)
        self.audit = audit if audit is not None else InMemoryAuditLog(faults=faults)
        self._faults = faults
        self._stream = graph_stream(str(graph_id))
        self._view = GraphView()
        self._head = EMPTY_HEAD
        self._lock = threading.Lock()
        self._blobs: dict[str, bytes] = {}
        self._lines: list[bytes] = []  # the sealed tx lines, to open again after a shred
        self._generation = self.codec.ring.generation

    # ---- read port -----------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_SNAPSHOT_AT_SEQ})

    def head(self) -> Head:
        return self._head

    def key_ring(self) -> KeyRing:
        return self.codec.ring

    def snapshot(self, *, at_seq: int | None = None) -> ViewSnapshot:
        self._refresh_keys()
        return ViewSnapshot(self._view.at(resolve_at_seq(at_seq, self._head.seq)))

    def changes_since(self, seq: int, *, limit: int = 1000) -> Sequence[TxRecord]:
        return self._view.at(self._head.seq).txs_since(seq, limit=limit)

    def blob(self, digest: str) -> BlobInfo | None:
        data = self._blobs.get(digest)
        if data is None:
            return None
        if self.codec.sealed:
            from graph_cli.storage.common import crypto

            return BlobInfo(digest, crypto.blob_content_size(len(data)))
        return BlobInfo(digest, len(data))

    def read_blob(self, digest: str) -> Iterator[bytes]:
        data = self._blobs.get(digest)
        if data is None:
            raise GraphCliError(
                "backend.corrupt", "a stored file is missing", details={"blob": digest}
            )
        if not self.codec.sealed:
            return iter((data,))
        try:
            # whole in memory and authenticated before the first chunk is handed out
            content = b"".join(self.codec.ring.open_blob(io.BytesIO(data)))
        except (KeyUnavailable, BlobInvalid) as exc:
            raise sealed_blob_error(digest, exc) from None
        return iter((content,))

    def blob_local_path(self, digest: str) -> Path | None:
        return None  # no capability blob_local_path: the bytes live in memory

    def stage_blob(self, chunks: Iterable[bytes], *, max_bytes: int) -> MemoryStagedBlob:
        parts: list[bytes] = []
        size = 0
        for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise GraphCliError(
                    "file.too_large",
                    f"the file is larger than {max_bytes} bytes",
                    details={"max_bytes": max_bytes},
                )
            parts.append(chunk)
        content = b"".join(parts)
        stored = content
        if self.codec.sealed:
            kid = self.codec.ring.file_key()
            assert kid is not None
            stored = b"".join(self.codec.ring.seal_blob(kid, (content,)))
        fire(self._faults, CrashPoint.BLOB_AFTER_STAGE)
        return MemoryStagedBlob(content, stored, self)

    # ---- write port ----------------------------------------------------

    @contextmanager
    def session(self, *, timeout_s: float) -> Iterator[MemorySession]:
        if not self._lock.acquire(timeout=max(timeout_s, 0.0)):
            raise GraphCliError(
                "backend.lock_timeout",
                f"lock not acquired within {timeout_s} s",
                details={"timeout_ms": round(timeout_s * 1000)},
            )
        self._refresh_keys()
        session = MemorySession(self)
        try:
            yield session
        finally:
            session.close()
            self._lock.release()

    def _refresh_keys(self) -> None:
        """After a shred the view holds records opened under keys that are gone: open the
        stored lines again (T16)."""
        ring = self.codec.ring
        ring.refresh()
        if ring.generation == self._generation:
            return
        view = GraphView()
        for line in self._lines:
            view.apply(self.codec.parse_tx(as_object(parse_json(line), "tx")))
        self._view, self._generation = view, ring.generation

    def _apply(self, record: TxRecord) -> None:
        self._view.apply(record)
        self._head = Head(record.seq, record.tx_id, record.hash, record.recorded_at)


class MemoryStagedBlob:
    """Staged bytes of one ``InMemoryStore``: the content, held as ``data`` (its ciphertext
    in a sealed graph)."""

    def __init__(self, content: bytes, data: bytes, store: InMemoryStore) -> None:
        self.data: bytes | None = data
        self.store = store
        self.hash = "sha256:" + hashlib.sha256(content).hexdigest()
        self.size = len(content)
        sealed = data is not content
        self.stored = "sha256:" + hashlib.sha256(data).hexdigest() if sealed else self.hash
        self.stored_size = len(data)

    def discard(self) -> None:
        self.data = None


class MemorySession:
    """``WriteSession`` of ``InMemoryStore``; valid only inside ``session()``."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self._token = object()
        self._used: set[EventId] = set()
        self._open = True

    def close(self) -> None:
        self._open = False

    def _check_open(self) -> None:
        if not self._open:
            raise GraphCliError("internal.error", "the write session is closed")

    def snapshot(self) -> ViewSnapshot:
        self._check_open()
        return self._store.snapshot()

    def head(self) -> Head:
        self._check_open()
        return self._store.head()

    def alias_counters(self) -> Mapping[str, int]:
        self._check_open()
        return self._store._view.at(self._store.head().seq).alias_counters()

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None:
        self._check_open()
        view = self._store._view.at(self._store.head().seq)
        return view.lookup_idempotency(key, principal_id)

    def append_audit(self, event: AuditEventDraft) -> AuditReceipt:
        self._check_open()
        store = self._store
        return store.audit.append(store._stream, event, session_token=self._token)

    def publish_blob(self, staged: StagedBlob) -> BlobInfo:
        self._check_open()
        store = self._store
        if not isinstance(staged, MemoryStagedBlob) or staged.store is not store:
            raise GraphCliError("internal.error", "the staged blob is not from this store")
        if staged.stored not in store._blobs:
            if staged.data is None:
                raise GraphCliError("internal.error", "the staged blob was discarded")
            try:
                fire(store._faults, CrashPoint.BLOB_BEFORE_RENAME)
            except OSError as exc:
                raise GraphCliError("backend.unavailable", f"cannot store the file: {exc}") from exc
            store._blobs[staged.stored] = staged.data
            fire(store._faults, CrashPoint.BLOB_AFTER_RENAME)
        staged.discard()
        return BlobInfo(staged.stored, staged.stored_size)

    def commit(self, tx: AuthorizedTx, receipt: AuditReceipt) -> CommitResult:
        self._check_open()
        store = self._store
        head = store.head()
        check_receipt(
            receipt,
            event_id=tx.event_id,
            stream=store._stream,
            session_token=self._token,
            used_events=self._used,
        )
        check_tx(
            tx,
            graph_id=store.graph_id,
            head=head,
            view=store._view.at(head.seq),
            blob_exists=store._blobs.__contains__,
            codec=store.codec,
        )
        record, line = seal_tx(tx, ChainLink(head.seq, head.hash), store.codec)
        try:
            fire(store._faults, CrashPoint.TX_BEFORE_WRITE)
            fire(store._faults, CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC)
        except OSError as exc:
            raise GraphCliError("backend.unavailable", f"tx append failed: {exc}") from exc
        store._apply(record)
        store._lines.append(line)
        self._used.add(receipt.event_id)
        warnings: list[WarningRecord] = []
        try:
            fire(store._faults, CrashPoint.TX_AFTER_SYNC)
        except OSError as exc:
            warnings.append(
                WarningRecord("storage.materialization_deferred", f"after commit: {exc}")
            )
        return CommitResult(record.seq, record.tx_id, record.hash, tuple(warnings))


if TYPE_CHECKING:  # static proof that the adapter implements the ports

    def _conforms(
        store: InMemoryStore,
        session: MemorySession,
        snapshot: ViewSnapshot,
        audit: InMemoryAuditLog,
    ) -> tuple[GraphStoreRead, GraphStoreWrite, WriteSession, Snapshot, AuditSink]:
        return store, store, session, snapshot, audit
