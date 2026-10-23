"""``--as-of`` text and the seq it names (contracts B8.8, T.2, T.4)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.query_ast import AsOf
from graph_cli.temporal_knowledge.as_of import as_of_text, parse_as_of, seq_at

T0 = datetime(2030, 1, 1, tzinfo=UTC)


class Log:
    """A snapshot that answers ``seq`` and ``tx(seq)``; tx ``n`` recorded at ``times[n-1]``."""

    def __init__(self, *minutes: int) -> None:
        self.times = [T0 + timedelta(minutes=m) for m in minutes]
        self.seq = len(self.times)

    def tx(self, seq: int) -> Any:
        return SimpleNamespace(recorded_at=self.times[seq - 1]) if 1 <= seq <= self.seq else None


def at(minutes: float) -> AsOf:
    return AsOf(recorded_at=T0 + timedelta(minutes=minutes))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tx:0", AsOf(tx_seq=0)),
        ("tx:12", AsOf(tx_seq=12)),
        ("tx:007", AsOf(tx_seq=7)),
        ("2030-01-01T00:00:00Z", AsOf(recorded_at=T0)),
        ("2030-01-01T02:00:00.5+02:00", AsOf(recorded_at=T0 + timedelta(milliseconds=500))),
    ],
)
def test_parse(text: str, expected: AsOf) -> None:
    assert parse_as_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "tx:",
        "tx:-1",
        "tx:1.5",
        "tx:" + chr(0x661),
        "TX:1",
        "12",
        "2030-01-01",
        "2030-01-01T00:00:00",
    ],
)
def test_parse_refuses(text: str) -> None:
    with pytest.raises(GraphCliError) as info:
        parse_as_of(text)
    assert info.value.code == "usage.invalid_argument"
    assert info.value.message.startswith("--as-of: ")
    assert info.value.details["argument"] == "--as-of"
    assert info.value.details["actual"] == text


def test_text_is_canonical() -> None:
    assert as_of_text(parse_as_of("tx:007")) == "tx:7"
    assert as_of_text(parse_as_of("2030-01-01T02:00:00+02:00")) == "2030-01-01T00:00:00.000Z"


def test_a_seq_is_bound_by_the_head() -> None:
    log: Any = Log(0, 1, 2)
    assert seq_at(log, AsOf(tx_seq=0)) == 0
    assert seq_at(log, AsOf(tx_seq=3)) == 3
    with pytest.raises(GraphCliError) as info:
        seq_at(log, AsOf(tx_seq=4))
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details == {"argument": "--as-of", "actual": "tx:4", "head_seq": 3}


@pytest.mark.parametrize(
    ("minutes", "seq"),
    [(-1, 0), (0, 2), (0.5, 2), (1, 3), (4, 3), (5, 5), (60, 5)],
)
def test_a_timestamp_names_the_last_tx_at_or_before_it(minutes: float, seq: int) -> None:
    """T.4: equal timestamps are possible (clamped clock); the last of them wins."""
    log: Any = Log(0, 0, 1, 5, 5)
    assert seq_at(log, at(minutes)) == seq


def test_a_timestamp_in_an_empty_graph() -> None:
    log: Any = Log()
    assert seq_at(log, at(0)) == 0
