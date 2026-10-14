"""UUIDv7 generation and id grammar (contracts ID.1-ID.7, B1)."""

from datetime import timedelta
from uuid import UUID

import pytest
from support.clock import FixedClock, StepClock
from support.ids import SeededIdProvider

from graph_cli.kernel.clock import epoch_ms
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import (
    ABBREV_RE,
    ALIAS_RE,
    LOCAL_RE,
    Uuid7Provider,
    format_id,
    id_timestamp_ms,
    is_uuid7,
    parse_id,
)


def test_bit_layout() -> None:
    clock = FixedClock("2026-10-14T19:21:05.123Z")
    value = Uuid7Provider(clock).new_id()
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
    assert is_uuid7(value)
    assert id_timestamp_ms(value) == epoch_ms(clock.now())


def test_strictly_increasing_with_same_ms_regression_and_overflow() -> None:
    clock = FixedClock("2026-10-14T00:00:00Z")
    provider = SeededIdProvider(clock, seed=1)
    previous = provider.new_id()
    for index in range(100_000):
        if index == 50_000:
            clock.advance(timedelta(seconds=-5))  # clock moves back
        elif index % 10_000 == 0:
            clock.advance(timedelta(milliseconds=1))
        current = provider.new_id()
        assert current.int > previous.int
        assert format_id(current) > format_id(previous)
        previous = current


def test_counter_overflow_moves_to_next_ms() -> None:
    clock = FixedClock("2026-10-14T00:00:00Z")
    provider = SeededIdProvider(clock)
    start = epoch_ms(clock.now())
    stamps = {id_timestamp_ms(provider.new_id()) for _ in range(5000)}
    assert min(stamps) == start
    assert max(stamps) > start  # 12-bit counter exhausted within one real ms


def test_seeded_provider_is_deterministic() -> None:
    a = SeededIdProvider(StepClock(), seed=7)
    b = SeededIdProvider(StepClock(), seed=7)
    assert [a.new_id() for _ in range(20)] == [b.new_id() for _ in range(20)]
    assert SeededIdProvider(StepClock(), seed=8).new_id() != SeededIdProvider(StepClock()).new_id()


def test_parse_and_format_round_trip() -> None:
    value = UUID("0192f6d4-2a10-7b43-a5f6-0718293a4b5c")
    text = format_id(value)
    assert text == "0192f6d4-2a10-7b43-a5f6-0718293a4b5c"
    assert parse_id(text) == value
    assert parse_id(text.upper()) == value
    assert parse_id("urn:uuid:" + text) == value
    assert parse_id("URN:UUID:" + text.upper()) == value


def test_parse_accepts_other_versions_for_lookups() -> None:
    assert parse_id("6ba7b810-9dad-41d1-80b4-00c04fd430c8").version == 4


@pytest.mark.parametrize(
    "text",
    [
        "0192f6d42a107b43a5f60718293a4b5c",
        "{0192f6d4-2a10-7b43-a5f6-0718293a4b5c}",
        "0192f6d4-2a10-7b43-c5f6-0718293a4b5c",  # Microsoft variant
        "not-a-uuid",
    ],
)
def test_parse_refuses(text: str) -> None:
    with pytest.raises(GraphCliError) as info:
        parse_id(text)
    assert info.value.code == "usage.invalid_argument"


def test_reference_grammars() -> None:
    assert ALIAS_RE.match("ITEM-0007")
    assert ALIAS_RE.match("LOC-123456")
    assert not ALIAS_RE.match("ITEM-07")
    assert not ALIAS_RE.match("item-0007")
    assert LOCAL_RE.match("@shelf_a")
    assert not LOCAL_RE.match("@Shelf")
    assert ABBREV_RE.match("0718293a")
    assert not ABBREV_RE.match("0718293")
