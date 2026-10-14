"""In-memory audit streams: the ``AuditSink`` of the reference backend (contracts B5)."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping

from graph_cli.kernel.canonical import GENESIS_HASH, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditReceipt,
    AuditStream,
)
from graph_cli.storage.common.audit import audit_body
from graph_cli.storage.common.chain import ChainLink, link_of, seal
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire


class InMemoryAuditLog:
    """Every stream of one installation. Graph streams are appended by ``InMemoryStore``."""

    def __init__(self, *, faults: FaultHook | None = None) -> None:
        self._faults = faults
        self._streams: dict[AuditStream, list[Mapping[str, JsonValue]]] = {}
        self._lock = threading.Lock()

    def append_installation(self, event: AuditEventDraft) -> AuditReceipt:
        with self._lock:
            return self.append(INSTALLATION_STREAM, event, session_token=object())

    def append(
        self, stream: AuditStream, event: AuditEventDraft, *, session_token: object
    ) -> AuditReceipt:
        """Seal and append one event; the caller holds the stream's lock."""
        body, event_id = audit_body(event, stream)
        events = self._streams.setdefault(stream, [])
        document, _ = seal(body, self._link(stream))
        try:
            fire(self._faults, CrashPoint.AUDIT_BEFORE_WRITE)
            events.append(document)
            fire(self._faults, CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC)
            fire(self._faults, CrashPoint.AUDIT_AFTER_SYNC)
        except OSError as exc:
            if events and events[-1] is document:
                events.pop()  # an unacknowledged event is rolled back, as in FileStore
            raise GraphCliError(
                "audit.unavailable", f"audit append failed: {exc}", details={"stream": stream}
            ) from exc
        link = link_of(document)
        return AuditReceipt(stream, event_id, link.seq, link.hash, session_token)

    def _link(self, stream: AuditStream) -> ChainLink:
        events = self._streams.get(stream)
        return link_of(events[-1]) if events else ChainLink(0, GENESIS_HASH)

    def read(self, stream: AuditStream, *, from_seq: int = 1) -> Iterator[Mapping[str, JsonValue]]:
        events = list(self._streams.get(stream, ()))
        yield from events[max(from_seq, 1) - 1 :]

    def head(self, stream: AuditStream) -> tuple[int, str]:
        link = self._link(stream)
        return link.seq, link.hash
