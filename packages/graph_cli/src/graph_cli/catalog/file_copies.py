"""Copies of stored files on this machine: ``<state root>/files/<graph id>/<hex>``.

The remote client downloads a stored file here for ``file path``; locally, a
sealed graph (T16) decrypts one here, because its stored files hold
ciphertext. A copy is named by its content hash, written to a private
partial file, checked against the hash, made read-only (0400) and renamed
into place; a copy that is there and has its hash is used again. Copies are
plaintext outside the data root, which a purge reaches only on the machine
and state root it runs with (``drop``): each use removes copies of the same
graph not used for ``KEEP_SECONDS`` (a use renews the time).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO, Final

from graph_cli.kernel.clock import Clock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.services.files import hash_file

KEEP_SECONDS: Final = 24 * 3600
COPY_MODE: Final = 0o400
_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_PARTIAL: Final = ".partial"


class FileCopyDir:
    """``services.files.FileCopies`` under ``<state root>/files``."""

    def __init__(self, root: Path, clock: Clock) -> None:
        self.root = root
        self.clock = clock

    def get(
        self,
        graph: str,
        digest: str,
        write: Callable[[BinaryIO], None],
        mismatch: Callable[[], GraphCliError],
    ) -> Path:
        hex_digest = digest.removeprefix("sha256:")
        if not _HEX_RE.match(hex_digest) or "/" in graph or graph in ("", ".", ".."):
            raise GraphCliError("internal.error", f"not a file copy name: {graph}/{digest}")
        directory = self.root / graph
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._prune(directory)
        target = directory / hex_digest
        if hash_file(target) == digest:
            os.utime(target)  # in use: kept another KEEP_SECONDS
            return target
        partial = directory / f".{hex_digest}.{secrets.token_hex(4)}{_PARTIAL}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            with os.fdopen(os.open(partial, flags, 0o600), "wb") as sink:
                write(sink)
            if hash_file(partial) != digest:
                raise mismatch()
            os.chmod(partial, COPY_MODE)
            os.replace(partial, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
        return target

    def drop(self, graph: str, digests: Iterable[str]) -> int:
        """Remove the copies of ``digests`` (a purge, T16); returns how many went."""
        removed = 0
        for digest in digests:
            hex_digest = digest.removeprefix("sha256:")
            if not _HEX_RE.match(hex_digest) or "/" in graph or graph in ("", ".", ".."):
                raise GraphCliError("internal.error", f"not a file copy name: {graph}/{digest}")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.root / graph / hex_digest)
                removed += 1
        return removed

    def _prune(self, directory: Path) -> None:
        cutoff = self.clock.now().timestamp() - KEEP_SECONDS
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            copy = _HEX_RE.match(name) or (name.startswith(".") and name.endswith(_PARTIAL))
            try:
                if (
                    copy
                    and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ):
                    os.unlink(entry.path)
            except OSError:
                continue  # gone meanwhile, or not ours to remove
