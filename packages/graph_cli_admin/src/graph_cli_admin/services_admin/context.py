"""What every admin service gets from the composition root.

Built by ``graph_cli_admin.maintenance_cli.wiring`` only; services never
read the environment, the clock or the filesystem layout on their own.
Services resolve a graph reference through ``AdminContext.resolve``, never
the catalog: a project admin (an admin token bound to a project) sees only
the graphs it maintains.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.access_control.authorize import authorize, is_project_admin
from graph_cli.access_control.effective import maintains
from graph_cli.audit import events
from graph_cli.catalog.config import Config
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.catalog.resolve import Roots
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import Clock
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EventId, GraphId, IdProvider
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, graph_not_found
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import EnvInfo, RequestMeta
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.maintenance import FileStoreMaintenance

# The action whose denial a project admin's look at another graph records.
VISIBLE: Final = "maint.graph_configure"


@dataclass(frozen=True)
class AdminContext:
    roots: Roots
    config: Config
    actor: ActorContext
    clock: Clock
    ids: IdProvider
    catalog: FileCatalog
    audit: FileAuditLog
    maintenance: FileStoreMaintenance
    policy: PolicySource
    env: EnvInfo
    lock_timeout_s: float

    def event_id(self) -> EventId:
        return EventId(self.ids.new_id())

    @property
    def project_admin(self) -> bool:
        return is_project_admin(self.actor)

    def resolve(self, ref: str, command: str) -> GraphEntry:
        """The graph ``ref`` names. A project admin sees only the graphs it maintains;
        any other is ``graph.not_found`` as if it did not exist (no existence leak, as in
        the app, B6.15), its denial of ``maint.graph_configure`` audited."""
        if not self.project_admin:
            return self.catalog.resolve(ref)
        found = self.catalog.manifests(include_archived=True, skip_unreadable=True)
        entry = next(
            (m.entry for m in found if m.entry.name == ref or str(m.entry.id) == ref.lower()),
            None,
        )
        if entry is None:
            raise graph_not_found(ref)
        if not maintains(self.actor, entry.id, self.policy.current(entry.id)):
            try:
                self.authorize(
                    [(VISIBLE, f"graph:{entry.id}")],
                    request=RequestMeta.of(command, {"graph": ref}),
                    graph=entry.id,
                )
            except GraphCliError as exc:
                if not exc.code.startswith("access."):
                    raise
            raise graph_not_found(ref)
        return entry

    def manifests(
        self, *, include_archived: bool, skip_unreadable: bool = False
    ) -> list[ManifestData]:
        """The graphs' manifests; a project admin's are those of the graphs it maintains
        (an unreadable manifest of another graph neither shows nor stops it).
        ``skip_unreadable`` leaves out every unreadable one, with a catalog warning."""
        if not self.project_admin:
            return list(
                self.catalog.manifests(
                    include_archived=include_archived, skip_unreadable=skip_unreadable
                )
            )
        found = self.catalog.manifests(include_archived=include_archived, skip_unreadable=True)
        return [
            m for m in found if maintains(self.actor, m.entry.id, self.policy.current(m.entry.id))
        ]

    def catalog_warnings(self) -> tuple[WarningRecord, ...]:
        """What the catalog found reading manifests; a project admin's only about the graphs
        it maintains (others would name graphs it cannot see)."""
        warnings = self.catalog.warnings
        if not self.project_admin:
            return warnings
        seen = {str(m.entry.id) for m in self.manifests(include_archived=True)}
        return tuple(w for w in warnings if any(graph in str(dict(w.details)) for graph in seen))

    def read_data(self, entry: GraphEntry, request: RequestMeta) -> None:
        """A project admin's command that answers with the graph's data (check and migration
        findings quote values) also needs ``read.get`` on the graph, so the policy's deny
        rules on reads hold for it; a maintainer reads the data root anyway."""
        if self.project_admin:
            self.authorize([("read.get", f"graph:{entry.id}")], request=request, graph=entry.id)

    def authorize(
        self,
        pairs: list[tuple[str, str]],
        *,
        request: RequestMeta,
        graph: GraphId | None = None,
        reason: str | None = None,
    ) -> tuple[Decision, ...]:
        """B6.3: decide every (action, resource) pair; a denial is audited and raised."""
        policy = self.policy.current(graph)
        ctx = AuthzContext(op_count=1, has_reason=reason is not None, dry_run=False)
        decisions = tuple(authorize(self.actor, a, r, ctx, policy) for a, r in pairs)
        denied = [d for d in decisions if d.effect == "deny"]
        if denied:
            first = denied[0]
            assert first.code is not None
            self.audit.append_installation(
                events.refusal(
                    status="denied",
                    code=first.code,
                    actor=self.actor,
                    decisions=decisions,
                    request=request,
                    ts=self.clock.now(),
                    event_id=self.event_id(),
                    env=self.env,
                    reason=reason,
                )
            )
            raise GraphCliError(first.code, f"{first.action} on {first.resource} is not permitted")
        return decisions

    def decide(self, action: str, resource: str, *, graph: GraphId | None = None) -> Decision:
        """What ``authorize`` would decide for the pair, without auditing a denial."""
        policy = self.policy.current(graph)
        ctx = AuthzContext(op_count=1, has_reason=False, dry_run=False)
        return authorize(self.actor, action, resource, ctx, policy)

    def allows(self, action: str, resource: str, *, graph: GraphId | None = None) -> bool:
        """Whether ``authorize`` would permit the pair; nothing is audited, so a listing
        can leave out what the caller may not see without recording a denial per entry."""
        return self.decide(action, resource, graph=graph).effect != "deny"

    def current_decisions(
        self,
        entry: GraphEntry,
        decisions: tuple[Decision, ...],
        meta: RequestMeta,
        *,
        reason: str | None = None,
    ) -> tuple[Decision, ...]:
        """``decisions``, made again when the graph's policy set differs from theirs: under
        the session's lock the policy in effect decides (B6.14), with the graph's own layer."""
        self.policy.refresh()
        if all(d.policy_hash == self.policy.current(entry.id).hash for d in decisions):
            return decisions
        return self.authorize(
            [(d.action, d.resource) for d in decisions],
            request=meta,
            graph=entry.id,
            reason=reason,
        )

    def intent(
        self,
        session: MaintenanceSession,
        entry: GraphEntry,
        decisions: tuple[Decision, ...],
        meta: RequestMeta,
        change: Mapping[str, JsonValue],
        *,
        reason: str | None = None,
        event_id: EventId | None = None,
    ) -> AuditReceipt:
        """The graph-stream intent of a maintenance effect (W.e), durable on return;
        ``event_id`` when the effect needs it in advance (the tx of a purge).

        The decisions are made again when the graph's policy set differs from theirs
        (``current_decisions``), as ``audit verify`` expects for a graph-stream event.
        """
        decisions = self.current_decisions(entry, decisions, meta, reason=reason)
        event = events.maintenance(
            actor=self.actor,
            decisions=decisions,
            request=meta,
            ts=self.clock.now(),
            event_id=event_id or self.event_id(),
            env=self.env,
            graph=entry,
            change=change,
            reason=reason,
        )
        return session.append_audit(event)

    def anchor(
        self,
        entry: GraphEntry,
        decisions: tuple[Decision, ...],
        meta: RequestMeta,
        receipt: AuditReceipt,
        *,
        reason: str | None = None,
    ) -> None:
        """The installation event anchoring a committed intent (B5.2, B5.11)."""
        event = events.maintenance(
            actor=self.actor,
            decisions=decisions,
            request=meta,
            ts=self.clock.now(),
            event_id=self.event_id(),
            env=self.env,
            graph=entry,
            reason=reason,
            anchors=[receipt],
        )
        self.audit.append_installation(event)
