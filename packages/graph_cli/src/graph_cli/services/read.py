"""Read services: graphs, get, find, neighbors, whoami (contracts §4.3, B2.10, B6.3, B6.12, B8.8,
B8.12, B8.13, B9.7).

Every read authorizes its action before touching storage; a denial is
appended to the installation stream and raised. Reads write nothing else.
A read takes one lock-free snapshot and answers everything from it, so the
envelope's ``graph.seq`` names the state that was read. A pinned artifact
that does not match its hash is read with ``ontology.pin_mismatch_read``,
an archived graph with ``graph.archived_read``. ``as_of`` is reserved: v1
refuses it with ``temporal.as_of_unsupported``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.policy import BUILTIN_POLICY
from graph_cli.audit import events
from graph_cli.kernel.actions import READ_ACTIONS, RESERVED_ACTIONS, WRITE_ACTIONS, Action
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import Clock, format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, EventId, IdProvider, TermRef
from graph_cli.kernel.ports.audit import AuditSink
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import EnvInfo, RequestMeta, Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.plan import FindSpec, plan_find, plan_neighbors
from graph_cli.services.models import ModelCache
from graph_cli.services.refs import resolve_ref

SUPERSEDES = "core:supersedes"
RETRACTS = "core:retracts"
_NO_GRAPH = "graph:00000000-0000-0000-0000-000000000000"  # whoami: any graph resource


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
    ) -> None:
        self._catalog = catalog
        self._audit = audit
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
        assert decision.code is not None
        message = f"{action} on {resource} is not permitted"
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
            raise GraphCliError(decision.code, message, warnings=(warning,)) from exc
        raise GraphCliError(decision.code, message)

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

    def graphs(
        self, actor: ActorContext, request: RequestMeta, *, allowed: Sequence[str] | None
    ) -> tuple[list[JsonValue], int, tuple[WarningRecord, ...]]:
        """B6.12: graphs the actor may list, narrowed by the pointer allowlist."""
        self.authorize(actor, "read.list", "installation", request)
        entries = self._catalog.list_graphs(skip_unreadable=True)
        shown = [e for e in entries if allowed is None or e.name in allowed or str(e.id) in allowed]
        items: list[JsonValue] = []
        for entry in shown:
            item = graph_json(entry, None)
            del item["seq"]
            items.append(item)
        return items, len(entries) - len(shown), self._catalog.warnings

    def get(self, view: GraphRead, ref: str) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        entity, ref_warnings = resolve_ref(ref, view.snapshot)
        revision = view.snapshot.get(entity)
        if revision is None:
            raise GraphCliError(
                "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
            )
        warnings = list(ref_warnings)
        superseded_by = _sources(view.snapshot, entity, SUPERSEDES)
        retracted_by = _sources(view.snapshot, entity, RETRACTS)
        if revision.record_status == "superseded":
            warnings.append(
                WarningRecord(
                    "entity.superseded",
                    f"{ref} is superseded; it is shown as recorded, without redirect",
                    {"id": str(entity), "superseded_by": list(superseded_by)},
                )
            )
        elif revision.record_status == "retracted":
            warnings.append(
                WarningRecord("entity.retracted", f"{ref} is retracted", {"id": str(entity)})
            )
        data: dict[str, JsonValue] = {
            "entity": entity_json(revision),
            "superseded_by": list(superseded_by),
            "retracted_by": list(retracted_by),
        }
        return data, warnings

    def find(
        self,
        view: GraphRead,
        spec: FindSpec,
        *,
        rel_refs: Sequence[tuple[str, str | None]] = (),
        in_rel_refs: Sequence[tuple[str, str | None]] = (),
    ) -> tuple[dict[str, JsonValue], Page, list[WarningRecord]]:
        warnings: list[WarningRecord] = []

        def resolve(
            pairs: Sequence[tuple[str, str | None]],
        ) -> tuple[tuple[str, EntityId | None], ...]:
            resolved = []
            for rel, text in pairs:
                entity = None
                if text is not None:
                    entity, found = resolve_ref(text, view.snapshot)
                    warnings.extend(found)
                resolved.append((rel, entity))
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
        return data, Page(page.total, len(page.items), page.limit, page.offset), warnings

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
        items: list[JsonValue] = [
            {
                "direction": "out" if edge.target == neighbor else "in",
                "edge": edge.to_json(),
                "entity": entity_json(revision),
            }
            for neighbor, edge, revision in view.snapshot.neighbors(query)
        ]
        return {"start": str(start), "items": items}, list(warnings)

    def whoami(self, actor: ActorContext) -> dict[str, JsonValue]:
        """B2.10: the actor and what the built-in policy lets it do."""
        policy = self._policy.current(None)
        ctx = AuthzContext(op_count=1, has_reason=False, dry_run=False)
        allowed: list[JsonValue] = []
        obligations: dict[str, JsonValue] = {}
        for action in sorted((READ_ACTIONS | WRITE_ACTIONS) - RESERVED_ACTIONS):
            resource = "installation" if action in _INSTALLATION_READS else _NO_GRAPH
            decision = authorize(actor, action, resource, ctx, policy)
            if decision.effect == "allow":
                allowed.append(action)
            elif decision.code == "access.reason_required":
                allowed.append(action)
                obligations[action] = {"require_reason": True}
        return {
            "actor": actor.to_json(),
            "permissions": {
                "policy": str(BUILTIN_POLICY.get("id")),
                "policy_hash": policy.hash,
                "allowed": allowed,
                "obligations": obligations,
            },
        }


_INSTALLATION_READS = frozenset({Action("read.list"), Action("read.describe")})


def _sources(snapshot: Snapshot, entity: EntityId, rel: str) -> list[str]:
    return [str(source) for source, _ in snapshot.incoming(entity, TermRef(rel))]
