"""Uploaded bytes for file parameters (contracts B14.6).

``PUT /v1/uploads`` streams the body into ``<state_root>/uploads/<token>/``
while hashing it and counting it against the limit; the answer names it by
its SHA-256. Uploads are kept per token, so a hash names only what that
token sent. A command that uses one gets a hard link in a request
directory, named after the client's argument (``file add`` keeps the file
name), and the upload is removed after the command. Uploads that nobody
used are removed after an hour, on the next upload. A token's pending
uploads may hold at most ``PENDING_UPLOADS`` times the size limit, so one
token cannot fill the state directory.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Final

from graph_cli.kernel.errors import GraphCliError

UPLOADS_DIR: Final = "uploads"
MAX_AGE_S: Final = 3600.0
CHUNK: Final = 1024 * 1024
DIGEST_RE: Final = re.compile(r"^sha256:([0-9a-f]{64})$")
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
_PARTIAL: Final = ".partial-"
_REQUEST: Final = "request-"
PENDING_UPLOADS: Final = 4  # a token's staged bytes: at most this many uploads of full size


class Uploads:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root / UPLOADS_DIR

    def store(self, token: str, body: BinaryIO, length: int, max_bytes: int) -> tuple[str, int]:
        """Stage ``length`` bytes of ``body``; ``(sha256:<hex>, size)``."""
        if length > max_bytes:
            raise _too_large(max_bytes)
        directory = self._dir(token)
        pending = sum(entry.stat().st_size for entry in directory.iterdir() if entry.is_file())
        if pending + length > PENDING_UPLOADS * max_bytes:
            raise GraphCliError(
                "file.too_large",
                f"this token's pending uploads would exceed {PENDING_UPLOADS * max_bytes} bytes;"
                " use or let expire the uploads first",
                details={"max_bytes": max_bytes},
            )
        partial = directory / f"{_PARTIAL}{secrets.token_hex(8)}"
        hasher, size = hashlib.sha256(), 0
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
            with os.fdopen(fd, "wb") as out:
                while size < length:
                    chunk = body.read(min(CHUNK, length - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    hasher.update(chunk)
                    out.write(chunk)
            if size != length:
                raise GraphCliError(
                    "usage.invalid_input",
                    f"the upload ended after {size} of {length} bytes",
                    details={"argument": "upload"},
                )
            digest = hasher.hexdigest()
            os.replace(partial, directory / digest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
        return f"sha256:{digest}", size

    def find(self, token: str, digest: str) -> Path | None:
        match = DIGEST_RE.match(digest)
        if match is None:
            return None
        path = self.root / token / match.group(1)
        return path if path.is_file() else None

    def remove(self, token: str, digest: str) -> None:
        found = self.find(token, digest)
        if found is not None:
            with contextlib.suppress(FileNotFoundError):
                found.unlink()

    @contextlib.contextmanager
    def request_dir(self, token: str) -> Iterator[Path]:
        """A directory for the hard links of one request, removed afterwards."""
        directory = self._dir(token) / f"{_REQUEST}{secrets.token_hex(8)}"
        os.mkdir(directory, DIR_MODE)
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def prune(self, now_s: float) -> None:
        """Remove uploads and leftovers older than an hour (best effort)."""
        if not self.root.is_dir():
            return
        for entry in self.root.glob("*/*"):
            with contextlib.suppress(OSError):
                if now_s - entry.lstat().st_mtime < MAX_AGE_S:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()

    def _dir(self, token: str) -> Path:
        directory = self.root / token
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        return directory


def link_name(name: str) -> str:
    """The name a staged upload or directory gets: the last part of the client's argument."""
    base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if base in ("", ".", "..", "-") or "\x00" in base or len(base.encode()) > 255:
        return "input"
    return base


def _too_large(max_bytes: int) -> GraphCliError:
    return GraphCliError(
        "file.too_large",
        f"the file is larger than {max_bytes} bytes",
        details={"max_bytes": max_bytes},
    )
