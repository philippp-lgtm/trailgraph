"""``graph-admin view``: what the live graph view shows (graph view plan, "View service").

- ``open_view`` resolves the graph and authorizes ``read.get`` on it; the
  view is a read, so it writes no audit event.
- ``ViewGraphs`` serves every graph of the data root: ``listing`` names
  those the caller may read (``read.get``, decided without an audit
  event per graph), ``get`` resolves and authorizes a graph on every
  request and opens it on first use, with its own feed and trail;
  ``authorize`` decides another action on it (``read.describe`` for the
  model, ``maint.audit_read`` on ``installation`` for the policies). A
  denial is audited once per graph, action and ``DENIAL_AUDIT_S``;
  repeats in that time are refused alike without an event, so a retrying
  page or client cannot flood the installation audit log. Up to
  ``MAX_OPEN`` graphs stay open: beyond that the least recently used graph that no
  client watches (``watched``) and that is not the default is closed
  (``on_close``); when every open graph is watched, more stay open.
  ``close_revoked`` closes the open graphs the caller may no longer read. A
  graph whose ontology pin moved is opened again, its feed and trail
  kept: on the next request for it, and on every round of the feed (``refresh_models``), which
  tells the clients of that graph to reload. The view shows the data as the app reads it: a
  graph whose migration is committed but not pinned yet (a release running, or interrupted)
  is shown under the target version, whose names its data has (``following``). Warnings of a
  graph opened later go to ``on_warning`` once.
- ``graph_json`` is the whole graph at one snapshot: every entity as a node
  (label, type, alias, record status) and every edge between two of them
  as a link. Attributes other than the label stay out.
- ``model_json`` is the graph's ontology as ``graph describe`` prints it
  (``services.describe``: every module, every type, the relations) plus the
  number of active entities per type; the page draws the type graph from
  it. ``functions_json`` is every published function in full, as
  ``graph describe function`` prints it, or none when the caller may not
  ``read.function`` on the graph (like ``describe functions``: nothing
  shown, nothing audited).
- ``policies_json`` is what ``policy.for_graph`` says about the graph: the
  installed rules that can decide on it and what each project, no project
  and each token gets there, as ``policy show`` and ``policy explain`` print.
- ``entity_json`` is one entity in full for the detail panel: attributes
  (hidden labels left out), edges both ways with their qualifiers, and
  every revision with the actor that wrote it.
- ``open_blob`` is the stored file of a ``files:File`` entity, authorized
  for ``read.get`` on its type like ``files path``.
- ``ViewFeed.poll`` turns committed txs after the last seen seq into
  ``created`` (rev 1) and ``updated`` (rev > 1) events. The txlog, not the
  audit log, is the source: an intent precedes its commit (W.12). A tx with more than
  ``RELOAD_REVISIONS`` revisions (a data migration rewrites every entity of a graph) is one
  ``reload`` event instead: the page loads the graph again, as it does after a reconnect.
- ``read_event`` turns an activity datagram for this graph into a ``read``
  event; ids the page does not know are the page's to ignore.
- ``ActivityTrail`` numbers these events as steps and keeps the last
  ``TRAIL_STEPS`` in memory, so a page can draw where agents went: one step
  per read command (its datagrams merged by ``call``) and one per written
  revision, each with command, actor and session. Nothing is stored; the
  trail starts empty with the view.

The view shows current revisions only, so it never maps old names (migration plan M7:
current revisions already carry the pin's names); an entity's history lists revision numbers,
times, actors and reasons, not attributes or types.

A node's label is the value of the first ``label`` role attribute, else the
alias, else the type's local name and the last 8 hex digits of the id; a
hidden label is never shown.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from graph_cli.catalog.activity import MAX_TEXT, ActivityEvent, activity_dir
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import RECORD_STATUSES, RequestMeta, Revision, TxRecord
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import FILE_TYPE
from graph_cli.query.lookup import label_text
from graph_cli.services.describe import (
    describe_module,
    describe_modules,
    describe_relations,
    describe_type,
    model_envelope,
)
from graph_cli.services.models import ModelCache
from graph_cli_admin.services_admin import policy
from graph_cli_admin.services_admin.context import AdminContext

PAGE: Final = 1000  # txs per changes_since call
# A tx with more revisions is sent as one ``reload`` event, not one event per revision: a
# client's queue holds 1,000 events (``view_server.CLIENT_QUEUE``) and a migration rewrites
# thousands. A quarter of the queue, so several such txs in one poll still fit.
RELOAD_REVISIONS: Final = 250
TRAIL_STEPS: Final = 500
MAX_OPEN: Final = 8  # graphs a view keeps open, each with its model, feed and trail
DENIAL_AUDIT_S: Final = 60.0  # a refused graph's denial is audited at most once in this time
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
    # The published functions the caller may read (``read.function``), else ``None``.
    functions: Callable[[], tuple[FunctionEntry, ...] | None] = lambda: None


@dataclass(frozen=True, slots=True)
class BlobStream:
    media_type: str
    inline: bool
    file_name: str
    size: int
    chunks: Iterator[bytes]


def effective_entry(ctx: AdminContext, graph_ref: str) -> GraphEntry:
    """The graph as the app reads it: pinned to the target version while its migration is
    committed but not pinned yet (graph-admin's catalog keeps the manifest's pin)."""
    return ctx.catalog.following(ctx.resolve(graph_ref, "view"))


def open_view(ctx: AdminContext, *, graph_ref: str) -> ViewSource:
    entry = effective_entry(ctx, graph_ref)
    meta = RequestMeta.of("view", {"graph": str(entry.id)})
    ctx.authorize([("read.get", f"graph:{entry.id}")], request=meta, graph=entry.id)
    model, warnings = ModelCache(ctx.catalog).load_for_read(entry)
    read, _ = ctx.catalog.store_for(entry.id)

    def authorize_type(type_ref: str) -> None:
        ctx.authorize(
            [("read.get", f"graph:{entry.id}/type:{type_ref}")], request=meta, graph=entry.id
        )

    def functions() -> tuple[FunctionEntry, ...] | None:
        if not ctx.allows("read.function", f"graph:{entry.id}", graph=entry.id):
            return None
        slot = ctx.catalog.read_functions(entry.id)
        if slot is None:
            return None
        try:
            return load_entries(slot.compiled) or None
        except JsonShapeError as exc:
            raise GraphCliError(
                "functions.slot_invalid",
                f"the compiled function catalog is unreadable: {exc}",
                details={"file": f"graphs/{entry.id}/functions/slot.json"},
            ) from exc

    return ViewSource(
        entry,
        model,
        read,
        activity_dir(ctx.roots.state_root),
        warnings,
        authorize_type,
        functions,
    )


def graph_json(source: ViewSource) -> dict[str, JsonValue]:
    snapshot = source.read.snapshot()
    revisions = snapshot.query(Query(record_status=_ALL_STATUSES, limit=sys.maxsize)).items
    known = {str(r.id) for r in revisions}
    language = source.entry.language
    nodes: list[JsonValue] = [node_json(source.model, r, language) for r in revisions]
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


def model_json(source: ViewSource) -> dict[str, JsonValue]:
    """The ontology of the graph: ``modules`` (``describe modules``), ``module`` and
    ``type`` (``describe module`` and ``describe type`` of every module and type, by id),
    ``relations`` (``describe relations``) and ``entities``, the active entities per type."""
    entry, model = source.entry, source.model
    functions = source.functions()
    revisions = source.read.snapshot().query(Query(limit=sys.maxsize)).items
    entities: dict[str, JsonValue] = dict(sorted(Counter(r.type for r in revisions).items()))
    modules: dict[str, JsonValue] = {
        m.id: describe_module(entry, model, m.id, functions)["module"]
        for m in sorted(model.modules, key=lambda m: m.id)
    }
    types: dict[str, JsonValue] = {
        curie: describe_type(entry, model, curie)["type"] for curie in sorted(model.classes)
    }
    return model_envelope(
        entry,
        model,
        modules=describe_modules(entry, model, functions)["modules"],
        module=modules,
        type=types,
        relations=describe_relations(entry, model)["relations"],
        entities=entities,
    )


def functions_json(source: ViewSource) -> dict[str, JsonValue]:
    """Every published function in full (``describe function``), in catalog order; none
    when the graph has none or the caller may not ``read.function`` on it."""
    functions: list[JsonValue] = [f.detail_json() for f in source.functions() or ()]
    return model_envelope(source.entry, source.model, functions=functions)


def policies_json(ctx: AdminContext, source: ViewSource) -> dict[str, JsonValue]:
    """The policies as they bear on the graph (``policy.for_graph``)."""
    return model_envelope(source.entry, source.model, **policy.for_graph(ctx, source.entry))


def label_of(model: CompiledModel, revision: Revision, language: str | None = None) -> str:
    """The main label (language-tagged: in ``language``, else the smallest tag), else
    the alias, else type and id."""
    for attr, role in model.label_attributes(revision.type):
        if role == "label" and (text := label_text(revision.attrs.get(attr), (language,))):
            return text
    return revision.alias or f"{revision.type.rpartition(':')[2]} {str(revision.id)[-8:]}"


def node_json(
    model: CompiledModel, revision: Revision, language: str | None = None
) -> dict[str, JsonValue]:
    return {
        "id": str(revision.id),
        "label": label_of(model, revision, language),
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
    """Who wrote, facet by facet, with how sure the harness is about each, and the roles the
    principal held (a server token's) when it had any."""
    data: dict[str, JsonValue] = {"label": actor.label()}
    for name in ("principal", "agent", "session"):
        facet = getattr(actor, name)
        if facet.id or facet.name or facet.type:
            data[name] = {
                "assurance": facet.assurance.value,
                "name": facet.name or facet.id,
                "type": facet.type,
            }
    if actor.roles:
        data["roles"] = list(actor.roles)
    data["client"] = f"{actor.client.name} {actor.client.version}".strip()
    data["entrypoint"] = actor.client.entrypoint.value
    return data


def _end_json(
    source: ViewSource, snapshot_get: Callable[[EntityId], Revision | None], entity: EntityId
) -> dict[str, JsonValue]:
    revision = snapshot_get(entity)
    return {
        "id": str(entity),
        "label": label_of(source.model, revision, source.entry.language)
        if revision
        else f"unknown {str(entity)[-8:]}",
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
        **node_json(source.model, revision, source.entry.language),
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
                "command": tx.request.command,
                "actor": tx.actor.label()[:MAX_TEXT],
                "session": _session(tx.actor),
                "node": node_json(model, revision),
                "links": links,
            }
        )
    return events


