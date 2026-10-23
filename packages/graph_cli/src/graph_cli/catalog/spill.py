"""Files for JSON answers too large for stdout (``[output] spill_bytes``).

``cli_kit.runner`` decides when an answer spills; this module owns where it
goes. The default directory is ``/tmp/graph_cli_<uid>``: short paths an
agent can read, cleaned by the OS, and per user, because a fixed shared
name in ``/tmp`` can be created or symlinked by another user first. The
directory must be a real directory owned by the user and closed to group
and others, else nothing is written (``OSError``, the runner then prints
the answer). Files live in ``<dir>/<graph>/`` (``<dir>/`` without a graph),
are named ``<command>_<UTC time>_<8 hex>.json``, created exclusively with
mode 0600; each spill removes files older than ``KEEP_SECONDS`` from the
same directory.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from datetime import datetime
from pathlib import Path
from typing import Final

KEEP_SECONDS: Final = 24 * 3600
SUFFIX: Final = ".json"


def default_spill_dir() -> Path:
    return Path("/tmp") / f"graph_cli_{os.getuid()}"


def _private_dir(path: Path) -> None:
    """Create ``path`` (0700) or check that it is the user's own closed directory."""
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{path} is not a directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"{path} is not a private directory of this user")


class SpillFiles:
    def __init__(self, root: Path, now: datetime) -> None:
        self.root = root
        self.now = now

    def write(self, graph: str | None, command: str | None, content: bytes) -> str:
        """Write ``content`` to a new file and return its path."""
        self.root.parent.mkdir(parents=True, exist_ok=True)
        _private_dir(self.root)
        directory = self.root if graph is None else self.root / graph
        if graph is not None:
            _private_dir(directory)
        self._prune(directory)
        words = (command or "graph").replace(" ", "_")
        stamp = self.now.strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{words}_{stamp}_{secrets.token_hex(4)}{SUFFIX}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
        return str(path)

    def _prune(self, directory: Path) -> None:
        cutoff = self.now.timestamp() - KEEP_SECONDS
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            try:
                if (
                    entry.name.endswith(SUFFIX)
                    and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ):
                    os.unlink(entry.path)
            except OSError:
                continue  # best effort: another process may have removed it
