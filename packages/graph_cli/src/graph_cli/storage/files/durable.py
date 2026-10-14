"""Durable file writes (contracts B13.9, W.14, W.15).

Everything that must survive a crash is written to a temp file, synced,
renamed into place, and followed by a sync of the parent directory. All
syncs go through a ``Syncer`` so tests can record their order or skip the
cost (``F_FULLFSYNC`` takes milliseconds on macOS).
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol

Durability = Literal["full", "fsync"]

# fcntl.F_FULLFSYNC exists on darwin only; the value is fixed by the macOS ABI.
_F_FULLFSYNC: Final[int] = getattr(fcntl, "F_FULLFSYNC", 51)
_FULLFSYNC_UNSUPPORTED: Final = frozenset({errno.EINVAL, errno.ENOTSUP, errno.ENOTTY})

TEMP_SUFFIX: Final = ".tmp"
FILE_MODE: Final = 0o644
DIR_MODE: Final = 0o755


class Syncer(Protocol):
    def sync_file(self, fd: int) -> None:
        """Flush one open file (or directory) to stable storage."""
        ...

    def sync_dir(self, path: Path) -> None:
        """Flush a directory's entries after a create, rename or unlink."""
        ...


class OsSyncer:
    """The production syncer.

    ``durability="full"`` uses ``F_FULLFSYNC`` on darwin, because ``fsync``
    there does not flush the drive cache; filesystems that refuse it fall back
    to ``fsync``. Everywhere else, and with ``"fsync"``, it uses ``os.fsync``.
    """

    def __init__(self, durability: Durability = "full", *, platform: str = sys.platform) -> None:
        self._full = durability == "full" and platform == "darwin"

    def sync_file(self, fd: int) -> None:
        if self._full:
            try:
                fcntl.fcntl(fd, _F_FULLFSYNC)
                return
            except OSError as exc:
                if exc.errno not in _FULLFSYNC_UNSUPPORTED:
                    raise
        os.fsync(fd)

    def sync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            self.sync_file(fd)
        finally:
            os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """``os.write`` until every byte is written."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def write_temp(path: Path, data: bytes, syncer: Syncer) -> Path:
    """Write ``data`` to ``<path>.tmp`` and sync it; leftovers of a crash are overwritten.

    Writers hold the graph lock, so the temp name needs no randomness.
    """
    temp = path.with_name(path.name + TEMP_SUFFIX)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, FILE_MODE)
    try:
        write_all(fd, data)
        syncer.sync_file(fd)
    finally:
        os.close(fd)
    return temp


def ensure_dir(path: Path, syncer: Syncer) -> bool:
    """Create ``path`` and missing parents, syncing each new entry's parent.

    Returns whether anything was created.
    """
    missing: list[Path] = []
    current = path
    while not current.is_dir():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, DIR_MODE)
        except FileExistsError:
            continue
        syncer.sync_dir(directory.parent)
    return bool(missing)


def write_tree(root: Path, files: Mapping[str, bytes], syncer: Syncer) -> None:
    """Write ``files`` (relative POSIX paths) below a fresh ``root``; sync files and dirs.

    ``root`` is a private staging directory, so files are written in place;
    the caller publishes it with one rename.
    """
    directories = {root}
    ensure_dir(root, syncer)
    for name in sorted(files):
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"not a relative file name: {name!r}")
        path = root.joinpath(*parts)
        ensure_dir(path.parent, syncer)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
        try:
            write_all(fd, files[name])
            syncer.sync_file(fd)
        finally:
            os.close(fd)
        directories.add(path.parent)
    for directory in sorted(directories):
        syncer.sync_dir(directory)
