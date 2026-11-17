"""The pure core of the change feed (`graph changes`): positions, the one body read, edge targets.

A position is ``tx:<seq>`` (every change through that tx delivered) or
``tx:<seq>/<entity id>`` (that tx delivered through the entity; its other
entities, ascending by id, follow). The entity id of a cursor is a sort key
only; nothing ever looks it up, so a forged cursor answers like any other.

``touched`` is the only place the feed reads the body of a raw tx, and it
takes ``(id, rev)`` pairs only: everything else the feed shows comes from
revisions read through the caller's view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.records import Revision, TxRecord
from graph_cli.temporal_knowledge.as_of import TX_PREFIX, parse_as_of
from graph_cli.temporal_knowledge.history import edge_changes

TARGET_CAP: Final = 50
"""Most target ids an entity item carries; the rest is counted, not listed."""

_MAX_SEQ_DIGITS: Final = 18
_ENTITY_SEPARATOR: Final = "/"
_EXPECTED: Final = (
    "tx:<seq>, tx:<seq>/<entity id> or an RFC 3339 timestamp with an offset (Z or +HH:MM)"
)


@dataclass(frozen=True, slots=True)
class FeedPosition:
    """After tx ``seq`` (0 = before any tx), or inside it: after ``entity`` in id order."""

    seq: int
    entity: EntityId | None = None

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise ValueError("a position's seq is not negative")
        if self.entity is not None and self.seq == 0:
            raise ValueError("a position inside a tx needs a tx: seq 0 has none")


def parse_since(text: str) -> FeedPosition | datetime:
    """The position ``text`` names, or the time of an RFC 3339 timestamp.

    ``usage.invalid_argument`` (argument ``--since``) for anything else. A time is
    resolved to a seq by the caller, against its snapshot (``as_of.seq_at``)."""
    if text.startswith(TX_PREFIX):
        digits, separator, entity = text.removeprefix(TX_PREFIX).partition(_ENTITY_SEPARATOR)
        if not digits.isascii() or not digits.isdigit() or len(digits) > _MAX_SEQ_DIGITS:
            raise _usage(f"{text!r} is not tx:<seq> with a non-negative integer", text)
        seq = int(digits)
        if not separator:
            return FeedPosition(seq)
        if seq == 0:
            raise _usage(f"{text!r}: tx:0 has no entities", text)
        try:
            return FeedPosition(seq, EntityId(parse_id(entity)))
        except GraphCliError:
            raise _usage(f"{text!r}: {entity!r} is not an entity id", text) from None
    try:
        at = parse_as_of(text).recorded_at
    except GraphCliError as exc:
        raise _usage(exc.message.removeprefix("--as-of: "), text) from None
    assert at is not None
    return at


def position_text(position: FeedPosition) -> str:
    """The canonical text of ``position``: ``tx:<seq>`` or ``tx:<seq>/<entity id>``."""
    text = f"{TX_PREFIX}{position.seq}"
    if position.entity is not None:
        text += f"{_ENTITY_SEPARATOR}{position.entity}"
    return text


def touched(tx: TxRecord) -> tuple[tuple[EntityId, int], ...]:
    """``(entity id, rev)`` of every revision ``tx`` wrote, sorted by id.

    The only read of a raw tx body by the feed, and ids and revision numbers are all
    it takes: what a revision holds is read through the caller's view."""
    return tuple(sorted((r.id, r.rev) for r in tx.revisions))


def targets(
    before: Revision | None, after: Revision, *, cap: int = TARGET_CAP
) -> tuple[tuple[EntityId, ...], int]:
    """The ids at the other end of the edges ``after`` added, removed or changed against
    ``before`` (all of them for a creation), sorted and unique, and how many more than
    ``cap`` there are.

    A changed edge names both its old and its new target. Callers pass revisions read
    through the view, so edges to entities the reader cannot see are already gone."""
    edges = edge_changes(before, after)
    found = {edge.target for edge in (*edges.added, *edges.removed)}
    for old, new in edges.changed:
        found.update((old.target, new.target))
    ordered = sorted(found)
    return tuple(ordered[:cap]), max(0, len(ordered) - cap)


def _usage(message: str, actual: str) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"--since: {message}",
        details={"argument": "--since", "actual": actual, "expected": _EXPECTED},
    )
