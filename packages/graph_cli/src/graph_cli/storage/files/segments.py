"""Segmented chained JSONL logs: the tx log and audit streams (contracts B4.11, B5.9, C.3).

A log is a directory of segment files named by the seq of their first line
(12 digits, B4 open point). The chain runs across segments. The writer rolls
to a new segment once the current one reaches ``roll_bytes``. Only the last
segment may end in a tail (torn or invalid last line) or be empty.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.storage.common.chain import GENESIS, ChainLink, TailState, scan
from graph_cli.storage.files.durable import FILE_MODE, Syncer, write_all
from graph_cli.storage.files.layout import parse_segment_name, segment_name

_OPEN_APPEND: Final = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC


@dataclass(frozen=True, slots=True)
class LogPosition:
    segment: str
    offset: int


@dataclass(frozen=True, slots=True)
class LogRecord:
    document: JsonObject
    start: LogPosition
    end: LogPosition


@dataclass(frozen=True, slots=True)
class LogCorruption:
    segment: str
    offset: int
    expected_seq: int
    reason: str


@dataclass(frozen=True, slots=True)
class LogScan:
    records: tuple[LogRecord, ...]
    end: ChainLink
    position: LogPosition | None  # where the next line goes; None for an empty log
    tail: TailState
    tail_bytes: bytes  # the not-committed bytes after ``position`` (torn or invalid tail)
    corruption: LogCorruption | None


class SegmentedLog:
    def __init__(self, directory: Path, prefix: str, roll_bytes: int) -> None:
        self.directory = directory
        self.prefix = prefix
        self.roll_bytes = roll_bytes

    def segments(self) -> list[tuple[int, str]]:
        """``(first_seq, name)`` of every segment, in seq order."""
        found = []
        for name in os.listdir(self.directory):
            first = parse_segment_name(self.prefix, name)
            if first is not None:
                found.append((first, name))
        return sorted(found)

    def path(self, segment: str) -> Path:
        return self.directory / segment

    def scan(self, start: ChainLink = GENESIS, at: LogPosition | None = None) -> LogScan:
        """Verify the log from ``at`` (a line boundary after ``start``) or from the beginning."""
        segments = self.segments()
        if at is not None:
            names = [name for _, name in segments]
            if at.segment not in names:
                return _broken(start, at, "checkpoint segment is missing")
            segments = segments[names.index(at.segment) :]
        records: list[LogRecord] = []
        link = start
        position = at
        for index, (first_seq, name) in enumerate(segments):
            offset = at.offset if at is not None and index == 0 else 0
            data = self._read(name, offset)
            if data is None:
                return _broken(link, LogPosition(name, offset), "segment shorter than checkpoint")
            is_last = index == len(segments) - 1
            if offset == 0 and first_seq != link.seq + 1:
                if data or not is_last:
                    reason = f"segment {name} does not start at seq {link.seq + 1}"
                    return LogScan(
                        tuple(records),
                        link,
                        position,
                        "clean",
                        b"",
                        LogCorruption(name, 0, link.seq + 1, reason),
                    )
                break  # a stray empty last segment; the next append names its own
            result = scan(data, link, final=is_last)
            for line in result.lines:
                records.append(
                    LogRecord(
                        line.document,
                        LogPosition(name, offset + line.start),
                        LogPosition(name, offset + line.end),
                    )
                )
            link = result.end
            position = LogPosition(name, offset + result.good_end)
            if result.corruption is not None:
                damage = LogCorruption(
                    name,
                    offset + result.corruption.offset,
                    result.corruption.expected_seq,
                    result.corruption.reason,
                )
                return LogScan(tuple(records), link, position, "clean", b"", damage)
            if result.tail != "clean":
                return LogScan(
                    tuple(records), link, position, result.tail, data[result.good_end :], None
                )
        return LogScan(tuple(records), link, position, "clean", b"", None)

    def _read(self, segment: str, offset: int) -> bytes | None:
        with open(self.path(segment), "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size < offset:
                return None
            handle.seek(offset)
            return handle.read()

    def append(self, line: bytes, seq: int, end: LogPosition | None) -> PendingAppend:
        """Write ``line`` (seq ``seq``) after ``end``; sync with the returned handle.

        Rolls to a new segment named ``seq`` when the current one is full.
        """
        if end is None or end.offset >= self.roll_bytes:
            target = LogPosition(segment_name(self.prefix, seq), 0)
        else:
            target = end
        path = self.path(target.segment)
        created = not path.exists()
        fd = os.open(path, _OPEN_APPEND, FILE_MODE)
        pending = PendingAppend(self, fd, target, created)
        try:
            write_all(fd, line)
        except BaseException:
            pending.rollback_quietly()
            raise
        pending.end = LogPosition(target.segment, target.offset + len(line))
        return pending

    def truncate(self, position: LogPosition, syncer: Syncer) -> None:
        """Cut a segment back to ``position`` (a line boundary) and sync it."""
        fd = os.open(self.path(position.segment), os.O_WRONLY | os.O_CLOEXEC)
        try:
            os.ftruncate(fd, position.offset)
            syncer.sync_file(fd)
        finally:
            os.close(fd)

    def read_all(self) -> Sequence[tuple[str, bytes]]:
        return [(name, self.path(name).read_bytes()) for _, name in self.segments()]


class PendingAppend:
    """A written but not yet synced line. ``sync`` makes it durable; ``rollback`` removes it."""

    def __init__(self, log: SegmentedLog, fd: int, start: LogPosition, created: bool) -> None:
        self.log = log
        self.start = start
        self.end = start
        self.created = created
        self._fd: int | None = fd

    def sync(self, syncer: Syncer) -> None:
        assert self._fd is not None
        syncer.sync_file(self._fd)
        if self.created:
            syncer.sync_dir(self.log.directory)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def rollback(self, syncer: Syncer) -> None:
        """Truncate back to the start of this append (the line was never acknowledged)."""
        assert self._fd is not None
        os.ftruncate(self._fd, self.start.offset)
        syncer.sync_file(self._fd)
        self.close()

    def rollback_quietly(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, self.start.offset)
        self.close()


def _broken(link: ChainLink, at: LogPosition, reason: str) -> LogScan:
    return LogScan(
        (), link, at, "clean", b"", LogCorruption(at.segment, at.offset, link.seq + 1, reason)
    )
