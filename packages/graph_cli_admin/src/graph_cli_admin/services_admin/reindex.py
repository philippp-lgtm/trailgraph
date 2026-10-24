"""``graph-admin graph reindex``: rebuild the derived files of one graph (B4.1).

Revision files, ``head.json`` and the disposable index are deleted and
rebuilt from the tx log under the graph lock; the log itself is not changed.
The rebuild is a ``maint.reindex`` event pair (W.e) whose change names the
seq it rebuilds from. Readers during the rebuild may miss a revision file
for a moment; scans fall back to the tx log until the index is back.
"""

from __future__ import annotations

from dataclasses import dataclass

from graph_cli.audit import events
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli_admin.services_admin.context import AdminContext


@dataclass(frozen=True, slots=True)
class ReindexOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


def reindex(
    ctx: AdminContext, *, graph_ref: str, reason: str | None, dry_run: bool
) -> ReindexOutcome:
    entry = ctx.resolve(graph_ref, "graph reindex")
    meta = RequestMeta.of("graph reindex", {"graph": str(entry.id), "dry_run": dry_run})
    decisions = ctx.authorize(
        [("maint.reindex", f"graph:{entry.id}")], request=meta, graph=entry.id, reason=reason
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        seq = session.head().seq
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "seq": seq,
            "dry_run": dry_run,
            "rebuilt": False,
        }
        if dry_run:
            return ReindexOutcome(entry, data, seq)
        receipt = ctx.intent(session, entry, decisions, meta, events.reindexed(seq), reason=reason)
        session.reindex(receipt)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        data["rebuilt"] = True
        return ReindexOutcome(entry, data, seq)
