"""The installation-level reads: the graphs a caller may list and who the caller is."""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.effective import (
    graph_level,
    holds_every_marking,
    marking_allowance,
    scope_of,
    type_limits,
    visible_limits,
    withheld_actions,
    write_allowance,
    write_grant_types,
    write_obligations,
)
from graph_cli.access_control.terms import TermModel
from graph_cli.kernel.actions import (
    ENTRY_ACTIONS,
    READ_ACTIONS,
    RESERVED_ACTIONS,
    WRITE_ACTIONS,
)
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.read.catalogs import ReadCatalogs
from graph_cli.services.read.graph_read import graph_json
from graph_cli.services.term_model import term_model

_NO_GRAPH = "graph:00000000-0000-0000-0000-000000000000"  # whoami: a graph no policy names


class ReadInstallation(ReadCatalogs):
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
        for action in sorted((READ_ACTIONS | WRITE_ACTIONS) - RESERVED_ACTIONS - ENTRY_ACTIONS):
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
            level = graph_level(actor, entry.id, layered)
            limits = type_limits(layered)
            item: dict[str, JsonValue] = {
                "graph": entry.name,
                "level": level,
                "withheld": withheld_actions(actor, entry.id, layered),
                "types": limits,
                "obligations": write_obligations(actor, entry.id, layered),
            }
            visible = self._seen_types(actor, entry)
            markings = self._held_markings(actor, entry, layered)
            if visible is None and markings is None:
                # The model is read only for a caller a write allow list names types for
                granted = write_grant_types(actor, entry.id, layered)
                terms = self._term_model(entry) if granted else None
            else:
                # A caller who sees some types or attributes only gets everything over what it
                # sees: no hidden type, relation or attribute is named (V8, F8)
                terms = self._visible_terms(actor, entry)
                if visible is not None:
                    item["reads"] = sorted(visible)
                    item["types"] = visible_limits(limits, terms.types)
                if markings is not None:
                    item["markings"] = markings
            writes = write_allowance(actor, entry.id, layered, terms, level=level)
            if writes is not None:
                item["writes"] = writes
            seen = self.seen_catalog(actor, entry)
            if seen is not None:
                item["catalog"] = seen
            graphs.append(item)
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

    def _seen_types(self, actor: ActorContext, entry: GraphEntry) -> frozenset[str] | None:
        """The types ``actor`` sees in the graph (``None``: all), for ``whoami``. A graph whose
        model cannot be read is shown as seen by nobody: fail closed, ``whoami`` still answers."""
        try:
            return self.visible_types(actor, entry)
        except GraphCliError:
            return frozenset()

    def _held_markings(
        self, actor: ActorContext, entry: GraphEntry, policy: PolicySet
    ) -> list[JsonValue] | None:
        """The markings ``actor`` holds in the graph when its pinned model uses markings and
        the actor lacks one of them (``None``: nothing to say), for ``whoami``; a marking the
        actor lacks is never named. A graph whose model cannot be read is shown as held by
        nobody (``[]``) unless the actor holds every marking whatever the model declares:
        fail closed, ``whoami`` still answers."""
        try:
            used = self._models.used_markings(entry)
        except GraphCliError:
            return None if holds_every_marking(actor, entry.id, policy) else []
        held = marking_allowance(actor, entry.id, policy, used)
        return None if held is None else list(held)

    def _visible_terms(self, actor: ActorContext, entry: GraphEntry) -> TermModel:
        """The terms of the model view of ``actor``; none when the model cannot be read."""
        try:
            model, _ = self._models.load_for_read(entry)
            view = self.read_view(actor, entry)
        except GraphCliError:
            return TermModel({})
        return term_model(self._visibility.model(entry, model, view))

    def _term_model(self, entry: GraphEntry) -> TermModel | None:
        """The graph's pinned model condensed to what term rules need, cached per pin;
        ``None`` when the model cannot be read (``whoami`` still answers, on types alone)."""
        key = (entry.id, entry.pin)
        found = self._terms.get(key)
        if found is None:
            try:
                model, _ = self._models.load_for_read(entry)
            except GraphCliError:
                return None
            found = self._terms[key] = term_model(model)
        return found

    def _listed(self) -> Sequence[GraphEntry]:
        """Every readable graph; none without a data root (``whoami`` still answers)."""
        try:
            return self._catalog.list_graphs(skip_unreadable=True)
        except GraphCliError as exc:
            if exc.code != "backend.unavailable":
                raise
            return ()
