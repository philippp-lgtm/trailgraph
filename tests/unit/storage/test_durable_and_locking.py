"""Durable writes and the flock wrapper (contracts B13.8, B13.9, W.c)."""

import errno
import os
import threading
from pathlib import Path

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files import durable
from graph_cli.storage.files.durable import OsSyncer, ensure_dir, write_all, write_temp
from graph_cli.storage.files.locking import acquire


class RecordingSyncer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def sync_file(self, fd: int) -> None:
        self.calls.append(("file", str(fd)))

    def sync_dir(self, path: Path) -> None:
        self.calls.append(("dir", str(path)))


def test_full_durability_uses_f_fullfsync_on_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(durable.fcntl, "fcntl", lambda fd, cmd: calls.append(cmd))
    monkeypatch.setattr(durable.os, "fsync", lambda fd: pytest.fail("fsync must not run"))
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_WRONLY)
    try:
        OsSyncer("full", platform="darwin").sync_file(fd)
    finally:
        os.close(fd)
    assert calls == [durable._F_FULLFSYNC]


@pytest.mark.parametrize(
    ("durability", "platform"), [("fsync", "darwin"), ("full", "linux"), ("fsync", "linux")]
)
def test_other_settings_use_fsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durability: durable.Durability, platform: str
) -> None:
    synced: list[int] = []
    monkeypatch.setattr(durable.fcntl, "fcntl", lambda fd, cmd: pytest.fail("no F_FULLFSYNC"))
    monkeypatch.setattr(durable.os, "fsync", synced.append)
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_WRONLY)
    try:
        OsSyncer(durability, platform=platform).sync_file(fd)
    finally:
        os.close(fd)
    assert synced == [fd]


def test_unsupported_f_fullfsync_falls_back_to_fsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(fd: int, cmd: int) -> None:
        raise OSError(errno.ENOTSUP, "not supported")

    synced: list[int] = []
    monkeypatch.setattr(durable.fcntl, "fcntl", refuse)
    monkeypatch.setattr(durable.os, "fsync", synced.append)
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_WRONLY)
    try:
        OsSyncer("full", platform="darwin").sync_file(fd)
    finally:
        os.close(fd)
    assert synced == [fd]


def test_real_syncer_syncs_files_and_dirs(tmp_path: Path) -> None:
    syncer = OsSyncer()
    temp = write_temp(tmp_path / "doc.json", b"{}\n", syncer)
    os.replace(temp, tmp_path / "doc.json")
    syncer.sync_dir(tmp_path)
    assert (tmp_path / "doc.json").read_bytes() == b"{}\n"


def test_write_temp_overwrites_leftovers_and_syncs(tmp_path: Path) -> None:
    (tmp_path / "doc.json.tmp").write_bytes(b"leftover from a crash, longer than the data")
    syncer = RecordingSyncer()
    temp = write_temp(tmp_path / "doc.json", b"new", syncer)
    assert temp.name == "doc.json.tmp"
    assert temp.read_bytes() == b"new"
    assert [kind for kind, _ in syncer.calls] == ["file"]


def test_ensure_dir_syncs_each_new_parent(tmp_path: Path) -> None:
    syncer = RecordingSyncer()
    assert ensure_dir(tmp_path / "a" / "b", syncer)
    assert syncer.calls == [("dir", str(tmp_path)), ("dir", str(tmp_path / "a"))]
    assert not ensure_dir(tmp_path / "a" / "b", syncer)
    assert len(syncer.calls) == 2


def test_write_all_handles_short_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_write = os.write
    monkeypatch.setattr(durable.os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_WRONLY)
    try:
        write_all(fd, b"0123456789")
    finally:
        os.close(fd)
    assert (tmp_path / "f").read_bytes() == b"0123456789"


def test_lock_conflicts_between_threads(tmp_path: Path) -> None:
    lock = tmp_path / ".lock"
    held = acquire(lock, timeout_s=1)
    outcome: list[str] = []

    def contend() -> None:
        try:
            acquire(lock, timeout_s=0.05).release()
            outcome.append("acquired")
        except GraphCliError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=contend)
    thread.start()
    thread.join()
    assert outcome == ["backend.lock_timeout"]
    held.release()
    with acquire(lock, timeout_s=0.05) as again:
        assert again.held
    assert not again.held


def test_closing_another_descriptor_keeps_the_lock(tmp_path: Path) -> None:
    """The POSIX record-lock trap does not apply to flock."""
    lock = tmp_path / ".lock"
    with acquire(lock, timeout_s=1):
        os.close(os.open(lock, os.O_RDONLY))
        with pytest.raises(GraphCliError) as info:
            acquire(lock, timeout_s=0)
        assert info.value.code == "backend.lock_timeout"
        assert info.value.retryable


def test_lock_file_is_never_deleted(tmp_path: Path) -> None:
    lock = tmp_path / ".lock"
    acquire(lock, timeout_s=0).release()
    assert lock.exists()
    assert lock.stat().st_mode & 0o777 == 0o644 & ~_umask()


def test_missing_lock_directory_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(GraphCliError) as info:
        acquire(tmp_path / "missing" / ".lock", timeout_s=0)
    assert info.value.code == "backend.unavailable"


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current
