"""The published functions, actions and views of a graph as an actor may see them."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graph_cli.access_control.effective import write_obligations
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.ports.catalog import FunctionsSlot, GraphEntry
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.kinetic.action_needs import NeedOptions
from graph_cli.kinetic.action_needs import action_needs as derive_needs
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.params import Problem
from graph_cli.kinetic.view_entries import ViewEntry, load_view_entries
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.action_rights import need_decisions
from graph_cli.services.catalog_access import (
    Classified,
    allowed_entries,
    first_denial,
)
from graph_cli.services.read.access import ACCESS_DENIED_HINT, ReadAccess, denial_error
from graph_cli.services.read.graph_read import ActionCatalog
from graph_cli.services.visible_catalog import hidden_actions, hidden_functions, hidden_views


class ReadCatalogs(ReadAccess):
    def functions(self, actor: ActorContext, entry: GraphEntry) -> tuple[FunctionEntry, ...] | None:
        """The graph's published functions as the actor may use them (K10): ``None`` when the
        graph has none or the actor may not ``read.function`` on it, so a listing shows
        nothing and records nothing (no denial event, like ``graphs``). A type limited actor
        sees only the functions that fit its model view (``services.visible_catalog``); the
        parsed catalog is cached by the hash of the compiled document. The allowed half of
        ``classified_functions``."""
        if self._function_denials(actor, entry):
            return None
        return self._visible_functions(entry, self.read_view(actor, entry))

    def classified_functions(
        self, actor: ActorContext, entry: GraphEntry
    ) -> tuple[Classified[FunctionEntry], ...]:
        """Every function the actor sees, with the rights it lacks (graph start plan S4): a
        function that fits the actor's read view is allowed, or not allowed for want of
        ``read.function``; one that names something the actor cannot see is not in the answer.
        Nothing for a graph the actor may not see (``read.list``). Records nothing."""
        if not self.visible(actor, entry):
            return ()
        denials = self._function_denials(actor, entry)
        view = self.read_view(actor, entry)
        return tuple(Classified(e, denials) for e in self._visible_functions(entry, view) or ())

    def _function_denials(self, actor: ActorContext, entry: GraphEntry) -> tuple[Decision, ...]:
        """The decisions that deny every function of the graph to the actor (a probe)."""
        decision = self._probe(actor, entry, "read.function")
        return () if decision.effect == "allow" else (decision,)

    def _published_slot(
        self, entry: GraphEntry
    ) -> tuple[tuple[FunctionEntry, ...], FunctionsSlot] | None:
        """The parsed catalog of the graph with its slot (``None`` when it has none), no
        authorization."""
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
        return found, slot

    def _published(self, entry: GraphEntry) -> tuple[FunctionEntry, ...] | None:
        """The parsed catalog of the graph (``None`` when it has none), no authorization and
        no read limits."""
        found = self._published_slot(entry)
        return None if found is None else found[0] or None

    def _hidden_functions(
        self,
        entry: GraphEntry,
        found: tuple[tuple[FunctionEntry, ...], FunctionsSlot],
        view: ReadView,
    ) -> frozenset[str]:
        """The functions of the catalog a reader with ``view`` must not see."""
        entries, slot = found

        def compute() -> frozenset[str]:
            full, _ = self._models.load_for_read(entry)
            limited = self._visibility.model(entry, full, view)
            return hidden_functions(entries, slot.compiled, full, limited)

        return self._hidden.of(
            "functions", slot.compiled_hash, entry.pin.artifact_hash, view, compute
        )

    def _visible_functions(
        self, entry: GraphEntry, view: ReadView
    ) -> tuple[FunctionEntry, ...] | None:
        """The functions of the catalog that fit a reader with ``view`` (all of them without a
        limit), ``None`` when there are none."""
        found = self._published_slot(entry)
        if found is None:
            return None
        entries = found[0]
        if view.limited:
            hidden = self._hidden_functions(entry, found, view)
            entries = tuple(e for e in entries if e.name not in hidden)
        return entries or None

    def visible_model(self, actor: ActorContext, entry: GraphEntry) -> CompiledModel:
        """The model ``actor`` sees (what ``open`` puts into ``GraphRead.model``): the pinned one,
        or its filtered view for a reader with read limits. Authorizes and records nothing."""
        full, _ = self._models.load_for_read(entry)
        view = self.read_view(actor, entry)
        return self._visibility.model(entry, full, view) if view.limited else full

    def published_actions(self, entry: GraphEntry) -> ActionCatalog | None:
        """The parsed action catalog of the graph (``None`` when it has none), no
        authorization; cached by the hash of the compiled document. A document that does not
        read is ``actions.slot_invalid``."""
        with self.timings.phase("model"):
            slot = self._catalog.read_actions(entry.id)
            if slot is None:
                return None
            found = self._actions.get(slot.compiled_hash)
            if found is None:
                try:
                    entries = load_action_entries(slot.compiled)
                except JsonShapeError as exc:
                    raise GraphCliError(
                        "actions.slot_invalid",
                        f"the compiled action catalog is unreadable: {exc}",
                        details={"file": f"graphs/{entry.id}/actions/slot.json"},
                    ) from exc
                found = ActionCatalog(
                    entries, slot.compiled_hash, slot.published_against, slot.compiled
                )
                self._actions[slot.compiled_hash] = found
        return found if found.entries else None

    def actions(self, actor: ActorContext, entry: GraphEntry) -> tuple[ActionEntry, ...] | None:
        """The graph's actions as the actor may run them (A6, A10): ``None`` when the graph has
        no catalog or the actor may run none of its actions, so a caller who may run none gets
        the answers of a graph without actions. Records nothing."""
        published = self.published_actions(entry)
        if published is None:
            return None
        return self.visible_actions(actor, entry, published) or None

    def visible_actions(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        catalog: ActionCatalog,
        *,
        limits: bool = True,
    ) -> tuple[ActionEntry, ...]:
        """The actions the actor may run (actions plan A6, write allow lists plan W9): the
        allowed half of the classification. ``limits=False`` leaves the actor's read view out
        (the full model's view of its rights, for the skill render): only this allowed-only
        method can do that, because what the full view classes as not allowed may name terms the
        actor cannot see. Records nothing, like ``functions``."""
        return allowed_entries(self._classify_actions(actor, entry, catalog, limits=limits))

    def classified_actions(
        self, actor: ActorContext, entry: GraphEntry, catalog: ActionCatalog
    ) -> tuple[Classified[ActionEntry], ...]:
        """Every action the actor sees, with the rights it lacks (graph start plan S4); nothing
        for a graph the actor may not see (``read.list``). Always judged by the actor's read
        view, so no entry names a hidden term. See ``_classify_actions``."""
        if not self.visible(actor, entry):
            return ()
        return self._classify_actions(actor, entry, catalog, limits=True)

    def _classify_actions(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        catalog: ActionCatalog,
        *,
        limits: bool,
    ) -> tuple[Classified[ActionEntry], ...]:
        """The classification of the actions of ``catalog`` (graph start plan S4).

        An action is allowed when, for each need, some concrete type allows it, with all the
        attributes and relations the action writes there where the policy has term rules for
        the type (``services.action_rights``); obligations (``--reason``, batch size) never
        count (``has_reason=True``). For a type limited actor also: a type the need is met on
        is one it sees, and no term the action names is missing from its model view (type read
        limits plan V7, ``services.visible_catalog``); with an attribute view, an attribute
        the action writes is met only where the actor sees it (field visibility plan F6);
        ``limits=False`` leaves that out (the full model's view of the actor's rights): its
        not-allowed entries may then name hidden terms and are never shown, only the allowed
        ones are used.

        An action that fails that view is hidden, and so is one with an unmet need no
        decision was made for (every option lies outside what the actor sees): such a need has
        no right to name. Any other action that is not allowed names, per unmet need, its first
        denied decision. Records nothing, like ``functions``."""
        needs = self.action_needs(entry, catalog)
        view = self.read_view(actor, entry) if limits else UNLIMITED
        hidden = self._hidden_actions(entry, catalog, view) if view.limited else frozenset()
        found: list[Classified[ActionEntry]] = []
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            fine = self._fine.of(policy, entry.id)
            ctx = AuthzContext(op_count=1, has_reason=True, dry_run=False)
            for action in catalog.entries:
                if action.name in hidden:
                    continue
                denials: list[Decision] = []
                undecided = False
                for need in needs[action.name]:
                    result = need_decisions(
                        actor, entry.id, need, ctx, policy, fine, view.types, keeps=view.keeps
                    )
                    if result.met:
                        continue
                    denied = first_denial(result.decisions)
                    if denied is None:
                        undecided = True
                        break
                    denials.append(denied)
                if not undecided:
                    found.append(Classified(action, tuple(denials)))
        return tuple(found)

    def _hidden_actions(
        self, entry: GraphEntry, catalog: ActionCatalog, view: ReadView
    ) -> frozenset[str]:
        """The actions of the catalog whose terms a reader with ``view`` lacks."""

        def compute() -> frozenset[str]:
            full, _ = self._models.load_for_read(entry)
            limited = self._visibility.model(entry, full, view)
            return hidden_actions(catalog.entries, catalog.compiled, full, limited)

        return self._hidden.of(
            "actions", catalog.compiled_hash, entry.pin.artifact_hash, view, compute
        )

    def reason_obligations(self, actor: ActorContext, entry: GraphEntry) -> frozenset[str]:
        """The write actions (``write.create``, ...) the actor's policy demands a reason for
        (a ``require_reason`` obligation), so that a listing can say which actions need one.
        Obligations never make an entry not allowed. Records nothing."""
        found = write_obligations(actor, entry.id, self._policy.current(entry.id))
        return frozenset(
            action
            for action, item in found.items()
            if isinstance(item, Mapping) and item.get("require_reason") is True
        )

    def refuse_not_allowed(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        item: Classified[Any],
        request: RequestMeta,
    ) -> GraphCliError:
        """The answer to a call of an entry the actor sees but may not use: the first denying
        decision is audited as a denial (a call is a recorded access, a listing is not) and
        raised as ``access.denied``, naming the missing right."""
        assert item.denials, "an allowed entry is never refused"
        decision = item.denials[0]
        error = denial_error(decision)
        if len(item.missing) > 1:  # say every right it lacks, so one refusal is enough to know
            rights = ", ".join(str(m) for m in item.missing)
            error = GraphCliError(
                error.code,
                f"{rights} are not permitted; {ACCESS_DENIED_HINT}",
                details=error.details,
            )
        return self._record_denial(actor, decision, request, entry, error)

    # ---- views (object views stage 2) -----------------------------------

    def _published_views(
        self, entry: GraphEntry
    ) -> tuple[tuple[ViewEntry, ...], FunctionsSlot] | None:
        """The parsed view catalog of the graph with its slot (``None`` when it has none), no
        authorization; cached by the hash of the compiled document. A document that does not
        read is ``views.slot_invalid``."""
        with self.timings.phase("model"):
            slot = self._catalog.read_views(entry.id)
            if slot is None:
                return None
            found = self._views.get(slot.compiled_hash)
            if found is None:
                try:
                    found = load_view_entries(slot.compiled)
                except JsonShapeError as exc:
                    raise GraphCliError(
                        "views.slot_invalid",
                        f"the compiled view catalog is unreadable: {exc}",
                        details={"file": f"graphs/{entry.id}/views/slot.json"},
                    ) from exc
                self._views[slot.compiled_hash] = found
        return found, slot

    def _hidden_views(
        self,
        entry: GraphEntry,
        found: tuple[tuple[ViewEntry, ...], FunctionsSlot],
        view: ReadView,
    ) -> frozenset[str]:
        """The views of the catalog a reader with ``view`` must not see because of the model
        it sees (the cached half; what depends on the actor's rights is judged per call)."""
        entries, slot = found
        functions = self._published_slot(entry)
        function_entries = functions[0] if functions is not None else ()
        digest = functions[1].compiled_hash if functions is not None else ""

        def compute() -> frozenset[str]:
            full, _ = self._models.load_for_read(entry)
            limited = self._visibility.model(entry, full, view)
            return hidden_views(entries, slot.compiled, limited, function_entries)

        return self._hidden.of(
            "views", f"{slot.compiled_hash}+{digest}", entry.pin.artifact_hash, view, compute
        )

    def _classified_views(
        self, actor: ActorContext, entry: GraphEntry, view: ReadView
    ) -> tuple[Classified[ViewEntry], ...]:
        """The views of the graph the actor sees, with the rights it lacks (graph start plan
        S4). A view is visible exactly when everything it names is: the model the reader sees
        has its classes, relations, attributes and parameters, and the functions a ``rows``
        section runs are visible to the reader (hidden otherwise). Of the views that remain, a
        set view needs ``read.find`` (it lists entities as ``find`` does) and one that runs
        functions needs ``read.function``; the missing right makes it not allowed, not hidden.
        ``read.get`` is the command's own action. A view that is hidden is a missing one
        everywhere."""
        found = self._published_views(entry)
        if found is None:
            return ()
        entries = found[0]
        if view.limited:
            hidden = self._hidden_views(entry, found, view)
            entries = tuple(e for e in entries if e.name not in hidden)
        if any(e.functions for e in entries):
            fits = frozenset(f.name for f in self._visible_functions(entry, view) or ())
            entries = tuple(e for e in entries if fits.issuperset(e.functions))
        find = (
            self._probe(actor, entry, "read.find") if any(not e.entity for e in entries) else None
        )
        run = (
            self._probe(actor, entry, "read.function")
            if any(e.functions for e in entries)
            else None
        )
        classified: list[Classified[ViewEntry]] = []
        for e in entries:
            denials = [
                d
                for d, needed in ((find, not e.entity), (run, bool(e.functions)))
                if d is not None and needed and d.effect != "allow"
            ]
            classified.append(Classified(e, tuple(denials)))
        return tuple(classified)

    def _visible_views(
        self, actor: ActorContext, entry: GraphEntry, view: ReadView
    ) -> tuple[ViewEntry, ...] | None:
        """The views of the graph the actor may use (the allowed half of ``_classified_views``),
        ``None`` when there are none (or the graph has no catalog)."""
        return allowed_entries(self._classified_views(actor, entry, view)) or None

    def classified_views(
        self, actor: ActorContext, entry: GraphEntry
    ) -> tuple[Classified[ViewEntry], ...]:
        """Every view the actor sees, with the rights it lacks; nothing for a graph the actor
        may not see (``read.list``). Records nothing."""
        if not self.visible(actor, entry):
            return ()
        return self._classified_views(actor, entry, self.read_view(actor, entry))

    def views(self, actor: ActorContext, entry: GraphEntry) -> tuple[ViewEntry, ...] | None:
        """The graph's published views as the actor may use them: ``None`` when there are none
        to use, so a listing shows nothing and records nothing."""
        return self._visible_views(actor, entry, self.read_view(actor, entry))

    def _stale_view(
        self,
        entry: GraphEntry,
        found: tuple[tuple[ViewEntry, ...], FunctionsSlot],
        name: str,
    ) -> Problem | None:
        """Why view ``name`` no longer fits the graph's current ontology (``view.stale``
        material), or ``None``. Only a catalog published against another pin is checked, against
        the full model and the published functions (the answer does not depend on the reader);
        cached by (compiled hashes, pin)."""
        from graph_cli.kinetic.view_compile import check_views  # compiles function queries too

        _entries, slot = found
        if slot.published_against == entry.pin:
            return None
        functions = self._published_slot(entry)
        key = (slot.compiled_hash, functions[1].compiled_hash if functions else None, entry.pin)
        cached = self._view_problems.get(key)
        if cached is None:
            full, _ = self._models.load_for_read(entry)
            try:
                problems = check_views(slot.compiled, full, functions[0] if functions else ())
            except (JsonShapeError, GraphCliError) as exc:
                raise GraphCliError(
                    "views.slot_invalid",
                    f"the compiled view catalog is unreadable: {exc}",
                    details={"file": f"graphs/{entry.id}/views/slot.json"},
                ) from exc
            cached = {}
            for problem in problems:
                cached.setdefault(problem.name, problem)
            self._view_problems[key] = cached
        return cached.get(name)

    def action_needs(
        self, entry: GraphEntry, catalog: ActionCatalog
    ) -> Mapping[str, tuple[NeedOptions, ...]]:
        """The needs of every action of ``catalog`` against the graph's pinned model, by action
        name; cached by (compiled hash, pin) like ``action_problem``."""
        key = (catalog.compiled_hash, entry.pin)
        found = self._action_needs.get(key)
        if found is None:
            model, _ = self._models.load_for_read(entry)
            found = {a.name: derive_needs(a, model) for a in catalog.entries}
            self._action_needs[key] = found
        return found

    def action_problem(
        self, entry: GraphEntry, catalog: ActionCatalog, model: CompiledModel, name: str
    ) -> Problem | None:
        """Why action ``name`` no longer fits the graph's current ontology (``action.stale``
        material), or ``None``. Only a catalog published against another pin is checked; the
        result is cached by (compiled hash, pin)."""
        from graph_cli.kinetic.action_compile import check_actions  # not needed by most reads

        if catalog.published_against == entry.pin:
            return None
        key = (catalog.compiled_hash, entry.pin)
        found = self._action_problems.get(key)
        if found is None:
            try:
                problems = check_actions(catalog.compiled, model)
            except (JsonShapeError, GraphCliError) as exc:
                raise GraphCliError(
                    "actions.slot_invalid",
                    f"the compiled action catalog is unreadable: {exc}",
                    details={"file": f"graphs/{entry.id}/actions/slot.json"},
                ) from exc
            found = {}
            for problem in problems:
                found.setdefault(problem.name, problem)
            self._action_problems[key] = found
        return found.get(name)
