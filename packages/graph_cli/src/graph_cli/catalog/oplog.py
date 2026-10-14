"""The operational log: ``<state_root>/logs/operations.jsonl`` (contracts section 4.2, B12.2).

Storage hands notes (recovery steps, truncated tails) to a callback; this
file is that callback. One canonical JSON line per note with a ``ts``. Not
chained and not audited. Best effort: a failure to log never fails the
operation that produced the note.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.clock import Clock, format_timestamp

OPLOG_FILE: Final = "operations.jsonl"


class OpLogFile:
    def __init__(self, state_root: Path, clock: Clock) -> None:
        self.path = state_root / "logs" / OPLOG_FILE
        self._clock = clock

    def __call__(self, note: Mapping[str, JsonValue]) -> None:
        line = canonical_bytes({**note, "ts": format_timestamp(self._clock.now())}) + b"\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o644)
            try:
                os.write(fd, line)  # one write per line: O_APPEND keeps lines whole
            finally:
                os.close(fd)
        except OSError:
            pass