def reload_event(tx: TxRecord) -> dict[str, JsonValue]:
    """One event for a tx too big to send revision by revision: the page loads the graph
    again (its seq includes the tx)."""
    return {
        "kind": "reload",
        "reason": "large_tx",
        "seq": tx.seq,
        "ts": format_timestamp(tx.recorded_at),
        "command": tx.request.command,
        "actor": tx.actor.label()[:MAX_TEXT],
        "revisions": len(tx.revisions),
    }


def _session(actor: ActorContext) -> str | None:
    """The session as a read datagram carries it, so reads and writes group alike."""
    key = actor.session_key()
    return None if key is None else key[:MAX_TEXT]


def read_event(
    source: ViewSource, event: ActivityEvent, received: datetime
) -> dict[str, JsonValue] | None:
    """A datagram for this graph as a ``read`` event, stamped when the view received it.
    A version 1 datagram has no command, call, actor or session (null)."""
    if event.graph != str(source.entry.id):
        return None
    ids: list[JsonValue] = list(event.ids)
    edges: list[JsonValue] = list(event.edges)
    return {
        "kind": "read",
        "ts": format_timestamp(received),
        "command": event.command,
        "call": event.call,
        "actor": event.actor,
        "session": event.session,
        "ids": ids,
        "edges": edges,
    }


