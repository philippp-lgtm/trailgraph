"""Who may read what: authorization, the read view of a caller and the audit of a denial."""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.access_control.authorize import authorize, authorize_entry
from graph_cli.access_control.effective import CatalogGrants
from graph_cli.audit import events
from graph_cli.kernel.actions import ENTRY_KINDS
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.ports.catalog import GraphEntry, graph_not_found
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.catalog_access import EntryRef
from graph_cli.services.read.state import ReadState
from graph_cli.services.visibility import VisibleSnapshot

VISIBLE = AuthzContext(op_count=0, has_reason=False, dry_run=False)


ACCESS_DENIED_HINT = "ask the maintainer of this installation; do not work around it"


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


class ReadAccess(ReadState):
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
            decision = authorize(actor, "read.list", f"graph:{entry.id}", VISIBLE, policy)
        return decision.effect == "allow"

    def read_view(self, actor: ActorContext, entry: GraphEntry) -> ReadView:
        """What ``actor`` sees in the graph (what ``open`` puts into ``GraphRead.read_view``).
        The model is read once per pin, and again only when the graph is read limited or its
        pin uses markings and the answer is not cached."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            return self._visibility.lazy(
                actor,
                entry,
                policy,
                lambda: self._models.load_for_read(entry)[0],
                lambda: self._models.used_markings(entry),
            )

    def catalog_scope(self, actor: ActorContext, entry: GraphEntry) -> CatalogGrants | None:
        """The entries ``actor``'s grants name when they are catalog grants only (everything the
        caller sees of the graph's catalog is among them), ``None`` otherwise. Cached per policy
        hash, graph and actor key; a pure read of the policy, nothing is decided or recorded."""
        with self.timings.phase("authorize"):
            return self._catalog_filter.scope(actor, entry.id, self._policy.current(entry.id))

    def entry_decisions(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        ref: EntryRef,
        needs: Sequence[str],
    ) -> tuple[Decision, tuple[Decision, ...]]:
        """What the policy decides for ``actor`` using the catalog entry ``ref`` of the graph, a
        probe (nothing is recorded): the entry decision (``authorize`` on the entry resource with
        the action of its kind) and, when it allows, the decisions that deny what the entry needs
        (``needs``: actions on the graph, each asked *deny-only* through ``authorize_entry``, so no
        rule has to allow them again and any rule that denies them still wins). The second item is
        ``(entry decision,)`` when the entry decision itself denies and empty when the actor may
        use the entry. Made under one policy, so the deny-only decisions always follow the entry
        decision."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            granted = authorize(actor, ref.action, ref.resource(entry.id), VISIBLE, policy)
            if granted.effect != "allow":
                return granted, (granted,)
            graph = f"graph:{entry.id}"
            asked = (authorize_entry(actor, granted, a, graph, VISIBLE, policy) for a in needs)
            return granted, tuple(d for d in asked if d.effect != "allow")

    def visible_types(self, actor: ActorContext, entry: GraphEntry) -> frozenset[str] | None:
        """The concrete types ``actor`` sees in the graph, ``None`` for no limit (the type
        half of ``read_view``; the model is read only when the graph is type limited)."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            return self._visibility.types(
                actor,
                entry,
                policy,
                lambda: self._models.load_for_read(entry)[0],
                lambda: self._models.used_markings(entry),
            )

    def limited_view(
        self, actor: ActorContext, entry: GraphEntry, snapshot: Snapshot, model: CompiledModel
    ) -> tuple[Snapshot, CompiledModel, ReadView]:
        """``snapshot`` and ``model`` as ``actor`` reaches them, and its read view (both
        returned as given when nothing is limited). For the services that look things up for
        a write (action parameters, file content)."""
        view = self.read_view(actor, entry)
        if not view.limited:
            return snapshot, model, view
        return (
            VisibleSnapshot(snapshot, view),
            self._visibility.model(entry, model, view),
            view,
        )

    def _probe(self, actor: ActorContext, entry: GraphEntry, action: str) -> Decision:
        """What the policy decides for ``actor`` doing ``action`` on the graph: a probe, nothing
        is decided on the record."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            return authorize(actor, action, f"graph:{entry.id}", VISIBLE, policy)

    def may(self, actor: ActorContext, entry: GraphEntry, action: str) -> bool:
        """Whether the policy lets ``actor`` do ``action`` on the graph (``_probe``): a probe,
        nothing is recorded (for listings that depend on a right, like the tool manifest)."""
        return self._probe(actor, entry, action).effect == "allow"

    def lookup_served(self, actor: ActorContext, entry: GraphEntry) -> bool:
        """Whether the ``lookup`` tool is served to ``actor``: it holds ``read.find`` on the graph,
        or its grants are catalog grants only and some entry they name lets it ``read.find``
        deny-only (``entry_decisions``: what a lookup for that entry's reference input asks), so
        that a rule that denies ``read.find`` takes the tool away. The one predicate behind the
        tool manifest, ``describe tools`` and ``call lookup``: a probe, nothing is recorded."""
        if self.may(actor, entry, "read.find"):
            return True
        scope = self.catalog_scope(actor, entry)
        if scope is None:
            return False
        return any(
            not self.entry_decisions(actor, entry, EntryRef(kind, name), ("read.find",))[1]
            for kind in ENTRY_KINDS
            for name in scope.names(kind)
        )

    def refuse_hidden(
        self, actor: ActorContext, entry: GraphEntry, ref: str, request: RequestMeta
    ) -> GraphCliError:
        """A selected graph the actor cannot see: the ``read.list`` denial is audited, the
        caller gets ``graph.not_found`` for ``ref`` (Q4: no existence leak)."""
        policy = self._policy.current(entry.id)
        decision = authorize(actor, "read.list", f"graph:{entry.id}", VISIBLE, policy)
        return self._record_denial(actor, decision, request, entry, graph_not_found(ref))

    def _record_denial(
        self,
        actor: ActorContext,
        decision: Decision,
        request: RequestMeta,
        graph: GraphEntry | None,
        error: GraphCliError,
        before: Sequence[Decision] = (),
    ) -> GraphCliError:
        """Audit the refusal ``decision`` stands for and return ``error``; ``before`` are the
        decisions that allowed the steps up to it (the entry decision of a caller that works
        through a catalog grant), so the event names the grant that let the request get that
        far."""
        assert decision.code is not None
        event = events.refusal(
            status="denied",
            code=decision.code,
            actor=actor,
            decisions=[*before, decision],
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
