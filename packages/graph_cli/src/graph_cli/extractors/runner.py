"""Run an extractor in a child process under limits (file metadata M11).

The write path never shares a process with a parser of untrusted files. ``run_extractor``
starts ``extractors.child`` with an empty environment, hands it one path read-only, and maps
how it ended to an ``ExtractionResult`` or a ``Transient``:

- the child answered ``ok`` or ``failed``: that result;
- wall-clock timeout, or killed by ``SIGXCPU``: ``failed`` with ``limit.time``;
- ``MemoryError`` in the child: ``failed`` with ``limit.memory`` (decided in the child);
- crashed (``SIGSEGV``, ``SIGBUS``, ``SIGABRT``, ``SIGFPE``, ``SIGILL``): ``failed`` with
  ``extractor.crashed``, because the bytes did it;
- any other signal (the OOM killer's ``SIGKILL``, ``SIGTERM``), no or garbled answer, an
  unreadable path: ``Transient``, which is not a function of the bytes.

Object storage has no local path: ``materialized`` writes the streamed bytes to a private
file for the duration of one run.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.extractors.model import (
    PROBLEM_CRASHED,
    PROBLEM_TIME,
    ExtractionResult,
    PartResult,
    Transient,
)

CHILD_COMMAND: Final = (sys.executable, "-I", "-X", "utf8", "-m", "graph_cli.extractors.child")

_CRASH_SIGNALS: Final = frozenset(
    {signal.SIGSEGV, signal.SIGBUS, signal.SIGABRT, signal.SIGFPE, signal.SIGILL}
)


@dataclass(frozen=True)
class ExtractLimits:
    """Limits of one run (``config.toml`` ``[extract]``)."""

    timeout_seconds: float = 30.0
    max_memory_mib: int = 512


DEFAULT_LIMITS: Final = ExtractLimits()


def run_extractor(
    name: str,
    path: Path,
    recorded_media_type: str,
    limits: ExtractLimits = DEFAULT_LIMITS,
    *,
    command: Sequence[str] = CHILD_COMMAND,
) -> ExtractionResult | Transient:
    """Run extractor ``name`` on the file at ``path`` in a child process.

    ``command`` is the child's command line; only tests replace it.
    """
    request = json.dumps(
        {
            "extractor": name,
            "path": str(path),
            "recorded_media_type": recorded_media_type,
            "timeout_seconds": limits.timeout_seconds,
            "max_memory_mib": limits.max_memory_mib,
        }
    )
    try:
        done = subprocess.run(
            list(command),
            input=request.encode(),
            capture_output=True,
            timeout=limits.timeout_seconds,
            env={},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ExtractionResult.failed(PROBLEM_TIME)
    except OSError as error:
        return Transient(f"the extractor process did not start: {error.strerror}")
    if done.returncode < 0:
        return _killed(-done.returncode)
    if done.returncode != 0:
        return Transient(f"the extractor process exited with status {done.returncode}")
    return _parse(done.stdout)


def _killed(number: int) -> ExtractionResult | Transient:
    if number == signal.SIGXCPU:
        return ExtractionResult.failed(PROBLEM_TIME)
    if number in _CRASH_SIGNALS:
        return ExtractionResult.failed(PROBLEM_CRASHED)
    return Transient(f"the extractor process was killed by signal {number}")


def _parse(stdout: bytes) -> ExtractionResult | Transient:
    garbled = Transient("the extractor process gave no readable answer")
    try:
        answer = json.loads(stdout)
    except ValueError:
        return garbled
    if not isinstance(answer, dict):
        return garbled
    status = answer.get("status")
    if status == "transient":
        detail = answer.get("detail")
        return Transient(detail if isinstance(detail, str) else "the file could not be read")
    values = answer.get("values")
    parts = answer.get("parts")
    problem = answer.get("problem")
    if (
        not isinstance(values, dict)
        or not isinstance(parts, list)
        or not (problem is None or isinstance(problem, str))
    ):
        return garbled
    result_parts: list[PartResult] = []
    for part in parts:
        if not (
            isinstance(part, dict)
            and isinstance(part.get("name"), str)
            and isinstance(part.get("values"), dict)
        ):
            return garbled
        result_parts.append(PartResult(part["name"], part["values"]))
    if status == "ok":
        return ExtractionResult(status="ok", values=values, parts=tuple(result_parts))
    if status == "failed":
        return ExtractionResult(status="failed", problem=problem)
    return garbled


@contextmanager
def materialized(chunks: Iterable[bytes]) -> Iterator[Path]:
    """The streamed bytes as a private read-only file, removed when the block ends.

    For graphs whose files have no local path (object storage): the extractor child still
    reads a path. The directory is private to the process (mode 0700).
    """
    directory = tempfile.mkdtemp(prefix="graph_cli_extract_")
    try:
        path = Path(directory) / "blob"
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(handle, "wb") as sink:
            for chunk in chunks:
                sink.write(chunk)
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


__all__ = ["CHILD_COMMAND", "DEFAULT_LIMITS", "ExtractLimits", "materialized", "run_extractor"]
