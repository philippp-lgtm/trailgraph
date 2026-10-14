"""Time formats and the clock port (contracts T.1-T.3)."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from support.clock import FixedClock, StepClock

from graph_cli.kernel.clock import (
    SystemClock,
    epoch_ms,
    format_date,
    format_timestamp,
    parse_date,
    parse_timestamp,
    truncate_ms,
)
from graph_cli.kernel.errors import GraphCliError


def test_format_timestamp() -> None:
    value = datetime(2026, 10, 14, 19, 21, 5, 123999, tzinfo=UTC)
    assert format_timestamp(value) == "2026-10-14T19:21:05.123Z"


def test_format_converts_to_utc() -> None:
    value = datetime(2026, 10, 14, 21, 0, tzinfo=timezone(timedelta(hours=2)))
    assert format_timestamp(value) == "2026-10-14T19:00:00.000Z"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-14T19:21:05Z", "2026-10-14T19:21:05.000Z"),
        ("2026-10-14T19:21:05.1239999Z", "2026-10-14T19:21:05.123Z"),
        ("2026-10-14t21:21:05.5+02:00", "2026-10-14T19:21:05.500Z"),
        ("2026-10-14 00:30:00-01:30", "2026-10-14T02:00:00.000Z"),
    ],
)
def test_parse_timestamp(text: str, expected: str) -> None:
    assert format_timestamp(parse_timestamp(text)) == expected


@pytest.mark.parametrize(
    "text", ["2026-10-14T19:21:05", "2026-13-01T00:00:00Z", "yesterday", "2026-10-14"]
)
def test_parse_timestamp_refuses(text: str) -> None:
    with pytest.raises(GraphCliError) as info:
        parse_timestamp(text)
    assert info.value.code == "usage.invalid_argument"


def test_dates() -> None:
    assert format_date(date(2026, 1, 2)) == "2026-01-02"
    assert parse_date("2026-01-02") == date(2026, 1, 2)
    for bad in ("2026-1-2", "2026-02-30", "x"):
        with pytest.raises(GraphCliError):
            parse_date(bad)


def test_truncate_and_epoch_ms() -> None:
    value = datetime(1970, 1, 1, 0, 0, 1, 999, tzinfo=UTC)
    assert truncate_ms(value).microsecond == 0
    assert epoch_ms(value) == 1000
    with pytest.raises(ValueError):
        truncate_ms(datetime(2026, 1, 1))


def test_system_clock_is_aware_and_truncated() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC
    assert now.microsecond % 1000 == 0


def test_fixed_and_step_clock() -> None:
    fixed = FixedClock("2026-10-14T00:00:00Z")
    assert fixed.now() == fixed.now()
    fixed.advance(timedelta(seconds=1))
    assert format_timestamp(fixed.now()) == "2026-10-14T00:00:01.000Z"
    step = StepClock("2026-10-14T00:00:00Z")
    assert [format_timestamp(step.now()) for _ in range(2)] == [
        "2026-10-14T00:00:00.000Z",
        "2026-10-14T00:00:00.001Z",
    ]
