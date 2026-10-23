"""Reading an earlier state: the ``--as-of`` text and the seq it names (contracts B8.8, T.4).

``tx:<seq>`` names a seq; an RFC 3339 timestamp names the last tx recorded at
or before it. ``recorded_at`` never decreases along the log (B4.3), so the
tx is found by bisection over the snapshot's tx records.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.clock import format_timestamp, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import AsOf

TX_PREFIX: Final = "tx:"
EXPECTED: Final = "tx:<seq> or an RFC 3339 timestamp with an offset (Z or +HH:MM)"


def parse_as_of(text: str) -> AsOf:
    """``usage.invalid_argument`` for anything but ``tx:<seq>`` or a timestamp with offset."""
    if text.startswith(TX_PREFIX):
        digits = text.removeprefix(TX_PREFIX)
        if not digits.isascii() or not digits.isdigit():
            raise _usage(f"{text!r} is not tx:<seq> with a non-negative integer", text)
        return AsOf(tx_seq=int(digits))
    try:
        return AsOf(recorded_at=parse_timestamp(text))
    except GraphCliError as exc:
        raise _usage(exc.message, text) from None


def as_of_text(as_of: AsOf) -> str:
    """The canonical text of ``as_of``: ``tx:<seq>`` or a T.1 timestamp."""
    if as_of.tx_seq is not None:
        return f"{TX_PREFIX}{as_of.tx_seq}"
    assert as_of.recorded_at is not None
    return format_timestamp(as_of.recorded_at)


def seq_at(snapshot: Snapshot, as_of: AsOf) -> int:
    """The seq ``as_of`` names in a graph whose head is ``snapshot``.

    A seq outside ``0..head`` is ``usage.invalid_argument``; a timestamp before
    the first tx is seq 0, one at or after the last tx the head."""
    head = snapshot.seq
    if as_of.tx_seq is not None:
        if as_of.tx_seq > head:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--as-of: the graph has no tx {as_of.tx_seq}; its last is tx:{head}",
                details={"argument": "--as-of", "actual": as_of_text(as_of), "head_seq": head},
            )
        return as_of.tx_seq
    at = as_of.recorded_at
    assert at is not None
    low, high = 0, head  # invariant: tx ``low`` is at or before ``at`` (0 = before any tx)
    while low < high:
        middle = (low + high + 1) // 2
        tx = snapshot.tx(middle)
        if tx is None:
            raise GraphCliError("internal.error", f"tx {middle} is missing below the head")
        if tx.recorded_at <= at:
            low = middle
        else:
            high = middle - 1
    return low


def _usage(message: str, actual: str) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"--as-of: {message}",
        details={"argument": "--as-of", "actual": actual, "expected": EXPECTED},
    )
