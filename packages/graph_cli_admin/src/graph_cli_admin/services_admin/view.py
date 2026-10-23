"""``graph-admin view``: what the live graph view shows (graph view plan, "View service").

- ``open_view`` resolves the graph and authorizes ``read.get`` on it; the
  view is a read, so it writes no audit event.
- ``graph_json`` is the whole graph at one snapshot: every entity as a node
  (label, type, alias, record status) and every edge between two of them
  as a link. Attributes other than the label stay out.
- ``entity_json`` is one entity in full for the detail panel: attributes
  (hidden labels left out), edges both ways with their qualifiers, and
  every revision with the actor that wrote it.
- ``open_blob`` is the stored file of a ``files:File`` entity, authorized
  for ``read.get`` on its type like ``files path``.
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.catalog.activity import ActivityEvent, activity_dir
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import RECORD_STATUSES, RequestMeta, Revision, TxRecord
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import FILE_TYPE
from graph_cli.services.models import ModelCache
from graph_cli_admin.services_admin.context import AdminContext

PAGE: Final = 1000  # txs per changes_since call
# Shown in the browser itself; any other media type is only offered as a download.
INLINE_MEDIA_TYPES: Final = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}
)
_ALL_STATUSES: Final = frozenset(RECORD_STATUSES)


@dataclass(frozen=True, slots=True)
class ViewSource:
    entry: GraphEntry
    model: CompiledModel
    read: GraphStoreRead
    activity_dir: Path
    warnings: tuple[WarningRecord, ...]
    authorize_type: Callable[[str], None] = lambda _type: None


@dataclass(frozen=True, slots=True)
class BlobStream:
    media_type: str
    inline: bool
    file_name: str
    size: int
    chunks: Iterator[bytes]


def open_view(ctx: AdminContext, *, graph_ref: str) -> ViewSource:
    entry = ctx.catalog.resolve(graph_ref)
    meta = RequestMeta.of("view", {"graph": str(entry.id)})
    ctx.authorize([("read.get", f"graph:{entry.id}")], request=meta, graph=entry.id)
    model, warnings = ModelCache(ctx.catalog).load_for_read(entry)
    read, _ = ctx.catalog.store_for(entry.id)

    def authorize_type(type_ref: str) -> None:
        ctx.authorize(
            [("read.get", f"graph:{entry.id}/type:{type_ref}")], request=meta, graph=entry.id
        )

    return ViewSource(
        entry, model, read, activity_dir(ctx.roots.state_root), warnings, authorize_type
    )


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
            "qualifiers": dict(edge.qualifiers),
        }
        for edge in revision.edges
    ]


def _entity_id(text: str) -> EntityId:
    return EntityId(parse_id(text))


def _is_file(source: ViewSource, revision: Revision) -> bool:
    model = source.model
    return FILE_TYPE in model.classes and model.is_a(revision.type, FILE_TYPE)


def _shown_attrs(model: CompiledModel, revision: Revision) -> dict[str, JsonValue]:
    hidden = {a for a, role in model.label_attributes(revision.type) if role == "hidden_label"}
    return {k: v for k, v in revision.attrs.items() if k not in hidden}


def _actor_json(actor: ActorContext) -> dict[str, JsonValue]:
    """Who wrote, facet by facet, with how sure the harness is about each."""
    data: dict[str, JsonValue] = {"label": actor_label(actor)}
    for name in ("principal", "agent", "session"):
        facet = getattr(actor, name)
        if facet.id or facet.name or facet.type:
            data[name] = {
                "assurance": facet.assurance.value,
                "name": facet.name or facet.id,
                "type": facet.type,
            }
    data["client"] = f"{actor.client.name} {actor.client.version}".strip()
    data["entrypoint"] = actor.client.entrypoint.value
    return data


def _end_json(
    source: ViewSource, snapshot_get: Callable[[EntityId], Revision | None], entity: EntityId
) -> dict[str, JsonValue]:
    revision = snapshot_get(entity)
    return {
        "id": str(entity),
        "label": label_of(source.model, revision) if revision else f"unknown {str(entity)[-8:]}",
        "type": revision.type if revision else None,
    }


def entity_json(source: ViewSource, text: str) -> dict[str, JsonValue]:
    entity = _entity_id(text)
    snapshot = source.read.snapshot()
    revision = snapshot.get(entity)
    if revision is None:
        raise GraphCliError("entity.not_found", f"no entity {text}", details={"id": text})
    outgoing: list[JsonValue] = [
        {
            "id": str(edge.edge_id),
            "rel": edge.rel,
            "qualifiers": dict(edge.qualifiers),
            "other": _end_json(source, snapshot.get, edge.target),
        }
        for edge in revision.edges
    ]
    incoming: list[JsonValue] = [
        {
            "id": str(edge.edge_id),
            "rel": edge.rel,
            "qualifiers": dict(edge.qualifiers),
            "other": _end_json(source, snapshot.get, origin),
        }
        for origin, edge in snapshot.incoming(entity)
    ]
    history: list[JsonValue] = []
    for meta in reversed(list(snapshot.history(entity))):
        tx = snapshot.tx(meta.tx_seq)
        history.append(
            {
                "rev": meta.rev,
                "seq": meta.tx_seq,
                "ts": format_timestamp(meta.recorded_at),
                "status": meta.record_status,
                "actor": _actor_json(tx.actor) if tx else None,
                "reason": tx.reason if tx else None,
                "command": tx.request.command if tx else None,
            }
        )
    data: dict[str, JsonValue] = {
        **node_json(source.model, revision),
        "rev": revision.rev,
        "attrs": _shown_attrs(source.model, revision),
        "valid_from": revision.valid_from,
        "valid_to": revision.valid_to,
        "created_at": format_timestamp(revision.created_at),
        "recorded_at": format_timestamp(revision.recorded_at),
        "ontology_version": revision.ontology_version,
        "outgoing": outgoing,
        "incoming": incoming,
        "history": history,
        "file": None,
    }
    digest = revision.attrs.get("content")
    if _is_file(source, revision) and isinstance(digest, str):
        stored = source.read.blob(digest)
        media_type = revision.attrs.get("media_type")
        data["file"] = {
            "media_type": media_type if isinstance(media_type, str) else None,
            "size": stored.size if stored else None,
            "stored": stored is not None,
            "inline": media_type in INLINE_MEDIA_TYPES,
        }
    return data


def open_blob(source: ViewSource, text: str) -> BlobStream:
    entity = _entity_id(text)
    revision = source.read.snapshot().get(entity)
    if revision is None:
        raise GraphCliError("entity.not_found", f"no entity {text}", details={"id": text})
    digest = revision.attrs.get("content")
    if not _is_file(source, revision) or not isinstance(digest, str):
        raise GraphCliError(
            "usage.invalid_argument",
            f"{text} is not a stored file ({FILE_TYPE})",
            details={"argument": "id", "actual": revision.type},
        )
    source.authorize_type(revision.type)
    stored = source.read.blob(digest)
    if stored is None:
        raise GraphCliError(
            "backend.corrupt",
            f"the stored copy of {text} is missing; run graph-admin files verify",
            details={"id": str(entity), "blob": digest},
        )
    media_type = revision.attrs.get("media_type")
    inline = isinstance(media_type, str) and media_type in INLINE_MEDIA_TYPES
    name = revision.attrs.get("file_name") or revision.attrs.get("title") or "file"
    return BlobStream(
        media_type=media_type
        if inline and isinstance(media_type, str)
        else "application/octet-stream",
        inline=inline,
        file_name=str(name),
        size=stored.size,
        chunks=source.read.read_blob(digest),
    )


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
