"""Deterministic clocks for tests (contracts section 2: FixedClock, StepClock)."""

from datetime import UTC, datetime, timedelta

from graph_cli.kernel.clock import parse_timestamp, truncate_ms

DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)


def _coerce(value: datetime | str) -> datetime:
    return parse_timestamp(value) if isinstance(value, str) else truncate_ms(value)


class FixedClock:
    """Returns the same instant until moved with ``set`` or ``advance``."""

    def __init__(self, at: datetime | str = DEFAULT_START) -> None:
        self._now = _coerce(at)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime | str) -> None:
        self._now = _coerce(at)

    def advance(self, delta: timedelta) -> None:
        self._now = truncate_ms(self._now + delta)


class StepClock:
    """Moves forward by ``step`` after every read (first read returns ``start``)."""

    def __init__(
        self, start: datetime | str = DEFAULT_START, step: timedelta = timedelta(milliseconds=1)
    ) -> None:
        self._next = _coerce(start)
        self._step = step

    def now(self) -> datetime:
        current = self._next
        self._next = truncate_ms(current + self._step)
        return current
