"""Read services: graphs, get, history, find, lookup, search, neighbors, query, whoami (contracts
§4.3, B2.10, B6.3, B6.12, B8.8, B8.12, B8.13, B8.15, B9.7, B13).

Every read authorizes its action before touching storage; a denial is
appended to the installation stream and raised. A graph is visible iff
``read.list`` on it is allowed; a denial on a graph the actor cannot see is
raised as ``graph.not_found``, exactly like a graph that does not exist
(B6.15), and still audited. Reads write nothing else.
A read takes one lock-free snapshot and answers everything from it, so the
envelope's ``graph.seq`` names the state that was read. A pinned artifact
that does not match its hash is read with ``ontology.pin_mismatch_read``,
an archived graph with ``graph.archived_read``. ``as_of`` reads the snapshot
at the seq it names (B8.8): it needs ``read.history`` besides the read's own
action, a backend with ``snapshot_at_seq``, and every answer carries
``temporal.as_of_read``; the model stays the current pin. A successful ``get``,
``history``, ``find``, ``lookup``, ``search``, ``neighbors`` or ``query`` hands the
entities it returns, and the edges ``neighbors`` walked or ``query`` rows hold, to the
activity sink with the command and actor that opened the graph (a transient signal for
``graph-admin view``, no log).
``history`` shows of a tx's actor only whether it is the caller's own
session: who wrote a revision is audit content (B8.15).
The command's timings get the phases ``authorize``, ``audit`` (a denial),
``model``, ``snapshot`` and ``query`` (answering from the snapshot); a
Cypher ``query`` has ``parse``, ``resolve`` and ``execute`` instead.
The Cypher engine is imported by ``parse_query`` and ``query`` only: every
other command starts without it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Concatenate

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.effective import (
    graph_level,
    scope_of,
    type_limits,
    withheld_actions,
    write_obligations,
)
from graph_cli.audit import events
from graph_cli.kernel.actions import READ_ACTIONS, RESERVED_ACTIONS, WRITE_ACTIONS
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import Clock, format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EdgeId, EntityId, EventId, IdProvider, TermRef
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ActivitySink, ReadSignal
from graph_cli.kernel.ports.audit import AuditSink
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry, SkillSlot, graph_not_found
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.ports.storage import CAPABILITY_SNAPSHOT_AT_SEQ, GraphStoreRead, Snapshot
from graph_cli.kernel.query_ast import AsOf, Query
from graph_cli.kernel.records import EnvInfo, RequestMeta, Revision
from graph_cli.kernel.timing import Timings
from graph_cli.kinetic.bind import bind_cli
from graph_cli.kinetic.entries import FunctionEntry, find_entry, load_entries
from graph_cli.kinetic.format import ParamDef
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.errors import did_you_mean
from graph_cli.query.cypher.values import Node, Rel, Value, plain_json
from graph_cli.query.lookup import (
    LookupPlan,
    LookupSpec,
    fit_classes,
    main_label,
    match_revision,
    plan_lookup,
    rank_key,
)
from graph_cli.query.plan import (
    ALWAYS_SHOWN,
    DEFAULT_LIMIT,
    FindSpec,
    check_fields,
    plan_find,
    plan_neighbors,
    resolve_term,
)
from graph_cli.query.search import SearchSpec, plan_search
from graph_cli.query.search import main_label as search_label
from graph_cli.query.search import match_revision as search_match
from graph_cli.query.search import rank_key as search_rank
from graph_cli.services.describe import describe_model
from graph_cli.services.models import ModelCache
from graph_cli.services.refs import resolve_ref
from graph_cli.temporal_knowledge.as_of import as_of_text, seq_at
from graph_cli.temporal_knowledge.history import changed_names

if TYPE_CHECKING:
    from graph_cli.query.cypher.ast import Query as CypherQuery
    from graph_cli.query.cypher.resolve import ResolvedQuery

SUPERSEDES = "core:supersedes"
RETRACTS = "core:retracts"
_NO_GRAPH = "graph:00000000-0000-0000-0000-000000000000"  # whoami: a graph no policy names
_VISIBLE = AuthzContext(op_count=0, has_reason=False, dry_run=False)
ACCESS_DENIED_HINT = "ask the maintainer of this installation; do not work around it"


@dataclass(frozen=True, slots=True)
class GraphRead:
    """One graph opened for reading by one actor and command: model and one snapshot."""

    entry: GraphEntry
    model: CompiledModel
    snapshot: Snapshot
    warnings: tuple[WarningRecord, ...]
    actor: ActorContext
    command: str

    def envelope_graph(self) -> dict[str, JsonValue]:
        return graph_json(self.entry, self.snapshot.seq)


@dataclass(frozen=True, slots=True)
class Page:
    total: int
    shown: int
    limit: int
    offset: int
    more: bool | None = None  # set when limit and offset apply per group (lookup values)

    def to_json(self) -> dict[str, JsonValue]:
        truncated = self.offset + self.shown < self.total if self.more is None else self.more
        next_offset = self.offset + (self.shown if self.more is None else self.limit)
        return {
            "total": self.total,
            "shown": self.shown,
            "limit": self.limit,
            "offset": self.offset,
            "truncated": truncated,
            "next_offset": next_offset if truncated else None,
        }


def graph_json(entry: GraphEntry, seq: int | None) -> dict[str, JsonValue]:
    """The envelope ``graph`` block (B9.2)."""
    return {
        "id": str(entry.id),
        "name": entry.name,
        "state": entry.state,
        "ontology": {"bundle": entry.pin.bundle, "version": entry.pin.version},
        "seq": seq,
    }


def entity_json(revision: Revision, fields: Sequence[str] = ()) -> dict[str, JsonValue]:
    """An entity as reads show it: every revision field, explicit nulls (C.6 envelope rule).

    With ``fields`` (checked by ``query.plan.check_fields``) only the always shown
    keys, ``attrs`` with the named attributes (null when absent) and the named
    entity parts and system fields.
    """
    full = _entity_json(revision)
    if not fields:
        return full
    shown: dict[str, JsonValue] = {key: full[key] for key in ALWAYS_SHOWN}
    shown["attrs"] = {n: revision.attrs.get(n) for n in fields if n not in full}
    shown.update({n: full[n] for n in fields if n in full and n not in ALWAYS_SHOWN})
    return shown


def _entity_json(revision: Revision) -> dict[str, JsonValue]:
    return {
        "id": str(revision.id),
        "alias": revision.alias,
        "type": revision.type,
        "rev": revision.rev,
        "record_status": revision.record_status,
        "attrs": dict(revision.attrs),
        "edges": [edge.to_json() for edge in revision.edges],
        "valid_from": revision.valid_from,
        "valid_to": revision.valid_to,
        "created_at": format_timestamp(revision.created_at),
        "recorded_at": format_timestamp(revision.recorded_at),
        "tx_seq": revision.tx_seq,
        "ontology_version": revision.ontology_version,
        "content_hash": revision.content_hash,
    }


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """How to answer a parsed Cypher query: its parameters, the paging of its rows."""

    parameters: Mapping[str, Value] = field(default_factory=dict)
    include_inactive: bool = False
    explain: bool = False
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    budget: int | None = None  # None: the engine's default (``execute.DEFAULT_BUDGET``)


def value_json(value: Value) -> JsonValue:
    """A query value as reads show it: an entity as ``get`` does, an edge with its source."""
    if isinstance(value, Node):
        return entity_json(value.revision)
    if isinstance(value, Rel):
        return {**value.edge.to_json(), "source": str(value.source)}
    if isinstance(value, list):
        return [value_json(v) for v in value]
    if isinstance(value, dict):
        return {k: value_json(v) for k, v in value.items()}
    return plain_json(value)


def entities_in(rows: Sequence[Sequence[Value]]) -> list[EntityId]:
    """The entities rows show (nodes, and both ends of relationships), first seen first."""
    found: dict[EntityId, None] = {}
    for value in _graph_values(rows):
        if isinstance(value, Node):
            found.setdefault(value.id)
        else:
            found.setdefault(value.source)
            found.setdefault(value.target)
    return list(found)


def edges_in(rows: Sequence[Sequence[Value]]) -> list[EdgeId]:
    """The edges rows show as relationships, first seen first."""
    return list(dict.fromkeys(v.edge.edge_id for v in _graph_values(rows) if isinstance(v, Rel)))


def _graph_values(rows: Sequence[Sequence[Value]]) -> Iterator[Node | Rel]:
    """Every node and relationship in rows, also inside lists and maps, in order."""

    def walk(value: Value) -> Iterator[Node | Rel]:
        if isinstance(value, Node | Rel):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)

    for row in rows:
        for value in row:
            yield from walk(value)


def _query[**P, R](
    method: Callable[Concatenate[ReadService, P], R],
) -> Callable[Concatenate[ReadService, P], R]:
    """A read answered from an open snapshot: the ``query`` phase."""

    @functools.wraps(method)
    def timed(self: ReadService, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self.timings.phase("query"):
            return method(self, *args, **kwargs)

    return timed


class ReadService:
    def __init__(
        self,
        *,
        catalog: Catalog,
        audit: AuditSink,
        policy: PolicySource,
        clock: Clock,
        ids: IdProvider,
        env: EnvInfo,
        activity: ActivitySink = NULL_ACTIVITY,
        timings: Timings | None = None,
    ) -> None:
        self._catalog = catalog
        self._audit = audit
        self.activity = activity
        self._policy = policy
        self._clock = clock
        self._ids = ids
        self._env = env
        self.timings = timings if timings is not None else Timings()
        self._models = ModelCache(catalog, self.timings)
        self._functions: dict[str, tuple[FunctionEntry, ...]] = {}  # compiled hash -> entries

    # ---- authorization -------------------------------------------------

    def authorize(
        self,
        actor: ActorContext,
        action: str,
        resource: str,
        request: RequestMeta,
        graph: GraphEntry | None = None,
    ) -> Decision:
        """B6.3: one decision before storage is touched; a denial is audited and raised."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(None if graph is None else graph.id)
            ctx = AuthzContext(op_count=0, has_reason=False, dry_run=False)
            decision = authorize(actor, action, resource, ctx, policy)
        if decision.effect == "allow":
            return decision
        hidden = graph is not None and not self.visible(actor, graph)
        error = (
            graph_not_found(str(graph.id))
            if graph is not None and hidden
            else denial_error(decision)
        )
        raise self._record_denial(actor, decision, request, graph, error)

    def visible(self, actor: ActorContext, entry: GraphEntry) -> bool:
        """B6.15: the actor may see the graph (``read.list`` on ``graph:<id>``)."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            decision = authorize(actor, "read.list", f"graph:{entry.id}", _VISIBLE, policy)
        return decision.effect == "allow"

    def functions(self, actor: ActorContext, entry: GraphEntry) -> tuple[FunctionEntry, ...] | None:
        """The graph's published functions as the actor may see them (K10): ``None`` when the
        graph has none or the actor may not ``read.function`` on it, so a listing shows
        nothing and records nothing (no denial event, like ``graphs``). The parsed catalog is
        cached by the hash of the compiled document."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            decision = authorize(actor, "read.function", f"graph:{entry.id}", _VISIBLE, policy)
        if decision.effect != "allow":
            return None
        return self._published(entry)

    def _published(self, entry: GraphEntry) -> tuple[FunctionEntry, ...] | None:
        """The parsed catalog of the graph (``None`` when it has none), no authorization."""
        with self.timings.phase("model"):
            slot = self._catalog.read_functions(entry.id)
            if slot is None:
                return None
            found = self._functions.get(slot.compiled_hash)
            if found is None:
                try:
                    found = load_entries(slot.compiled)
                except JsonShapeError as exc:
                    raise GraphCliError(
                        "functions.slot_invalid",
                        f"the compiled function catalog is unreadable: {exc}",
                        details={"file": f"graphs/{entry.id}/functions/slot.json"},
                    ) from exc
                self._functions[slot.compiled_hash] = found
        return found or None

    def refuse_hidden(
        self, actor: ActorContext, entry: GraphEntry, ref: str, request: RequestMeta
    ) -> GraphCliError:
        """A selected graph the actor cannot see: the ``read.list`` denial is audited, the
        caller gets ``graph.not_found`` for ``ref`` (Q4: no existence leak)."""
        policy = self._policy.current(entry.id)
        decision = authorize(actor, "read.list", f"graph:{entry.id}", _VISIBLE, policy)
        return self._record_denial(actor, decision, request, entry, graph_not_found(ref))

    def _record_denial(
        self,
        actor: ActorContext,
        decision: Decision,
        request: RequestMeta,
        graph: GraphEntry | None,
        error: GraphCliError,
    ) -> GraphCliError:
        assert decision.code is not None
        event = events.refusal(
            status="denied",
            code=decision.code,
            actor=actor,
            decisions=[decision],
            request=request,
            ts=self._clock.now(),
            event_id=EventId(self._ids.new_id()),
            env=self._env,
            graph=graph,
        )
        try:
            with self.timings.phase("audit"):
                self._audit.append_installation(event)
        except GraphCliError as exc:
            warning = WarningRecord(
                "audit.refusal_not_recorded", "the denial could not be audited", {"cause": exc.code}
            )
            return GraphCliError(
                error.code, error.message, details=error.details, warnings=(warning,)
            )
        return error

    def _signal(
        self,
        view: GraphRead,
        entities: Sequence[EntityId],
        edges: Sequence[EdgeId] = (),
        function: str | None = None,
    ) -> None:
        """Tell running views what this read returned (``[activity] signal_reads``)."""
        self.activity.reads(
            ReadSignal(view.entry.id, view.command, view.actor, entities, edges, function)
        )

    # ---- reads ---------------------------------------------------------

    def open(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        action: str,
        request: RequestMeta,
        *,
        as_of: AsOf | None = None,
    ) -> GraphRead:
        """The graph's model and one snapshot: the head, or the state ``as_of`` names;
        an earlier state is history, so it also needs ``read.history``."""
        self.authorize(actor, action, f"graph:{entry.id}", request, entry)
        if as_of is not None and action != "read.history":
            self.authorize(actor, "read.history", f"graph:{entry.id}", request, entry)
        model, warnings = self._models.load_for_read(entry)
        if entry.state == "archived":
            warnings += (
                WarningRecord("graph.archived_read", f"graph {entry.name} is archived", {}),
            )
        read, _ = self._catalog.store_for(entry.id)
        with self.timings.phase("snapshot"):
            snapshot = read.snapshot()
            if as_of is not None:
                snapshot, warning = _snapshot_as_of(read, snapshot, entry, as_of)
                warnings += (warning,)
        warnings = (*warnings, *snapshot.warnings)
        return GraphRead(entry, model, snapshot, warnings, actor, request.command)

    def skill(
        self, actor: ActorContext, entry: GraphEntry, request: RequestMeta
    ) -> SkillSlot | None:
        """The graph's published skill (D15 Q1): a description of the graph, so
        ``read.describe`` on ``graph:<id>``; ``None`` when none is published."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        return self._catalog.read_skill(entry.id)

    def skill_model(
        self, actor: ActorContext, entry: GraphEntry, request: RequestMeta
    ) -> tuple[dict[str, JsonValue], tuple[WarningRecord, ...]]:
        """The model documents a generated graph skill is rendered with: those of the
        graph's current pin, as ``graph describe`` prints them (``read.describe``), plus the
        compiled function catalog when the graph has one. Like the type names, the function
        names and summaries are schema-level text, so the skill lists them for every reader
        of the description (K10)."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        model, warnings = self._models.load_for_read(entry)
        slot = self._catalog.read_functions(entry.id)
        catalog = slot.compiled if slot is not None and slot.count else None
        return describe_model(entry, model, catalog), warnings

    def graphs(
        self, actor: ActorContext, request: RequestMeta, *, allowed: Sequence[str] | None
    ) -> tuple[list[JsonValue], int, tuple[WarningRecord, ...]]:
        """B6.12: the graphs the actor may see, narrowed by the pointer allowlist.

        Listing filters and records nothing; ``hidden_count`` counts the visible
        graphs the pointer hides, never the ones the policy hides."""
        visible = [
            e for e in self._catalog.list_graphs(skip_unreadable=True) if self.visible(actor, e)
        ]
        shown = [e for e in visible if allowed is None or e.name in allowed or str(e.id) in allowed]
        items: list[JsonValue] = []
        for entry in shown:
            item = graph_json(entry, None)
            del item["seq"]
            item["title"] = entry.title
            item["language"] = entry.language
            items.append(item)
        return items, len(visible) - len(shown), self._catalog.warnings

    @_query
    def get(
        self, view: GraphRead, ref: str, *, rev: int | None = None, fields: Sequence[str] = ()
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """The current revision, or revision ``rev``; the status warnings and
        ``superseded_by``/``retracted_by`` always describe the current state."""
        shown = check_fields(view.model, (), fields)
        entity, ref_warnings = resolve_ref(ref, view.snapshot)
        current = view.snapshot.get(entity)
        if current is None:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
            )
        revision = current if rev is None else view.snapshot.get_revision(entity, rev)
        if revision is None:
            raise GraphCliError(
                "entity.revision_not_found",
                f"{ref} has no revision {rev}",
                details={"ref": ref, "id": str(entity), "rev": rev, "current_rev": current.rev},
            )
        warnings = list(ref_warnings)
        superseded_by = _sources(view.snapshot, entity, SUPERSEDES)
        retracted_by = _sources(view.snapshot, entity, RETRACTS)
        if current.record_status == "superseded":
            warnings.append(
                WarningRecord(
                    "entity.superseded",
                    f"{ref} is superseded; it is shown as recorded, without redirect",
                    {"id": str(entity), "superseded_by": list(superseded_by)},
                )
            )
        elif current.record_status == "retracted":
            warnings.append(
                WarningRecord("entity.retracted", f"{ref} is retracted", {"id": str(entity)})
            )
        data: dict[str, JsonValue] = {
            "entity": entity_json(revision, shown),
            "current_rev": current.rev,
            "superseded_by": list(superseded_by),
            "retracted_by": list(retracted_by),
        }
        self._signal(view, [entity])
        return data, warnings

    @_query
    def history(
        self, view: GraphRead, actor: ActorContext, ref: str, *, limit: int, offset: int
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        """One entity's revisions, newest first: when, by which command, why, and the
        names of what changed against the revision before (B8.15)."""
        entity, warnings = resolve_ref(ref, view.snapshot)
        metas = list(view.snapshot.history(entity))
        if not metas:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
            )
        shown = metas[::-1][offset : offset + limit]
        session = actor.session.id
        items: list[JsonValue] = []
        for meta in shown:
            tx = view.snapshot.tx(meta.tx_seq)
            before = view.snapshot.get_revision(entity, meta.rev - 1) if meta.rev > 1 else None
            after = view.snapshot.get_revision(entity, meta.rev)
            items.append(
                {
                    "rev": meta.rev,
                    "tx_seq": meta.tx_seq,
                    "recorded_at": format_timestamp(meta.recorded_at),
                    "record_status": meta.record_status,
                    "command": tx.request.command if tx else None,
                    "reason": tx.reason if tx else None,
                    "own_session": tx is not None
                    and session is not None
                    and tx.actor.session.id == session,
                    "changed": changed_names(before, after) if before and after else None,
                }
            )
        data: dict[str, JsonValue] = {
            "id": str(entity),
            "current_rev": metas[-1].rev,
            "items": items,
        }
        self._signal(view, [entity])
        return data, Page(len(metas), len(shown), limit, offset), list(warnings)

    @_query
    def find(
        self,
        view: GraphRead,
        spec: FindSpec,
        *,
        rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        in_rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        fields: Sequence[str] = (),
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        warnings: list[WarningRecord] = []
        shown = check_fields(view.model, spec.types, fields)

        def resolve(
            pairs: Sequence[tuple[str, Sequence[str]]],
        ) -> tuple[tuple[str, tuple[EntityId, ...]], ...]:
            resolved = []
            for rel, texts in pairs:
                entities: list[EntityId] = []
                for text in texts:
                    entity, found = resolve_ref(text, view.snapshot)
                    warnings.extend(found)
                    entities.append(entity)
                resolved.append((rel, tuple(sorted(set(entities)))))
            return tuple(resolved)

        spec = FindSpec(
            types=spec.types,
            where=spec.where,
            rels=(*spec.rels, *resolve(rel_refs)),
            in_rels=(*spec.in_rels, *resolve(in_rel_refs)),
            include_inactive=spec.include_inactive,
            sort=spec.sort,
            limit=spec.limit,
            offset=spec.offset,
        )
        query: Query = plan_find(view.model, spec)
        page = view.snapshot.query(query)
        data: dict[str, JsonValue] = {"items": [entity_json(r, shown) for r in page.items]}
        self._signal(view, [r.id for r in page.items])
        return data, Page(page.total, len(page.items), page.limit, page.offset), warnings

    def _use_fit(
        self, view: GraphRead, use: str | None, source: str | None
    ) -> tuple[tuple[str, ...] | None, bool]:
        """The classes ``--for`` and ``--for-source`` allow, intersected, and whether either
        was given. ``--for`` is a relation (the candidate is its target) or
        ``<function>.<param>``, a ``ref`` parameter of a published function the actor may
        read (``read.function``); ``--for-source`` is a relation (the candidate is its source).
        None: any class."""
        fits: list[tuple[str, ...] | None] = []
        if use is not None:
            if "." in use:
                fits.append(self._parameter_fit(view, use))
            else:
                relation = resolve_term("relation", use, view.model.relations)
                fits.append(fit_classes(view.model, relation, "target"))
        if source is not None:
            relation = resolve_term("relation", source, view.model.relations)
            fits.append(fit_classes(view.model, relation, "source"))
        allowed = [set(found) for found in fits if found is not None]
        if not allowed:
            return None, bool(fits)
        return tuple(sorted(set.intersection(*allowed))), True

    def _parameter_fit(self, view: GraphRead, use: str) -> tuple[str, ...]:
        """The class of a function's ``ref`` parameter and its subclasses (``--for f.p``)."""
        name, _, param_name = use.partition(".")
        function = find_entry(self.functions(view.actor, view.entry), name)
        params = function.defn.params
        found = next((p for p in params if p.name == param_name), None)
        if found is None:
            known = [p.name for p in params]
            raise GraphCliError(
                "function.parameter_unknown",
                f"{name} has no parameter {param_name!r}"
                + (f"; it takes {', '.join(known)}" if known else " and takes no parameters"),
                details={
                    "function": name,
                    "parameter": param_name,
                    "expected": known,
                    "did_you_mean": did_you_mean(param_name, known),
                },
            )
        if found.type != "ref" or found.of_class is None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--for: parameter {param_name!r} of {name} is {found.type_text}, not a ref",
                details={"argument": "--for", "function": name, "parameter": param_name},
            )
        return view.model.subtypes(found.of_class)

    @_query
    def lookup(
        self,
        view: GraphRead,
        spec: LookupSpec,
        texts: Sequence[str] = (),
        *,
        for_use: str | None = None,
        for_source: str | None = None,
    ) -> tuple[dict[str, JsonValue], Page]:
        """Entities whose labels match, best first; one item per entity and text, its best match.

        ``texts`` (default: ``spec.text`` alone) are looked up one after the other with the
        options of ``spec``; texts with the same normalized key are looked up once. Limit and
        offset apply per text; ``data.texts`` counts the candidates and exact matches of each.

        ``for_use`` / ``for_source`` (``--for``, ``--for-source``) narrow the candidates to the
        classes that can fill the slot (see ``_use_fit``) and replace ``spec.fit``. A match of
        another class is no candidate: ``data.texts[].excluded`` counts it, ``total`` and
        ``exact`` do not, and ``data.fit`` names the options and the classes that fit.
        """
        fit, asked = self._use_fit(view, for_use, for_source)
        spec = replace(spec, fit=fit)
        plans: dict[str, tuple[str, LookupPlan]] = {}
        for text in texts or (spec.text,):
            plan = plan_lookup(view.model, replace(spec, text=text), view.entry.language)
            plans.setdefault(plan.key, (text, plan))
        revisions = view.snapshot.query(next(iter(plans.values()))[1].query).items
        items: list[JsonValue] = []
        counts: list[JsonValue] = []
        shown_ids: list[EntityId] = []
        total = more = 0
        for text, plan in plans.values():
            found = []
            excluded = 0
            for revision in revisions:
                match = match_revision(plan, revision)
                if match is None:
                    continue
                if plan.fits(revision.type):
                    found.append((rank_key(plan, revision, match), revision, match))
                else:
                    excluded += 1
            found.sort(key=lambda row: row[0])
            shown = found[plan.offset : plan.offset + plan.limit]
            items += [
                {
                    "text": text,
                    "id": str(revision.id),
                    "alias": revision.alias,
                    "type": revision.type,
                    "record_status": revision.record_status,
                    "label": main_label(plan, revision, match),
                    "match": match.to_json(),
                }
                for _, revision, match in shown
            ]
            exact = sum(1 for _, _, match in found if match.kind == "exact")
            counts.append({"text": text, "total": len(found), "exact": exact, "excluded": excluded})
            shown_ids += [revision.id for _, revision, _ in shown]
            total += len(found)
            more += plan.offset + len(shown) < len(found)
        self._signal(view, shown_ids)
        page = Page(total, len(items), spec.limit, spec.offset, more=bool(more))
        data: dict[str, JsonValue] = {"items": items, "texts": counts}
        data["fit"] = (
            {"for": for_use, "for_source": for_source, "types": None if fit is None else list(fit)}
            if asked
            else None
        )
        return data, page

    @_query
    def search(self, view: GraphRead, spec: SearchSpec) -> tuple[dict[str, JsonValue], Page]:
        """Entities whose text fields match, best first; one item per entity."""
        plan = plan_search(view.model, spec, view.entry.language)
        found = []
        for revision in view.snapshot.query(plan.query).items:
            match = search_match(plan, revision)
            if match is not None:
                found.append((search_rank(plan, revision, match), revision, match))
        found.sort(key=lambda row: row[0])
        shown = found[plan.offset : plan.offset + plan.limit]
        items: list[JsonValue] = [
            {
                "id": str(revision.id),
                "alias": revision.alias,
                "type": revision.type,
                "record_status": revision.record_status,
                "label": search_label(plan, revision, match),
                "match": match.to_json(plan),
                "fields": list(match.fields),
            }
            for _, revision, match in shown
        ]
        self._signal(view, [revision.id for _, revision, _ in shown])
        return {"items": items}, Page(len(found), len(shown), plan.limit, plan.offset)

    @_query
    def neighbors(
        self,
        view: GraphRead,
        ref: str,
        *,
        rels: Sequence[str] = (),
        direction: str = "both",
        depth: int = 1,
        fields: Sequence[str] = (),
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        shown = check_fields(view.model, (), fields)
        start, warnings = resolve_ref(ref, view.snapshot)
        if view.snapshot.get(start) is None:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(start)}
            )
        query = plan_neighbors(view.model, start, rels=rels, direction=direction, depth=depth)
        found = view.snapshot.neighbors(query)
        items: list[JsonValue] = [
            {
                "direction": "out" if edge.target == neighbor else "in",
                "edge": edge.to_json(),
                "entity": entity_json(revision, shown),
            }
            for neighbor, edge, revision in found
        ]
        entities = [start, *(neighbor for neighbor, _, _ in found)]
        self._signal(view, entities, [edge.edge_id for _, edge, _ in found])
        return {"start": str(start), "items": items}, list(warnings)

    def parse_query(self, text: str) -> CypherQuery:
        """The syntax tree of a Cypher query; needs no graph, so it runs before ``open``."""
        from graph_cli.query.cypher.parser import parse

        with self.timings.phase("parse"):
            return parse(text)

    def query(
        self, view: GraphRead, query: CypherQuery, spec: QuerySpec
    ) -> tuple[dict[str, JsonValue], Page | None]:
        """A parsed query resolved against the graph's model and, unless ``explain``,
        run over its snapshot; the rows are paged like ``find``."""
        from graph_cli.query.cypher.resolve import resolve

        with self.timings.phase("resolve"):
            resolved = resolve(query, view.model, spec.parameters)
        if spec.explain:
            return {"plan": resolved.explain()}, None
        return self._rows(
            view, resolved, spec.include_inactive, spec.budget, spec.limit, spec.offset
        )

    def _rows(
        self,
        view: GraphRead,
        resolved: ResolvedQuery,
        include_inactive: bool,
        budget: int | None,
        limit: int,
        offset: int,
        function: str | None = None,
    ) -> tuple[dict[str, JsonValue], Page]:
        """A resolved query run over the snapshot, one page of its rows, and the activity
        signal for what it read."""
        from graph_cli.query.cypher.execute import DEFAULT_BUDGET, execute

        with self.timings.phase("execute"):
            result = execute(
                resolved,
                view.model,
                view.snapshot,
                include_inactive=include_inactive,
                budget=DEFAULT_BUDGET if budget is None else budget,
            )
        shown = result.rows[offset : offset + limit]
        items: list[JsonValue] = [
            {column: value_json(value) for column, value in zip(result.columns, row, strict=True)}
            for row in shown
        ]
        # What the rows show first, then what the matches went through: a query that
        # returns only values or counts lights up the entities it read too.
        entities = dict.fromkeys([*entities_in(shown), *result.touched])
        edges = dict.fromkeys([*edges_in(shown), *result.walked])
        self._signal(view, list(entities), list(edges), function)
        data: dict[str, JsonValue] = {"columns": list(result.columns), "items": items}
        return data, Page(len(result.rows), len(shown), limit, offset)

    def run(
        self,
        view: GraphRead,
        name: str,
        pairs: Sequence[tuple[str, str]],
        *,
        limit: int,
        offset: int,
    ) -> tuple[dict[str, JsonValue], Page, tuple[WarningRecord, ...]]:
        """A published function run with ``name=value`` parameters (plan K2, K3, K5, K8).

        The caller is authorized (``read.function``, by ``open``). Values are read by the
        declared types, a ``ref`` is resolved and its class checked, and the stored
        normalized query text is parsed and resolved against the current pin: text that no
        longer does is ``function.stale``. The rows are paged and signalled like ``query``;
        the activity signal names the function, never a value."""
        from graph_cli.query.cypher.resolve import resolve

        function = find_entry(self._published(view.entry), name)
        warnings: list[WarningRecord] = []
        if function.deprecated:
            replaced = function.defn.replaced_by
            warnings.append(
                WarningRecord(
                    "function.deprecated",
                    f"function {name} is deprecated"
                    + (f"; use {replaced}" if replaced is not None else ""),
                    {"name": name, "replaced_by": replaced},
                )
            )
        with self.timings.phase("query"):
            bound = bind_cli(name, function.defn.params, pairs, view.model)
            for param in function.defn.params:
                if param.type == "ref":
                    bound[param.name], found = self._entity_params(view, name, param, bound)
                    warnings += found
        try:
            parsed = self.parse_query(function.query)
            with self.timings.phase("resolve"):
                resolved = resolve(parsed, view.model, bound)
        except GraphCliError as exc:
            raise GraphCliError(
                "function.stale",
                f"function {name} no longer fits the graph's ontology ({exc.code}); "
                "tell the maintainer of this graph",
                details={"function": name, "hash": function.hash, "cause": exc.code},
            ) from exc
        data, page = self._rows(view, resolved, False, None, limit, offset, name)
        data = {"function": {"name": name, "hash": function.hash}, **data}
        return data, page, tuple(warnings)

    def _entity_params(
        self, view: GraphRead, function: str, param: ParamDef, bound: Mapping[str, Value]
    ) -> tuple[Value, list[WarningRecord]]:
        """The canonical ids for the reference texts of a ``ref`` parameter, each checked to
        exist and to be of the declared class or a subclass (no name resolution)."""
        assert param.of_class is not None
        given = bound[param.name]
        texts = given if isinstance(given, list) else [given]
        warnings: list[WarningRecord] = []
        ids: list[Value] = []
        for text in texts:
            assert isinstance(text, str)
            entity, found = resolve_ref(text, view.snapshot)
            warnings += found
            revision = view.snapshot.get(entity)
            if revision is None:
                raise GraphCliError(
                    "entity.not_found",
                    f"no entity {text} (parameter {param.name!r} of {function})",
                    details={"ref": text, "id": str(entity), "function": function},
                )
            if not view.model.is_a(revision.type, param.of_class):
                raise GraphCliError(
                    "function.parameter_type",
                    f"parameter {param.name!r} of {function} expects {param.of_class}, "
                    f"{text} is a {revision.type}",
                    details={
                        "function": function,
                        "parameter": param.name,
                        "expected": param.of_class,
                        "found": revision.type,
                    },
                )
            ids.append(str(entity))
        return (ids if isinstance(given, list) else ids[0]), warnings

    def whoami(
        self, actor: ActorContext, *, allowed: Sequence[str] | None = None
    ) -> dict[str, JsonValue]:
        """B2.10: the actor and what the policy lets it do, overall and per graph.

        ``allowed`` and ``obligations`` answer for a graph the policy names nowhere;
        ``graphs`` lists the graphs ``graph graphs`` lists (``allowed`` is the pointer
        allowlist), with their level, type limits and write obligations. Works without a
        data root: there are no graphs then."""
        policy = self._policy.current(None)
        ctx = AuthzContext(op_count=1, has_reason=False, dry_run=False)
        actions: list[JsonValue] = []
        obligations: dict[str, JsonValue] = {}
        for action in sorted((READ_ACTIONS | WRITE_ACTIONS) - RESERVED_ACTIONS):
            decision = authorize(actor, action, _NO_GRAPH, ctx, policy)
            if decision.effect == "allow":
                actions.append(action)
            elif decision.code == "access.reason_required":
                actions.append(action)
                obligations[action] = {"require_reason": True}
        graphs: list[JsonValue] = []
        for entry in self._listed():
            if allowed is not None and entry.name not in allowed and str(entry.id) not in allowed:
                continue
            if not self.visible(actor, entry):
                continue
            layered = self._policy.current(entry.id)
            graphs.append(
                {
                    "graph": entry.name,
                    "level": graph_level(actor, entry.id, layered),
                    "withheld": withheld_actions(actor, entry.id, layered),
                    "types": type_limits(layered),
                    "obligations": write_obligations(actor, entry.id, layered),
                }
            )
        return {
            "actor": actor.to_json(),
            "permissions": {
                "policy": str(policy.documents[0].get("id")),
                "policy_hash": policy.hash,
                "scope": scope_of(actor, policy),
                "allowed": actions,
                "obligations": obligations,
                "graphs": graphs,
            },
        }

    def _listed(self) -> Sequence[GraphEntry]:
        """Every readable graph; none without a data root (``whoami`` still answers)."""
        try:
            return self._catalog.list_graphs(skip_unreadable=True)
        except GraphCliError as exc:
            if exc.code != "backend.unavailable":
                raise
            return ()


