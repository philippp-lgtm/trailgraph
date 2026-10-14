"""FileAuditLog specifics: torn tails, damage, segments, locking (contracts B5.3, B5.9, W.b)."""

import base64
import os
from collections.abc import Mapping
from pathlib import Path

import pytest
from support.clock import StepClock
from support.drafts import draft
from support.ids import SeededIdProvider
from support.stores import RecordingSyncer

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditStream
from graph_cli.storage.common.chain import GENESIS, scan
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.layout import segment_name
from graph_cli.storage.files.locking import acquire


class Env:
    def __init__(self, root: Path, **options: object) -> None:
        self.clock = StepClock()
        self.ids = SeededIdProvider(self.clock)
        self.notes: list[Mapping[str, JsonValue]] = []
        self.log = FileAuditLog(
            root,
            syncer=RecordingSyncer(),
            oplog=self.notes.append,
            **options,  # type: ignore[arg-type]
        )

    def append(self, count: int = 1) -> None:
        for _ in range(count):
            event_id = EventId(self.ids.new_id())
            self.log.append_installation(draft(INSTALLATION_STREAM, event_id, self.clock.now()))

    @property
    def segment(self) -> Path:
        return self.log.installation_dir / segment_name("events-", 1)


def test_reader_ignores_a_torn_tail(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.append(2)
    with open(env.segment, "ab") as handle:
        handle.write(b'{"schema":"graph_cli.au')
    assert [e["seq"] for e in env.log.read(INSTALLATION_STREAM)] == [1, 2]
    assert env.log.head(INSTALLATION_STREAM)[0] == 2


def test_next_append_truncates_a_torn_tail(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.append(2)
    good = env.segment.read_bytes()
    torn = b'{"schema":"graph_cli.au'
    with open(env.segment, "ab") as handle:
        handle.write(torn)
    env.append()
    data = env.segment.read_bytes()
    assert data.startswith(good) and torn not in data
    assert scan(data, GENESIS).end.seq == 3
    (note,) = env.notes
    assert note["event"] == "tail_truncated" and note["tail"] == "torn"
    assert base64.b64decode(str(note["removed_b64"])) == torn
    assert note["offset"] == len(good)


def test_invalid_last_line_is_truncated_too(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.append(1)
    with open(env.segment, "ab") as handle:
        handle.write(b"garbage\n")
    env.append()
    assert [e["seq"] for e in env.log.read(INSTALLATION_STREAM)] == [1, 2]
    assert env.notes[0]["tail"] == "invalid"


def test_interior_damage_refuses_appends_and_reads(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.append(3)
    lines = env.segment.read_bytes().splitlines(keepends=True)
    env.segment.write_bytes(lines[0] + b"not json\n" + lines[2])
    with pytest.raises(GraphCliError) as info:
        env.append()
    assert info.value.code == "audit.unavailable"
    with pytest.raises(GraphCliError) as info:
        list(env.log.read(INSTALLATION_STREAM))
    assert info.value.code == "backend.corrupt"
    assert env.log.head(INSTALLATION_STREAM)[0] == 1


def test_read_does_not_verify_the_chain(tmp_path: Path) -> None:
    """Chain checks are the audit verifier's job (item 4); read hands out documents."""
    env = Env(tmp_path)
    env.append(3)
    lines = env.segment.read_bytes().splitlines(keepends=True)
    env.segment.write_bytes(lines[0] + lines[2])
    assert [e["seq"] for e in env.log.read(INSTALLATION_STREAM)] == [1, 3]
    assert env.log.head(INSTALLATION_STREAM)[0] == 1


def test_segments_roll_and_read_across(tmp_path: Path) -> None:
    env = Env(tmp_path, segment_bytes=1)
    env.append(4)
    names = sorted(os.listdir(env.log.installation_dir))
    assert names == [".lock", *(segment_name("events-", n) for n in range(1, 5))]
    assert [e["seq"] for e in env.log.read(INSTALLATION_STREAM, from_seq=3)] == [3, 4]
    fresh = FileAuditLog(tmp_path, segment_bytes=1)
    assert fresh.head(INSTALLATION_STREAM) == env.log.head(INSTALLATION_STREAM)


def test_two_instances_continue_one_chain(tmp_path: Path) -> None:
    first, second = Env(tmp_path), Env(tmp_path)
    first.append()
    second.append()
    first.append()
    data = first.segment.read_bytes()
    assert scan(data, GENESIS).end.seq == 3


def test_lock_timeout_on_the_installation_lock(tmp_path: Path) -> None:
    env = Env(tmp_path, lock_timeout_s=0.05)
    env.append()
    with acquire(env.log.installation_dir / ".lock", timeout_s=1):
        with pytest.raises(GraphCliError) as info:
            env.append()
        assert info.value.code == "backend.lock_timeout"
    env.append()
    assert env.log.head(INSTALLATION_STREAM)[0] == 2


def test_unknown_stream_names(tmp_path: Path) -> None:
    log = FileAuditLog(tmp_path)
    for stream in ("graph:NOT-A-UUID", "graphs", "graph:../x"):
        with pytest.raises(GraphCliError) as info:
            log.head(AuditStream(stream))
        assert info.value.code == "internal.error"


def test_reads_create_nothing(tmp_path: Path) -> None:
    log = FileAuditLog(tmp_path / "root")
    assert list(log.read(INSTALLATION_STREAM)) == []
    assert log.head(INSTALLATION_STREAM) == (GENESIS.seq, GENESIS.hash)
    assert not (tmp_path / "root").exists()
