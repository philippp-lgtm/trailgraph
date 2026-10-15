"""What every admin service gets from the composition root.

Built by ``graph_cli_admin.maintenance_cli.wiring`` only; services never
read the environment, the clock or the filesystem layout on their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from graph_cli.access_control.authorize import authorize
from graph_cli.audit import events
from graph_cli.catalog.config import Config
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.catalog.resolve import Roots
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import Clock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId, GraphId, IdProvider
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.policy import PolicySource
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import EnvInfo, RequestMeta
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.maintenance import FileStoreMaintenance


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

    def intent(
        self,
        session: MaintenanceSession,
        entry: GraphEntry,
        decisions: tuple[Decision, ...],
        meta: RequestMeta,
        change: Mapping[str, JsonValue],
        *,
        reason: str | None = None,
    ) -> AuditReceipt:
        """The graph-stream intent of a maintenance effect (W.e), durable on return."""
        event = events.maintenance(
            actor=self.actor,
            decisions=decisions,
            request=meta,
            ts=self.clock.now(),
            event_id=self.event_id(),
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
