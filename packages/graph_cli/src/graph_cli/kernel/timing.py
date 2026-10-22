"""Phase timings of one command, for the access log (contracts B5.10).

Durations only: ``time.perf_counter_ns`` is a monotonic counter, never wall
time (T.3), and nothing here is persisted except whole microseconds in the
access log. A phase counts its own time: a phase opened inside another is
taken out of the outer one, so the phases of a command never overlap and
their sum never exceeds the total. A phase entered more than once (a
snapshot before and under the lock, a batch) adds up.

``IMPORTED_NS`` is taken when ``graph_cli`` is first imported, so the
``startup`` phase covers the program's own imports (not the interpreter's
start before them).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from graph_cli.kernel.canonical import JsonValue

IMPORTED_NS: Final = time.perf_counter_ns()
OTHER: Final = "other"  # the time no phase claimed


class Timings:
    def __init__(self, start_ns: int | None = None) -> None:
        self.start_ns = time.perf_counter_ns() if start_ns is None else start_ns
        self._phases: dict[str, int] = {}
        self._nested: list[int] = []  # per open phase: time taken by phases inside it

    @classmethod
    def since_import(cls) -> Timings:
        return cls(IMPORTED_NS)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter_ns()
        self._nested.append(0)
        try:
            yield
        finally:
            elapsed = time.perf_counter_ns() - started
            inner = self._nested.pop()
            self._phases[name] = self._phases.get(name, 0) + elapsed - inner
            if self._nested:
                self._nested[-1] += elapsed

    def until_now(self, name: str) -> None:
        """Everything since the start no phase claimed so far becomes ``name``."""
        self._phases[name] = self._phases.get(name, 0) + self._unclaimed(time.perf_counter_ns())

    def to_json(self) -> dict[str, JsonValue]:
        """``{total, phases}`` in microseconds; ``phases.other`` is the unclaimed rest."""
        now = time.perf_counter_ns()
        phases: dict[str, JsonValue] = {
            name: ns // 1000 for name, ns in sorted(self._phases.items())
        }
        phases[OTHER] = self._unclaimed(now) // 1000
        return {"total": (now - self.start_ns) // 1000, "phases": phases}

    def _unclaimed(self, now: int) -> int:
        return max(now - self.start_ns - sum(self._phases.values()), 0)
