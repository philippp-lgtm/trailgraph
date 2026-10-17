"""``graph-admin view``: what the live graph view shows (graph view plan, "View service").

- ``open_view`` resolves the graph and authorizes ``read.get`` on it; the
  view is a read, so it writes no audit event.
- ``graph_json`` is the whole graph at one snapshot: every entity as a node
  (label, type, alias, record status) and every edge between two of them
  as a link. Attributes other than the label stay out.
- ``ViewFeed.poll`` turns committed txs after the last seen seq into
  ``created`` (rev 1) and ``updated`` (rev > 1) events. The txlog, not the
  audit log, is the source: an intent precedes its commit (W.12).
- ``read_event`` turns an activity datagram for this graph into a ``read``
  event; ids the page does not know are the page's to ignore.

A node's label is the value of the first ``label`` role attribute, else the
alias, else the type's local name and the last 8 hex digits of the id; a
hidden label is never shown.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.catalog.activity import ActivityEvent, activity_dir
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import RECORD_STATUSES, RequestMeta, Revision, TxRecord
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.models import ModelCache
from graph_cli_admin.services_admin.context import AdminContext

PAGE: Final = 1000  # txs per changes_since call
_ALL_STATUSES: Final = frozenset(RECORD_STATUSES)


@dataclass(frozen=True, slots=True)
class ViewSource:
    entry: GraphEntry
    model: CompiledModel
    read: GraphStoreRead
    activity_dir: Path
    warnings: tuple[WarningRecord, ...]


def open_view(ctx: AdminContext, *, graph_ref: str) -> ViewSource:
    entry = ctx.catalog.resolve(graph_ref)
    meta = RequestMeta.of("view", {"graph": str(entry.id)})
    ctx.authorize([("read.get", f"graph:{entry.id}")], request=meta, graph=entry.id)
    model, warnings = ModelCache(ctx.catalog).load_for_read(entry)
    read, _ = ctx.catalog.store_for(entry.id)
    return ViewSource(entry, model, read, activity_dir(ctx.roots.state_root), warnings)


def graph_json(source: ViewSource) -> dict[str, JsonValue]:
    snapshot = source.read.snapshot()
    revisions = snapshot.query(Query(record_status=_ALL_STATUSES, limit=sys.maxsize)).items
    known = {str(r.id) for r in revisions}
    nodes: list[JsonValue] = [node_json(source.model, r) for r in revisions]
    links: list[JsonValue] = [
        link for r in revisions for link in links_json(r) if link["target"] in known
    ]
    return {
        "graph": {"id": str(source.entry.id), "name": source.entry.name},
        "seq": snapshot.seq,
        "types": sorted({r.type for r in revisions}),
        "nodes": nodes,
        "links": links,
    }


def label_of(model: CompiledModel, revision: Revision) -> str:
    for attr, role in model.label_attributes(revision.type):
        value = revision.attrs.get(attr)
        if role == "label" and isinstance(value, str) and value:
            return value
    return revision.alias or f"{revision.type.rpartition(':')[2]} {str(revision.id)[-8:]}"


def node_json(model: CompiledModel, revision: Revision) -> dict[str, JsonValue]:
    return {
        "id": str(revision.id),
        "label": label_of(model, revision),
        "type": revision.type,
        "alias": revision.alias,
        "status": revision.record_status,
    }


def links_json(revision: Revision) -> list[dict[str, JsonValue]]:
    return [
        {
            "id": str(edge.edge_id),
            "source": str(revision.id),
            "target": str(edge.target),
            "rel": edge.rel,
        }
        for edge in revision.edges
    ]


def actor_label(actor: ActorContext) -> str:
    """Who wrote: the agent if the harness named one, else the principal."""
    for facet in (actor.agent, actor.principal):
        for value in (facet.name, facet.id, facet.type):
            if value:
                return value
    return actor.client.entrypoint.value


def tx_events(model: CompiledModel, tx: TxRecord) -> list[dict[str, JsonValue]]:
    """One event per revision of a committed tx, in id order."""
    events: list[dict[str, JsonValue]] = []
    for revision in sorted(tx.revisions, key=lambda r: r.id):
        links: list[JsonValue] = list(links_json(revision))
        events.append(
            {
                "kind": "created" if revision.rev == 1 else "updated",
                "seq": tx.seq,
                "ts": format_timestamp(tx.recorded_at),
                "actor": actor_label(tx.actor),
                "node": node_json(model, revision),
                "links": links,
            }
        )
    return events


def read_event(source: ViewSource, event: ActivityEvent) -> dict[str, JsonValue] | None:
    if event.graph != str(source.entry.id):
        return None
    ids: list[JsonValue] = list(event.ids)
    return {"kind": "read", "ids": ids}


class ViewFeed:
    """Committed changes after ``seq``, as view events."""

    def __init__(self, source: ViewSource, seq: int) -> None:
        self.source = source
        self.seq = seq

    def poll(self) -> list[dict[str, JsonValue]]:
        if self.source.read.head().seq <= self.seq:
            return []
        events: list[dict[str, JsonValue]] = []
        while True:
            txs = self.source.read.changes_since(self.seq, limit=PAGE)
            for tx in txs:
                events += tx_events(self.source.model, tx)
                self.seq = tx.seq
            if len(txs) < PAGE:
                return events
