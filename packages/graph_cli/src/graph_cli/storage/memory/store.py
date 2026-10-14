"""InMemoryStore: read and write ports over one live replay view (contracts B4, B13)."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EventId, GraphId
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditReceipt, AuditSink, graph_stream
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
from graph_cli.kernel.records import TxRecord
from graph_cli.storage.common.chain import ChainLink
from graph_cli.storage.common.commit import check_receipt, check_tx, seal_tx
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
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
    ) -> None:
        self.graph_id = graph_id
        self.audit = audit if audit is not None else InMemoryAuditLog(faults=faults)
        self._faults = faults
        self._stream = graph_stream(str(graph_id))
        self._view = GraphView()
        self._head = EMPTY_HEAD
        self._lock = threading.Lock()

    # ---- read port -----------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_SNAPSHOT_AT_SEQ})

    def head(self) -> Head:
        return self._head

    def snapshot(self, *, at_seq: int | None = None) -> ViewSnapshot:
        return ViewSnapshot(self._view.at(resolve_at_seq(at_seq, self._head.seq)))

    def changes_since(self, seq: int, *, limit: int = 1000) -> Sequence[TxRecord]:
        return self._view.at(self._head.seq).txs_since(seq, limit=limit)

    # ---- write port ----------------------------------------------------

    @contextmanager
    def session(self, *, timeout_s: float) -> Iterator[MemorySession]:
        if not self._lock.acquire(timeout=max(timeout_s, 0.0)):
            raise GraphCliError(
                "backend.lock_timeout",
                f"lock not acquired within {timeout_s} s",
                details={"timeout_ms": round(timeout_s * 1000)},
            )
        session = MemorySession(self)
        try:
            yield session
        finally:
            session.close()
            self._lock.release()

    def _apply(self, record: TxRecord) -> None:
        self._view.apply(record)
        self._head = Head(record.seq, record.tx_id, record.hash, record.recorded_at)


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
        check_tx(tx, graph_id=store.graph_id, head=head, view=store._view.at(head.seq))
        record, _ = seal_tx(tx, ChainLink(head.seq, head.hash))
        try:
            fire(store._faults, CrashPoint.TX_BEFORE_WRITE)
            fire(store._faults, CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC)
        except OSError as exc:
            raise GraphCliError("backend.unavailable", f"tx append failed: {exc}") from exc
        store._apply(record)
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
