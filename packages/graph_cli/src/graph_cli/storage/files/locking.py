"""Exclusive file locks with a timeout (contracts B13.8, W.a-W.c).

``fcntl.flock`` locks belong to the open file description: closing another
descriptor of the same file does not drop them (unlike POSIX record locks),
two descriptors in one process conflict, and the OS releases them when the
process dies. Lock files are created by writers only and never deleted.
NFS is unsupported (B13.12).
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files.durable import FILE_MODE

_FIRST_DELAY_S: Final = 0.001
_MAX_DELAY_S: Final = 0.05


class HeldLock:
    """An acquired lock; release it once (or use it as a context manager)."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> HeldLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def acquire(path: Path, *, timeout_s: float) -> HeldLock:
    """Take an exclusive lock on ``path``, polling with backoff until ``timeout_s``.

    ``time.monotonic`` and ``time.sleep`` measure durations only; no record
    time is read here (T.3). A timeout is ``backend.lock_timeout`` (retryable).
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable",
            f"cannot open lock file: {exc.strerror}",
            details={"lock": path.name},
        ) from exc
    deadline = time.monotonic() + max(timeout_s, 0.0)
    delay = _FIRST_DELAY_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GraphCliError(
                        "backend.lock_timeout",
                        f"lock not acquired within {timeout_s} s",
                        details={"lock": path.name, "timeout_ms": round(timeout_s * 1000)},
                    ) from None
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, _MAX_DELAY_S)
                continue
            return HeldLock(path, fd)
    except OSError as exc:
        os.close(fd)
        raise GraphCliError(
            "backend.unavailable", f"cannot lock: {exc.strerror}", details={"lock": path.name}
        ) from exc
    except BaseException:
        os.close(fd)
        raise