@dataclass(slots=True)
class TrailStep:
    """One step of the trail: a read command or a written revision."""

    step: int
    kind: str
    ts: str | None
    command: str | None
    actor: str | None
    session: str | None
    seq: int | None
    ids: list[str]
    edges: list[str]

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "step": self.step,
            "kind": self.kind,
            "ts": self.ts,
            "command": self.command,
            "actor": self.actor,
            "session": self.session,
            "ids": list(self.ids),
            "edges": list(self.edges),
        }
        if self.seq is not None:
            data["seq"] = self.seq
        return data


class ActivityTrail:
    """The last ``limit`` steps of activity the view saw, oldest first.

    ``add`` numbers an event as a step and returns it with ``step`` set: a
    ``read`` event whose ``call`` is already in the trail continues that
    step (its new ids and edges are added), any other event is a new step.
    A write step names the written entity and its ``seq``. Called from the
    feed thread, read from request threads.
    """

    def __init__(self, limit: int = TRAIL_STEPS) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._steps: OrderedDict[str, TrailStep] = OrderedDict()
        self._last = 0

    def add(self, event: dict[str, JsonValue]) -> dict[str, JsonValue]:
        kind = str(event["kind"])
        call = event.get("call") if kind == "read" else None
        with self._lock:
            step = self._steps.get(f"call:{call}") if isinstance(call, str) else None
            if step is None:
                self._last += 1
                step = _trail_step(self._last, kind, event)
                key = f"call:{call}" if isinstance(call, str) else f"step:{self._last}"
                self._steps[key] = step
                while len(self._steps) > self.limit:
                    self._steps.popitem(last=False)
            else:
                step.ids += [i for i in _texts(event.get("ids")) if i not in step.ids]
                step.edges += [e for e in _texts(event.get("edges")) if e not in step.edges]
            return {**event, "step": step.step}

    def to_json(self) -> dict[str, JsonValue]:
        with self._lock:
            steps: list[JsonValue] = [step.to_json() for step in self._steps.values()]
        return {"limit": self.limit, "steps": steps}


