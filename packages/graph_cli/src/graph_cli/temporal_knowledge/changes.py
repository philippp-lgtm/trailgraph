"""The pure core of the change feed (`graph changes`): positions, the one body read, edge
targets and the paging of a feed page.

A position is ``tx:<seq>`` (every change through that tx delivered) or
``tx:<seq>/<entity id>`` (that tx delivered through the entity; its other
entities, ascending by id, follow). The entity id of a cursor is a sort key
only; nothing ever looks it up, so a forged cursor answers like any other.

``touched`` (through ``touched_at``) is the only place the feed reads the body
of a raw tx, and it takes ``(id, rev)`` pairs only: everything else the feed
shows comes from revisions read through the caller's view.

``page`` cuts a page: txs ascending by seq, at most ``SCAN_CAP`` of them read
per call, whole txs while their entity items fit the limit; a tx that does not
fit ends the page before it, unless the page holds no entity item yet: then it
is split and the cursor names the last entity delivered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.records import Revision, TxRecord
from graph_cli.temporal_knowledge.as_of import TX_PREFIX, parse_as_of, seq_at
from graph_cli.temporal_knowledge.history import edge_changes

TARGET_CAP: Final = 50
"""Most target ids an entity item carries; the rest is counted, not listed."""
FEED_LIMIT: Final = 100
"""Entity items of a page unless the caller asks for another number (at most ``MAX_LIMIT``)."""
SCAN_CAP: Final = 1000
"""Most txs one page reads (T1 work budget): a page over txs the reader sees nothing of may be
empty and still say ``more``."""

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


def resolve_since(snapshot: Snapshot, since: FeedPosition | datetime) -> FeedPosition:
    """The position ``since`` names in a graph whose head is ``snapshot``: a time is the
    position after the last tx recorded at or before it (``seq_at``). A seq above the head is
    ``usage.invalid_argument`` with ``details.head_seq`` (a graph restored from a backup)."""
    if isinstance(since, datetime):
        return FeedPosition(seq_at(snapshot, AsOf(recorded_at=since)))
    head = snapshot.seq
    if since.seq > head:
        raise GraphCliError(
            "usage.invalid_argument",
            f"--since: the graph has no tx {since.seq}; its last is tx:{head}",
            details={"argument": "--since", "actual": position_text(since), "head_seq": head},
        )
    return since


def touched(tx: TxRecord) -> tuple[tuple[EntityId, int], ...]:
    """``(entity id, rev)`` of every revision ``tx`` wrote, sorted by id.

    The only read of a raw tx body by the feed, and ids and revision numbers are all
    it takes: what a revision holds is read through the caller's view."""
    return tuple(sorted((r.id, r.rev) for r in tx.revisions))


def touched_at(raw: Snapshot, seq: int) -> tuple[tuple[EntityId, int], ...]:
    """``touched`` of tx ``seq`` of ``raw``, the snapshot below every read limit (a view hands
    out tx metadata only). ``seq`` must be in ``1..raw.seq``: the snapshot bounds the read, so
    a tx written after it is never half seen."""
    tx = raw.tx(seq)
    if tx is None:
        raise GraphCliError("internal.error", f"tx {seq} is missing below the head")
    return touched(tx)


@dataclass(frozen=True, slots=True)
class FeedEntry[T]:
    """The entity items of tx ``seq`` on a page; ``partial`` when the tx goes on on the next."""

    seq: int
    entities: tuple[T, ...]
    partial: bool = False


@dataclass(frozen=True, slots=True)
class FeedPage[T]:
    """The entries of a page, the position after it and whether anything follows it."""

    entries: tuple[FeedEntry[T], ...]
    next: FeedPosition
    more: bool


def page[T](
    start: FeedPosition,
    head: int,
    limit: int,
    items: Callable[[int, EntityId | None], Iterator[tuple[EntityId, T]]],
    always: Callable[[int], bool],
    *,
    scan_cap: int = SCAN_CAP,
) -> FeedPage[T]:
    """The page after ``start`` (at most ``head``): ``items(seq, after)`` yields what the
    reader sees of tx ``seq`` as ``(entity id, item)`` pairs ascending by id, after ``after``
    when given; it is read lazily, no further than the page needs. A tx with no item has no
    entry, unless ``always(seq)`` (a migration) and the tx is not a remainder. ``next`` passes
    every tx read, the trailing ones without entries too."""
    if limit < 1 or scan_cap < 1:
        raise ValueError("a page needs a positive limit and scan cap")
    entries: list[FeedEntry[T]] = []
    used = 0
    first = start.seq if start.entity is not None else start.seq + 1
    after_all = FeedPosition(start.seq)
    for seq in range(first, min(head, first + scan_cap - 1) + 1):
        after = start.entity if seq == start.seq else None
        budget = limit - used
        taken = list(islice(items(seq, after), budget + 1))
        if len(taken) <= budget:
            if taken or (after is None and always(seq)):
                entries.append(FeedEntry(seq, tuple(item for _, item in taken)))
                used += len(taken)
            after_all = FeedPosition(seq)
            continue
        if used == 0:  # one tx larger than a page: split it
            kept = taken[:budget]
            entries.append(FeedEntry(seq, tuple(item for _, item in kept), partial=True))
            return FeedPage(tuple(entries), FeedPosition(seq, kept[-1][0]), True)
        return FeedPage(tuple(entries), after_all, True)
    return FeedPage(tuple(entries), after_all, after_all.seq < head)


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