def _snapshot_as_of(
    read: GraphStoreRead, head: Snapshot, entry: GraphEntry, as_of: AsOf
) -> tuple[Snapshot, WarningRecord]:
    """The snapshot at the seq ``as_of`` names, and the warning that says so (B8.8)."""
    if CAPABILITY_SNAPSHOT_AT_SEQ not in read.capabilities():
        raise GraphCliError(
            "temporal.as_of_unsupported",
            "the storage backend of this graph cannot read an earlier state",
            details={"as_of": as_of_text(as_of)},
        )
    seq = seq_at(head, as_of)
    snapshot = head if seq == head.seq else read.snapshot(at_seq=seq)
    tx = snapshot.tx(seq)
    version = tx.ontology.version if tx is not None else None
    message = f"read as of tx:{seq}; the graph is at tx:{head.seq}"
    if version is not None and version != entry.pin.version:
        message += (
            f"; that state was written under ontology {version}, names follow {entry.pin.version}"
        )
    details: dict[str, JsonValue] = {
        "as_of": as_of_text(as_of),
        "seq": seq,
        "head_seq": head.seq,
        "ontology_version": version,
    }
    return snapshot, WarningRecord("temporal.as_of_read", message, details)


def _sources(snapshot: Snapshot, entity: EntityId, rel: str) -> list[str]:
    return [str(source) for source, _ in snapshot.incoming(entity, TermRef(rel))]


def denial_error(decision: Decision) -> GraphCliError:
    """The error for a denial on a visible graph or the installation (B6.15)."""
    assert decision.code is not None
    message = f"{decision.action} on {decision.resource} is not permitted"
    if decision.code == "access.denied":
        message += f"; {ACCESS_DENIED_HINT}"
    return GraphCliError(
        decision.code,
        message,
        details={
            "action": decision.action,
            "resource": decision.resource,
            "rule": decision.rule_id,
        },
    )