def _trail_step(number: int, kind: str, event: dict[str, JsonValue]) -> TrailStep:
    node, seq = event.get("node"), event.get("seq")
    if kind == "read":
        ids = list(dict.fromkeys(_texts(event.get("ids"))))
    else:
        ids = [str(node["id"])] if isinstance(node, dict) else []
    return TrailStep(
        step=number,
        kind=kind,
        ts=_text(event.get("ts")),
        command=_text(event.get("command")),
        actor=_text(event.get("actor")),
        session=_text(event.get("session")),
        seq=seq if isinstance(seq, int) else None,
        ids=ids,
        edges=list(dict.fromkeys(_texts(event.get("edges")))),
    )


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _texts(value: JsonValue | None) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


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
                if len(tx.revisions) > RELOAD_REVISIONS:
                    events.append(reload_event(tx))
                else:
                    events += tx_events(self.source.model, tx)
                self.seq = tx.seq
            if len(txs) < PAGE:
                return events


@dataclass(slots=True)
class GraphView:
    """One graph as the view serves it: what it reads, the changes it has not
    sent yet and the steps it saw."""

    source: ViewSource
    feed: ViewFeed
    trail: ActivityTrail


class ViewGraphs:
    """The graphs of the data root, opened on first use; called from request
    threads and the feed thread."""

    def __init__(
        self,
        ctx: AdminContext,
        *,
        default: str | None = None,
        limit: int = MAX_OPEN,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        self.limit = limit
        self.monotonic = monotonic
        self.on_close: Callable[[str], None] = lambda _graph: None
        self.watched: Callable[[str], bool] = lambda _graph: False  # a client watches it
        self.on_warning: Callable[[str, WarningRecord], None] = lambda _graph, _warning: None
        self._lock = threading.Lock()
        self._open: OrderedDict[str, GraphView] = OrderedDict()
        # (graph id, action) -> when its last denial was audited
        self._denied: dict[tuple[str, str], float] = {}
        self._warned: set[tuple[str, str]] = set()  # (graph id, code) passed to on_warning
        self.default: str | None = None  # the id of the graph a request without one gets
        if default is not None:
            self.default = str(self.get(default).source.entry.id)

    def listing(self) -> dict[str, JsonValue]:
        """The graphs the caller may read, by name; archived ones too, marked."""
        entries = [
            m.entry
            for m in self.ctx.manifests(include_archived=True, skip_unreadable=True)
            if self.ctx.allows("read.get", f"graph:{m.entry.id}", graph=m.entry.id)
        ]
        graphs: list[JsonValue] = [
            {
                "id": str(e.id),
                "name": e.name,
                "title": e.title,
                "language": e.language,
                "state": e.state,
            }
            for e in entries
        ]
        default = next((e.name for e in entries if str(e.id) == self.default), None)
        return {"default": default, "graphs": graphs}

    def get(self, ref: str | None) -> GraphView:
        """The graph ``ref`` names (name or id), else the default graph."""
        if not ref:
            if self.default is None:
                raise GraphCliError(
                    "usage.invalid_argument",
                    "name a graph: ?graph=<name or id>",
                    details={"argument": "graph"},
                )
            ref = self.default
        entry = effective_entry(self.ctx, ref)
        key = str(entry.id)
        self.authorize(entry)
        with self._lock:
            found = self._open.get(key)
            if found is not None and found.source.entry == entry:
                self._open.move_to_end(key)
                return found
        source = open_view(self.ctx, graph_ref=key)
        closed: list[str] = []
        with self._lock:
            warnings = [w for w in source.warnings if (key, w.code) not in self._warned]
            self._warned.update((key, w.code) for w in warnings)
            found = self._open.get(key)
            if found is not None:  # the pin moved, or the name: same feed and trail
                # Two stores, not one: until the second, the feed thread may poll with the
                # old source. Both read the same store; only labels of the new model wait.
                found.source = found.feed.source = source
                self._open.move_to_end(key)
                view = found
            else:
                view = GraphView(source, ViewFeed(source, source.read.head().seq), ActivityTrail())
                self._open[key] = view
                closed = self._close_unused(keep=key)
        for warning in warnings:
            self.on_warning(entry.name, warning)
        for graph in closed:
            self.on_close(graph)
        return view

    def refresh_models(self) -> list[str]:
        """Open again every open graph whose ontology pin moved since it was opened (a release,
        or a migration committed but not pinned yet), keeping its feed and trail; the ids of
        those graphs. Without this a graph nobody requests would keep sending events labeled
        with the old model."""
        moved: list[str] = []
        for view in self.opened():
            graph = str(view.source.entry.id)
            try:
                if effective_entry(self.ctx, graph).pin == view.source.entry.pin:
                    continue
                source = open_view(self.ctx, graph_ref=graph)
            except GraphCliError:  # gone, hidden or unreadable: the next request says why
                continue
            with self._lock:
                if self._open.get(graph) is view:
                    view.source = view.feed.source = source
                    moved.append(graph)
        return moved

    def authorize(
        self, entry: GraphEntry, action: str = "read.get", resource: str | None = None
    ) -> None:
        """``action`` on the graph, or on ``resource`` for the graph's page; a denial is
        audited at most once per graph, action and ``DENIAL_AUDIT_S``, a repeat in that
        time is refused without an event."""
        resource = resource or f"graph:{entry.id}"
        decision = self.ctx.decide(action, resource, graph=entry.id)
        if decision.effect != "deny":
            return
        key, now = (str(entry.id), action), self.monotonic()
        with self._lock:
            last = self._denied.get(key)
            first = last is None or now - last >= DENIAL_AUDIT_S
            if first:
                self._denied[key] = now
        if first:  # audits the denial and raises it; passes if the policy just changed
            meta = RequestMeta.of("view", {"graph": key[0]})
            self.ctx.authorize([(action, resource)], request=meta, graph=entry.id)
            return
        raise GraphCliError(
            decision.code or "access.denied",
            f"{decision.action} on {decision.resource} is not permitted",
        )

    def close_revoked(self) -> None:
        """Close every open graph the caller may no longer ``read.get`` (``on_close``), so
        its streams end and the page's reconnect is refused; nothing is audited here."""
        revoked = [view for view in self.opened() if not self._readable(view.source.entry)]
        closed: list[str] = []
        with self._lock:
            for view in revoked:
                graph = str(view.source.entry.id)
                if self._open.get(graph) is view:
                    del self._open[graph]
                    closed.append(graph)
        for graph in closed:
            self.on_close(graph)

    def _readable(self, entry: GraphEntry) -> bool:
        try:
            return self.ctx.allows("read.get", f"graph:{entry.id}", graph=entry.id)
        except GraphCliError:  # an invalid policy decides nothing: closed
            return False

    def _close_unused(self, *, keep: str) -> list[str]:
        """Above ``limit``, the least recently used graphs no client watches, never
        ``keep`` or the default; a watched graph is never closed. Under the lock."""
        closed: list[str] = []
        for graph in list(self._open):
            if len(self._open) <= self.limit:
                break
            if graph in (keep, self.default) or self.watched(graph):
                continue
            del self._open[graph]
            closed.append(graph)
        return closed

    def opened(self) -> list[GraphView]:
        with self._lock:
            return list(self._open.values())

    def find(self, graph: str) -> GraphView | None:
        """An open graph by id, without resolving it again."""
        with self._lock:
            return self._open.get(graph)
