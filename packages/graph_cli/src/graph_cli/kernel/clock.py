"""Clock port and the persisted time formats (contracts T.1-T.4).

``SystemClock`` is the only place allowed to read wall time (T.3, A test).
Everything else receives a ``Clock`` from the composition root.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol

from graph_cli.kernel.errors import GraphCliError

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS: Final = timedelta(milliseconds=1)

_RFC3339_RE: Final = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(?:([Zz])|([+-])(\d{2}):(\d{2}))?$"
)
_DATE_RE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC, truncated to milliseconds."""
        ...


def truncate_ms(value: datetime) -> datetime:
    """Convert an aware datetime to UTC and floor it to the millisecond."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime")
    value = value.astimezone(UTC)
    return value.replace(microsecond=value.microsecond - value.microsecond % 1000)


class SystemClock:
    def now(self) -> datetime:
        return truncate_ms(datetime.now(UTC))


def epoch_ms(value: datetime) -> int:
    """Milliseconds since the Unix epoch (exact integer arithmetic)."""
    return (truncate_ms(value) - EPOCH) // _ONE_MS


def format_timestamp(value: datetime) -> str:
    """T.1: ``YYYY-MM-DDTHH:MM:SS.sssZ``."""
    value = truncate_ms(value)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_timestamp(text: str) -> datetime:
    """T.2: any RFC 3339 timestamp with an offset, converted to UTC and floored to ms.

    A missing offset is refused with ``usage.invalid_argument``.
    """
    match = _RFC3339_RE.match(text)
    if match is None:
        raise GraphCliError(
            "usage.invalid_argument",
            f"not an RFC 3339 timestamp: {text!r}",
            details={"expected": "YYYY-MM-DDTHH:MM:SS[.fff](Z|+HH:MM)", "actual": text},
        )
    year, month, day, hour, minute, second, fraction, zulu, sign, off_h, off_m = match.groups()
    if zulu is None and sign is None:
        raise GraphCliError(
            "usage.invalid_argument",
            f"timestamp has no UTC offset: {text!r}",
            details={"expected": "an offset such as Z or +02:00", "actual": text},
        )
    micros = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        offset = timedelta()
        if sign is not None:
            offset = timedelta(hours=int(off_h), minutes=int(off_m))
            if sign == "-":
                offset = -offset
        local = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micros
        )
        return truncate_ms((local - offset).replace(tzinfo=UTC))
    except (ValueError, OverflowError) as exc:
        raise GraphCliError(
            "usage.invalid_argument", f"invalid timestamp {text!r}: {exc}", details={"actual": text}
        ) from exc


def format_date(value: date) -> str:
    return value.isoformat()


def parse_date(text: str) -> date:
    """T.1 date ``YYYY-MM-DD``; anything else is ``usage.invalid_argument``."""
    match = _DATE_RE.match(text)
    try:
        if match is None:
            raise ValueError("expected YYYY-MM-DD")
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as exc:
        raise GraphCliError(
            "usage.invalid_argument", f"invalid date {text!r}: {exc}", details={"actual": text}
        ) from exc
