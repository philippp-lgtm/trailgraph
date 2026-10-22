"""Read services: graphs, get, history, find, lookup, neighbors, whoami (contracts §4.3,
B2.10, B6.3, B6.12, B8.8, B8.12, B8.13, B8.15, B9.7).

Every read authorizes its action before touching storage; a denial is
appended to the installation stream and raised. A graph is visible iff
``read.list`` on it is allowed; a denial on a graph the actor cannot see is
raised as ``graph.not_found``, exactly like a graph that does not exist
(B6.15), and still audited. Reads write nothing else.
A read takes one lock-free snapshot and answers everything from it, so the
envelope's ``graph.seq`` names the state that was read. A pinned artifact
that does not match its hash is read with ``ontology.pin_mismatch_read``,
an archived graph with ``graph.archived_read``. ``as_of`` is reserved: v1
refuses it with ``temporal.as_of_unsupported``. A successful ``get``,
``history``, ``find``, ``lookup`` or ``neighbors`` hands the ids it returns
to the activity sink (a transient signal for ``graph-admin view``, no log).
``history`` shows of a tx's actor only whether it is the caller's own
session: who wrote a revision is audit content (B8.15).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
from graph_cli.kernel.ids import EntityId, EventId, IdProvider, TermRef
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ActivitySink
from graph_cli.kernel.ports.audit import AuditSink
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry, SkillSlot, graph_not_found
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import EnvInfo, RequestMeta, Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.lookup import LookupSpec, main_label, match_revision, plan_lookup, rank_key
from graph_cli.query.plan import FindSpec, plan_find, plan_neighbors
from graph_cli.services.describe import describe_model
from graph_cli.services.models import ModelCache
from graph_cli.services.refs import resolve_ref
from graph_cli.temporal_knowledge.history import changed_names

SUPERSEDES = "core:supersedes"
RETRACTS = "core:retracts"
_NO_GRAPH = "graph:00000000-0000-0000-0000-000000000000"  # whoami: a graph no policy names
_VISIBLE = AuthzContext(op_count=0, has_reason=False, dry_run=False)
ACCESS_DENIED_HINT = "ask the maintainer of this installation; do not work around it"


@dataclass(frozen=True, slots=True)
class GraphRead:
    """One graph opened for reading: model and one snapshot."""

    entry: GraphEntry
    model: CompiledModel
    snapshot: Snapshot
    warnings: tuple[WarningRecord, ...]

    def envelope_graph(self) -> dict[str, JsonValue]:
        return graph_json(self.entry, self.snapshot.seq)


@dataclass(frozen=True, slots=True)
class Page:
    total: int
    shown: int
    limit: int
    offset: int

    def to_json(self) -> dict[str, JsonValue]:
        truncated = self.offset + self.shown < self.total
        return {
            "total": self.total,
            "shown": self.shown,
            "limit": self.limit,
            "offset": self.offset,
            "truncated": truncated,
            "next_offset": self.offset + self.shown if truncated else None,
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


def entity_json(revision: Revision) -> dict[str, JsonValue]:
    """An entity as reads show it: every revision field, explicit nulls (C.6 envelope rule)."""
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


def refuse_as_of(as_of: str | None) -> None:
    if as_of is not None:
        raise GraphCliError(
            "temporal.as_of_unsupported",
            "reading as of an earlier state is not supported yet",
            details={"as_of": as_of},
        )


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
    ) -> None:
        self._catalog = catalog
        self._audit = audit
        self.activity = activity
        self._policy = policy
        self._clock = clock
        self._ids = ids
        self._env = env
        self._models = ModelCache(catalog)

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
        policy = self._policy.current(entry.id)
        return authorize(actor, "read.list", f"graph:{entry.id}", _VISIBLE, policy).effect == (
            "allow"
        )

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
            self._audit.append_installation(event)
        except GraphCliError as exc:
            warning = WarningRecord(
                "audit.refusal_not_recorded", "the denial could not be audited", {"cause": exc.code}
            )
            return GraphCliError(
                error.code, error.message, details=error.details, warnings=(warning,)
            )
        return error

    # ---- reads ---------------------------------------------------------

    def open(
        self, actor: ActorContext, entry: GraphEntry, action: str, request: RequestMeta
    ) -> GraphRead:
        self.authorize(actor, action, f"graph:{entry.id}", request, entry)
        model, warnings = self._models.load_for_read(entry)
        if entry.state == "archived":
            warnings += (
                WarningRecord("graph.archived_read", f"graph {entry.name} is archived", {}),
            )
        read, _ = self._catalog.store_for(entry.id)
        snapshot = read.snapshot()
        return GraphRead(entry, model, snapshot, (*warnings, *snapshot.warnings))

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
        graph's current pin, as ``graph describe`` prints them (``read.describe``)."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        model, warnings = self._models.load_for_read(entry)
        return describe_model(entry, model), warnings

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

    def get(
        self, view: GraphRead, ref: str, *, rev: int | None = None
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """The current revision, or revision ``rev``; the status warnings and
        ``superseded_by``/``retracted_by`` always describe the current state."""
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
            "entity": entity_json(revision),
            "current_rev": current.rev,
            "superseded_by": list(superseded_by),
            "retracted_by": list(retracted_by),
        }
        self.activity.reads(view.entry.id, [entity])
        return data, warnings

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
        self.activity.reads(view.entry.id, [entity])
        return data, Page(len(metas), len(shown), limit, offset), list(warnings)

    def find(
        self,
        view: GraphRead,
        spec: FindSpec,
        *,
        rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        in_rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        warnings: list[WarningRecord] = []

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
        data: dict[str, JsonValue] = {"items": [entity_json(r) for r in page.items]}
        self.activity.reads(view.entry.id, [r.id for r in page.items])
        return data, Page(page.total, len(page.items), page.limit, page.offset), warnings

    def lookup(self, view: GraphRead, spec: LookupSpec) -> tuple[dict[str, JsonValue], Page]:
        """Entities whose labels match, best first; one item per entity, its best match."""
        plan = plan_lookup(view.model, spec)
        found = []
        for revision in view.snapshot.query(plan.query).items:
            match = match_revision(plan, revision)
            if match is not None:
                found.append((rank_key(plan, revision, match), revision, match))
        found.sort(key=lambda row: row[0])
        shown = found[plan.offset : plan.offset + plan.limit]
        items: list[JsonValue] = [
            {
                "id": str(revision.id),
                "alias": revision.alias,
                "type": revision.type,
                "record_status": revision.record_status,
                "label": main_label(plan, revision),
                "match": match.to_json(),
            }
            for _, revision, match in shown
        ]
        self.activity.reads(view.entry.id, [revision.id for _, revision, _ in shown])
        return {"items": items}, Page(len(found), len(shown), plan.limit, plan.offset)

    def neighbors(
        self,
        view: GraphRead,
        ref: str,
        *,
        rels: Sequence[str] = (),
        direction: str = "both",
        depth: int = 1,
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
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
                "entity": entity_json(revision),
            }
            for neighbor, edge, revision in found
        ]
        self.activity.reads(view.entry.id, [start, *(neighbor for neighbor, _, _ in found)])
        return {"start": str(start), "items": items}, list(warnings)

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
