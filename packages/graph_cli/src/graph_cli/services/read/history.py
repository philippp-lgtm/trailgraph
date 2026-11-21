"""The history of one entity and the change feed of a graph."""

from __future__ import annotations

from datetime import datetime

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.query.plan import check_paging
from graph_cli.services.read.change_feed import change_page, revision_change, tx_fields
from graph_cli.services.read.graph_read import GraphRead, Page
from graph_cli.services.read.migration_effects import migration_item
from graph_cli.services.read.state import ReadState, query_phase
from graph_cli.services.refs import resolve_ref
from graph_cli.temporal_knowledge.changes import FeedPosition, resolve_since


class ReadHistory(ReadState):
    @query_phase
    def history(
        self, view: GraphRead, actor: ActorContext, ref: str, *, limit: int, offset: int
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        """One entity's revisions, newest first: when, by which command, why, and the
        names of what changed against the revision before (B8.15)."""
        entity, warnings = resolve_ref(ref, view.snapshot, argument="ref")
        metas = list(view.snapshot.history(entity))
        if not metas:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
            )
        shown = metas[::-1][offset : offset + limit]
        session = actor.session.id
        items: list[JsonValue] = []
        # a limited reader sees what a migration did only as far as its view reaches
        full = self._models.load_for_read(view.entry)[0] if view.read_view.limited else None
        for meta in shown:
            tx = view.snapshot.tx(meta.tx_seq)
            change = revision_change(view.snapshot, entity, meta.rev)
            item: dict[str, JsonValue] = {
                "rev": meta.rev,
                "tx_seq": meta.tx_seq,
                "recorded_at": format_timestamp(meta.recorded_at),
                "record_status": meta.record_status,
                **tx_fields(tx, session),
                "changed": change.changed,
            }
            if tx is not None and tx.request.action is not None:  # additive: set by a ``do``
                item["action"] = tx.request.action.to_json()
            if tx is not None and tx.request.source is not None:  # additive: a source run
                item["source"] = tx.request.source.to_json()
            migration = migration_item(view, entity, meta.rev, meta.tx_seq, full)
            if migration is not None:  # additive: the revision a migration wrote (M7)
                item["migration"] = migration
            items.append(item)
        data: dict[str, JsonValue] = {
            "id": str(entity),
            "current_rev": metas[-1].rev,
            "items": items,
        }
        self._signal(view, [entity])
        return data, Page(len(metas), len(shown), limit, offset), list(warnings)

    @query_phase
    def changes(
        self, view: GraphRead, since: FeedPosition | datetime, *, limit: int
    ) -> dict[str, JsonValue]:
        """The change feed: what changed in the whole graph after ``since``, oldest first, as
        far as the caller sees it, one page of at most ``limit`` entity items (change feed
        plan CF3-CF13). The txs are read from the opened snapshot up to its head, so a write
        after it waits for the next page. No read signal (CF13)."""
        check_paging(limit, 0)
        mapping = view.mapping
        if mapping is None:
            raise GraphCliError("internal.error", "the change feed needs a view opened to read")
        return change_page(
            view.snapshot,
            mapping,  # below the read limits; its txs are those of ``mapping.raw``, memoised
            resolve_since(view.snapshot, since),
            limit,
            session=view.actor.session.id,
            migration=mapping.migration_record,
            digest=view.read_view.digest,
        )
