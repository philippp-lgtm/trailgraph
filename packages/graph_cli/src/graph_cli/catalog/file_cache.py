"""This machine's file cache: read-only copies of stored files (object storage O8).

``<state>/files/<graph id>/<hex>`` holds the copies ``graph file path`` answers when the
stored file has no local path here: the remote client's downloads, and in local mode the
files of a graph whose stored files are in an object store. One implementation of
``services.files.FileCache`` for both, with these rules:

- A copy is written to a unique ``.<hex>.partial-<random>`` file (two processes may fetch
  the same file at once), hash-checked, made read-only (mode 0400) and renamed into
  place; a partial file never becomes a copy, and is removed when the fetch fails.
- A copy is checked against its hash on every use (a changed copy is fetched again) and
  its modification time is set to now (``os.utime``): the time it was last used.
- ``prune`` removes the copies and partial files not used for ``UNUSED_FOR`` (a day);
  ``file path`` calls it, so a copy nobody needs does not stay on the machine.

Directories are created with mode 0700. Nothing here is authoritative: the cache can be
deleted at any time.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Final

from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.services.files import hash_file

UNUSED_FOR: Final = timedelta(hours=24)
COPY_MODE: Final = 0o400
_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_RE: Final = re.compile(r"^(?:[0-9a-f]{64}|\.[0-9a-f]{64}\.partial.*)$")


def _mismatch(digest: str) -> GraphCliError:
    return GraphCliError(
        "backend.corrupt",
        "the copy of a stored file does not have its hash",
        details={"blob": digest},
    )


class StateFileCache:
    """``<state>/files``; ``clock`` sets the use times and decides what expired."""

    def __init__(self, state_root: Path, clock: Clock | None = None) -> None:
        self.root = state_root / "files"
        self._clock: Clock = clock or SystemClock()

    def path(self, graph: str, digest: str) -> Path:
        return self.root / graph / digest.removeprefix("sha256:")

    def prune(self) -> None:
        """Remove copies and partial files whose last use is ``UNUSED_FOR`` ago or more,
        and graph directories left empty; whatever cannot be read or removed stays."""
        oldest = (self._clock.now() - UNUSED_FOR).timestamp()
        try:
            graphs = list(os.scandir(self.root))
        except OSError:
            return
        for graph in graphs:
            with contextlib.suppress(OSError):
                if not graph.is_dir(follow_symlinks=False):
                    continue
                for entry in os.scandir(graph.path):
                    if not _ENTRY_RE.match(entry.name):
                        continue
                    with contextlib.suppress(OSError):
                        if entry.is_file(follow_symlinks=False) and (
                            entry.stat(follow_symlinks=False).st_mtime <= oldest
                        ):
                            os.unlink(entry.path)
                with contextlib.suppress(OSError):
                    os.rmdir(graph.path)  # only when empty

    def copy(
        self,
        graph: str,
        digest: str,
        fill: Callable[[BinaryIO], None],
        *,
        mismatch: Callable[[], GraphCliError] | None = None,
    ) -> Path:
        hexdigest = digest.removeprefix("sha256:")
        if not _HEX_RE.match(hexdigest) or "/" in graph or graph in ("", ".", ".."):
            raise GraphCliError("internal.error", "a file cache entry needs a graph id and hash")
        target = self.path(graph, digest)
        if self._use(target, digest):
            return target
        directory = target.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = directory / f".{hexdigest}.partial-{secrets.token_hex(8)}"
        try:
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as sink:
                fill(sink)
            if hash_file(partial) != digest:
                raise (mismatch or (lambda: _mismatch(digest)))()
            os.chmod(partial, COPY_MODE)
            os.replace(partial, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
        self._touch(target)
        return target

    def _use(self, target: Path, digest: str) -> bool:
        """Whether the copy is there with its hash; its use time is set first, so a
        concurrent ``prune`` leaves it."""
        try:
            self._touch(target)
        except FileNotFoundError:
            return False
        if hash_file(target) != digest:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            return False
        if target.stat().st_mode & 0o777 != COPY_MODE:
            os.chmod(target, COPY_MODE)  # a copy of an older version (0444)
        return True

    def _touch(self, target: Path) -> None:
        now = self._clock.now().timestamp()
        os.utime(target, (now, now))
