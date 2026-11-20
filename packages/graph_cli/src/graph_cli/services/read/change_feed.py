"""The change feed page and the per-revision parts it shares with ``history``.

Pure functions over a snapshot (the reader's view) and the raw snapshot below it."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import Revision, TxRecord
from graph_cli.temporal_knowledge.changes import (
    SCAN_CAP,
    FeedEntry,
    FeedPosition,
    page,
    position_text,
    targets,
    touched_at,
)
from graph_cli.temporal_knowledge.history import changed_names


@dataclass(frozen=True, slots=True)
class _RevisionChange:
    """One revision and the one it is compared with, as the caller's view hands them out, and
    the names of what changed between them (``None`` for a creation or a revision the view
    hides)."""

    before: Revision | None
    after: Revision | None
    changed: dict[str, JsonValue] | None


def revision_change(snapshot: Snapshot, entity: EntityId, rev: int) -> _RevisionChange:
    """The per-revision part shared by every list of changes (``history``, the change feed):
    both revisions through the caller's snapshot (its view), so hidden edges and attributes are
    already gone, and ``changed_names`` between them.

    The revision compared with is the latest one the view shows before ``rev`` (F3): a view
    omits a revision of a hidden type (a number it leaves a gap at), and the first one it shows
    is a creation for this reader (``before`` and ``changed`` are ``None``), never a diff against
    what it does not show. The walk back reads a revision only where the one after it is
    omitted, so a view that omits nothing reads exactly one revision before ``rev``."""
    after = snapshot.get_revision(entity, rev)
    before = None
    if after is not None:
        for earlier in range(rev - 1, 0, -1):
            before = snapshot.get_revision(entity, earlier)
            if before is not None:
                break
    changed = changed_names(before, after) if before and after else None
    return _RevisionChange(before, after, changed)


def tx_fields(tx: TxRecord | None, session: str | None) -> dict[str, JsonValue]:
    """The tx-level keys of a list item: ``command``, ``reason`` and whether the tx was
    written by the caller's own session (never who wrote it, B8.15)."""
    return {
        "command": tx.request.command if tx else None,
        "reason": tx.reason if tx else None,
        "own_session": tx is not None and session is not None and tx.actor.session.id == session,
    }


def change_page(
    snapshot: Snapshot,
    raw: Snapshot,
    start: FeedPosition,
    limit: int,
    *,
    session: str | None,
    migration: Callable[[int], Mapping[str, JsonValue] | None],
    digest: str | None = None,
    scan_cap: int = SCAN_CAP,
) -> dict[str, JsonValue]:
    """One page of the change feed after ``start`` (resolved, at most the head), as the reader
    whose view ``snapshot`` is sees it; ``raw`` is the snapshot below the view (txs as stored),
    ``migration`` the migration record of a seq (``MappedSnapshot.migration_record``),
    ``digest`` the reader's ``ReadView.digest``.

    Of a raw tx only ``touched`` is read (ids and revision numbers); each revision and the one
    before come through ``snapshot``, so a hidden entity has no item (CF5), its id is in no
    ``targets`` and a hidden attribute is in no ``changed``. A tx with no visible item has no
    entry (CF6), a migration always has one (CF7); the tx-level keys are those of
    ``history`` (CF10), from ``snapshot.tx`` (metadata only for a limited reader)."""
    head = snapshot.seq
    records: dict[int, Mapping[str, JsonValue] | None] = {}

    def record(seq: int) -> Mapping[str, JsonValue] | None:
        if seq not in records:
            records[seq] = migration(seq)
        return records[seq]

    def items(seq: int, after: EntityId | None) -> Iterator[tuple[EntityId, dict[str, JsonValue]]]:
        for entity, rev in touched_at(raw, seq):
            if after is not None and entity <= after:
                continue
            item = _feed_item(snapshot, entity, rev)
            if item is not None:
                yield entity, item

    found = page(start, head, limit, items, lambda seq: record(seq) is not None, scan_cap=scan_cap)
    return {
        "since": position_text(start),
        "since_tx_id": _tx_id(snapshot, start.seq),
        "head": head,
        "next": position_text(found.next),
        "next_tx_id": _tx_id(snapshot, found.next.seq),
        "more": found.more,
        "view": digest,
        "items": [
            _feed_entry(snapshot, entry, session, record(entry.seq)) for entry in found.entries
        ],
    }


def _feed_item(snapshot: Snapshot, entity: EntityId, rev: int) -> dict[str, JsonValue] | None:
    """The entity item of revision ``rev`` (CF9), ``None`` when the view hides the entity:
    names and ids, never values."""
    change = revision_change(snapshot, entity, rev)
    after = change.after
    if after is None:
        return None
    shown, beyond = targets(change.before, after)
    item: dict[str, JsonValue] = {
        "id": str(entity),
        "type": str(after.type),
        "rev": rev,
        "record_status": after.record_status,
        "changed": change.changed,
        "targets": [str(target) for target in shown],
    }
    if beyond:  # additive: more targets than an item lists
        item["targets_more"] = beyond
    return item


def _feed_entry(
    snapshot: Snapshot,
    entry: FeedEntry[dict[str, JsonValue]],
    session: str | None,
    migration: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """A tx of a feed page: the tx-level keys of ``history`` and the entity items."""
    tx = snapshot.tx(entry.seq)
    if tx is None:
        raise GraphCliError("internal.error", f"tx {entry.seq} is missing below the head")
    found: dict[str, JsonValue] = {
        "tx_seq": entry.seq,
        "tx_id": str(tx.tx_id),
        "recorded_at": format_timestamp(tx.recorded_at),
        **tx_fields(tx, session),
    }
    if tx.request.action is not None:  # additive: set by a ``do``
        found["action"] = tx.request.action.to_json()
    if tx.request.source is not None:  # additive: the source run the tx hands over
        found["source"] = tx.request.source.to_json()
    if migration is not None:  # additive: a migration tx (CF7), its versions and summary
        found["migration"] = _feed_migration(migration)
    if entry.partial:  # additive: the tx goes on on the next page
        found["partial"] = True
    found["entities"] = list(entry.entities)
    return found


def _feed_migration(record: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """``{from, to, summary}`` of a migration record; ``summary`` only when the release had a
    migration file, as in ``history``."""
    found: dict[str, JsonValue] = {"from": record.get("from"), "to": record.get("to")}
    summary = record.get("summary")
    if isinstance(summary, str):
        found["summary"] = summary
    return found


def _tx_id(snapshot: Snapshot, seq: int) -> str | None:
    """The id of tx ``seq`` (``None`` for 0, before any tx): random, so it tells nothing
    beyond the seq, also for a tx the reader sees nothing of (CF11)."""
    if seq == 0:
        return None
    tx = snapshot.tx(seq)
    if tx is None:
        raise GraphCliError("internal.error", f"tx {seq} is missing below the head")
    return str(tx.tx_id)
