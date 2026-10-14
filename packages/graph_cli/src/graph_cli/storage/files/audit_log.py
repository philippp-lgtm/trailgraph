"""FileAuditLog: the installation audit stream and readers for every stream (contracts B5, W.b).

The installation stream lives in ``<data_root>/audit/`` with its own
``audit/.lock``, held only for one append. Graph streams live in
``<data_root>/graphs/<graph_id>/audit/`` and are appended only through a
FileStore write session under the graph lock.

``read`` yields the parsed documents of all complete lines without checking
the chain: verification belongs to ``graph_cli.audit``. A torn or
unparseable last line is a tail and ignored (B5.9, as in ``head``); an
unparseable interior line is ``backend.corrupt``. ``read_lines`` hands the
raw lines to the verifier, which reports damage as findings. ``head`` is the
last chain-verified position.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditLine,
    AuditReceipt,
    AuditSink,
    AuditStream,
)
from graph_cli.storage.common.audit import audit_body
from graph_cli.storage.common.chain import GENESIS, ChainLink, link_of, seal
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.files.durable import Durability, OsSyncer, Syncer, ensure_dir
from graph_cli.storage.files.layout import AUDIT_SEGMENT_PREFIX, DEFAULT_SEGMENT_BYTES
from graph_cli.storage.files.locking import acquire
from graph_cli.storage.files.segments import (
    LogPosition,
    LogScan,
    OpLog,
    PendingAppend,
    SegmentedLog,
    truncation_note,
)

_GRAPH_STREAM_RE: Final = re.compile(
    r"^graph:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


class FileAuditLog:
    def __init__(
        self,
        data_root: Path,
        *,
        durability: Durability = "full",
        syncer: Syncer | None = None,
        faults: FaultHook | None = None,
        lock_timeout_s: float = 10.0,
        segment_bytes: int = DEFAULT_SEGMENT_BYTES,
        oplog: OpLog | None = None,
    ) -> None:
        self.data_root = data_root
        self.syncer: Syncer = syncer if syncer is not None else OsSyncer(durability)
        self.faults = faults
        self.lock_timeout_s = lock_timeout_s
        self.segment_bytes = segment_bytes
        self.oplog = oplog
        self._resume: tuple[ChainLink, LogPosition | None] | None = None

    @property
    def installation_dir(self) -> Path:
        return self.data_root / "audit"

    def _directory(self, stream: AuditStream) -> Path:
        if stream == INSTALLATION_STREAM:
            return self.installation_dir
        match = _GRAPH_STREAM_RE.match(stream)
        if match is None:
            raise GraphCliError("internal.error", f"unknown audit stream {stream!r}")
        return self.data_root / "graphs" / match.group(1) / "audit"

    def _log(self, stream: AuditStream) -> SegmentedLog:
        return SegmentedLog(self._directory(stream), AUDIT_SEGMENT_PREFIX, self.segment_bytes)

    # ---- AuditSink -----------------------------------------------------

    def append_installation(self, event: AuditEventDraft) -> AuditReceipt:
        body, event_id = audit_body(event, INSTALLATION_STREAM)
        directory = self.installation_dir
        try:
            ensure_dir(directory, self.syncer)
        except OSError as exc:
            raise _audit_unavailable("cannot create the audit directory", exc) from exc
        with acquire(directory / ".lock", timeout_s=self.lock_timeout_s):
            log = self._log(INSTALLATION_STREAM)
            result = self._writer_scan(log)
            document, line = seal(body, result.end)
            pending: PendingAppend | None = None
            try:
                fire(self.faults, CrashPoint.AUDIT_BEFORE_WRITE)
                pending = log.append(line, result.end.seq + 1, result.position)
                fire(self.faults, CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC)
                pending.sync(self.syncer)
                fire(self.faults, CrashPoint.AUDIT_AFTER_SYNC)
            except OSError as exc:
                self._resume = None
                if pending is not None:
                    pending.rollback_quietly()
                raise _audit_unavailable("installation audit append failed", exc) from exc
            pending.close()
        link = link_of(document)
        self._resume = (link, pending.end)
        return AuditReceipt(INSTALLATION_STREAM, event_id, link.seq, link.hash, object())

    def _writer_scan(self, log: SegmentedLog) -> LogScan:
        """Find the end of the installation chain; truncate a torn or invalid tail (B5.9)."""
        try:
            result = None
            if self._resume is not None:
                result = log.scan(self._resume[0], at=self._resume[1])
                if result.corruption is not None:
                    result = None
            if result is None:
                result = log.scan()
            if result.corruption is not None:
                raise GraphCliError(
                    "audit.unavailable",
                    "the installation audit stream is damaged",
                    details={
                        "segment": result.corruption.segment,
                        "offset": result.corruption.offset,
                    },
                )
            if result.tail != "clean":
                assert result.position is not None
                note = truncation_note(log, result, stream=INSTALLATION_STREAM)
                log.truncate(result.position, self.syncer)
                if self.oplog is not None:
                    self.oplog(note)
            return result
        except OSError as exc:
            raise _audit_unavailable("cannot read the installation audit stream", exc) from exc

    def read(self, stream: AuditStream, *, from_seq: int = 1) -> Iterator[Mapping[str, JsonValue]]:
        log = self._log(stream)
        if not log.directory.is_dir():
            return
        segments = log.segments()
        for index, (_, name) in enumerate(segments):
            last_segment = index == len(segments) - 1
            data = log.path(name).read_bytes()
            lines = data.split(b"\n")
            complete, rest = lines[:-1], lines[-1]
            for number, line in enumerate(complete):
                is_tail = last_segment and not rest and number == len(complete) - 1
                document = _parse(line, stream, name, number, tail=is_tail)
                if document is None:
                    return
                seq = document.get("seq")
                if not isinstance(seq, int) or seq >= from_seq:
                    yield document
            if rest and not last_segment:
                raise GraphCliError(
                    "backend.corrupt",
                    "audit segment ends in a torn line",
                    details={"stream": stream, "segment": name},
                )

    def read_lines(self, stream: AuditStream) -> Iterator[AuditLine]:
        log = self._log(stream)
        if not log.directory.is_dir():
            return
        for _, name in log.segments():
            offset = 0
            for line in log.path(name).read_bytes().splitlines(keepends=True):
                yield AuditLine(f"{name}:{offset}", line)
                offset += len(line)

    def head(self, stream: AuditStream) -> tuple[int, str]:
        log = self._log(stream)
        if not log.directory.is_dir():
            return GENESIS.seq, GENESIS.hash
        end = log.scan().end
        return end.seq, end.hash


def _parse(
    line: bytes, stream: str, segment: str, number: int, *, tail: bool
) -> Mapping[str, JsonValue] | None:
    """Parse one complete line; an unreadable last line is a tail (``None``), else corrupt."""
    try:
        document = parse_json(line)
    except CanonicalError as exc:
        document = None
        reason = str(exc)
    else:
        reason = "not a JSON object"
    if not isinstance(document, Mapping):
        if tail:
            return None
        raise GraphCliError(
            "backend.corrupt",
            f"audit line is unreadable: {reason}",
            details={"stream": stream, "segment": segment, "line": number + 1},
        )
    return document


def _audit_unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("audit.unavailable", f"{message}: {exc.strerror or exc}")


if TYPE_CHECKING:

    def _conforms(log: FileAuditLog) -> AuditSink:
        return log
