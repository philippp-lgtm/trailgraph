"""Audit sink port (contracts section 2, B5). All audit file I/O lives in storage."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import NewType, Protocol

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EventId

AuditStream = NewType("AuditStream", str)  # "installation" | "graph:<graph_id>"

INSTALLATION_STREAM = AuditStream("installation")


def graph_stream(graph_id: str) -> AuditStream:
    return AuditStream(f"graph:{graph_id}")


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    stream: AuditStream
    body: Mapping[str, JsonValue]  # full event minus seq/prev_hash/hash; built by graph_cli.audit


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    stream: AuditStream
    event_id: EventId
    seq: int
    hash: str
    session_token: object  # ties the receipt to the session that appended it


@dataclass(frozen=True, slots=True)
class AuditLine:
    """One raw stream line for the verifier; ``data`` keeps its ``\\n`` unless the line is torn."""

    position: str  # opaque locator for findings, e.g. "<segment>:<offset>"
    data: bytes


class AuditSink(Protocol):
    def append_installation(self, event: AuditEventDraft) -> AuditReceipt:
        """Append to the installation stream under its own lock; durable on return."""
        ...

    def read(
        self, stream: AuditStream, *, from_seq: int = 1
    ) -> Iterator[Mapping[str, JsonValue]]: ...

    def read_lines(self, stream: AuditStream) -> Iterator[AuditLine]:
        """Every line in stream order, unparsed and unverified; never raises on content."""
        ...

    def head(self, stream: AuditStream) -> tuple[int, str]: ...
