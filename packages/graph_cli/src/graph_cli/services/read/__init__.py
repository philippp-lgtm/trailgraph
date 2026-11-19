"""Read services: graphs, get, history, changes, find, lookup, search, neighbors, query, whoami
(contracts §4.3, B2.10, B6.3, B6.12, B8.8, B8.12, B8.13, B8.15, B9.7, B13).

Every read authorizes its action before touching storage; a denial is
appended to the installation stream and raised. A graph is visible iff
``read.list`` on it is allowed; a denial on a graph the actor cannot see is
raised as ``graph.not_found``, exactly like a graph that does not exist
(B6.15), and still audited. Reads write nothing else.
A read takes one lock-free snapshot and answers everything from it, so the
envelope's ``graph.seq`` names the state that was read. A pinned artifact
that does not match its hash is read with ``ontology.pin_mismatch_read``
(refused when it shows markings: only the pin vouches for them), an archived
graph with ``graph.archived_read``. ``as_of`` reads the snapshot
at the seq it names (B8.8): it needs ``read.history`` besides the read's own
action, a backend with ``snapshot_at_seq``, and every answer carries
``temporal.as_of_read``; the model stays the current pin, and revisions written under
an older major are shown under its names (``services.names``, migration plan M7; current
revisions never are). A successful ``get``,
``history``, ``find``, ``lookup``, ``search``, ``neighbors`` or ``query`` hands the
entities it returns, and the edges ``neighbors`` walked or ``query`` rows hold, to the
activity sink with the command and actor that opened the graph (a transient signal for
``graph-admin view``, no log).
``history`` shows of a tx's actor only whether it is the caller's own
session: who wrote a revision is audit content (B8.15). ``changes`` (the change
feed) is the merged history of every entity the caller sees, in tx order, and
signals no entities (a poller would flood the views' trails).
The command's timings get the phases ``authorize``, ``audit`` (a denial),
``model``, ``snapshot`` and ``query`` (answering from the snapshot); a
Cypher ``query`` has ``parse``, ``resolve`` and ``execute`` instead.
The Cypher engine is imported by ``parse_query`` and ``query`` only: every
other command starts without it.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final

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
from graph_cli.audit import events
from graph_cli.kernel.actions import READ_ACTIONS, RESERVED_ACTIONS, WRITE_ACTIONS
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, EventId, TermRef
from graph_cli.kernel.json_access import JsonShapeError, req_list
from graph_cli.kernel.ports.catalog import FunctionsSlot, GraphEntry, SkillSlot, graph_not_found
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import AsOf, Query
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import EdgeRecord, OntologyPin, RequestMeta, Revision
from graph_cli.kinetic import params as kinetic_params
from graph_cli.kinetic.action_entries import ActionEntry, load_action_entries
from graph_cli.kinetic.action_needs import NeedOptions
from graph_cli.kinetic.action_needs import action_needs as derive_needs
from graph_cli.kinetic.affordances import actions_for, functions_for, views_for
from graph_cli.kinetic.bind import bind_cli
from graph_cli.kinetic.card import AffordancesSpec, file_classes_of, generated_spec, render, spec_of
from graph_cli.kinetic.entries import FunctionEntry, find_entry, load_entries, seen_replacement
from graph_cli.kinetic.params import Problem, json_value
from graph_cli.kinetic.view_entries import ViewEntry, find_view, load_view_entries
from graph_cli.kinetic.view_format import ENTITY, RowsSection, placeholder_name
from graph_cli.kinetic.view_page import decode_cursor, render_set
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.ontology_layer.affordances import relations, transitions
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.errors import did_you_mean
from graph_cli.query.cypher.values import Value
from graph_cli.query.lookup import (
    LookupPlan,
    LookupSpec,
    fact_edges,
    fact_label,
    fit_classes,
    main_label,
    match_revision,
    plan_lookup,
    rank_key,
)
from graph_cli.query.plan import (
    DEFAULT_LIMIT,
    FindSpec,
    check_fields,
    check_paging,
    plan_find,
    plan_neighbors,
    resolve_term,
)
from graph_cli.query.search import SearchSpec, plan_search
from graph_cli.query.search import main_label as search_label
from graph_cli.query.search import match_revision as search_match
from graph_cli.query.search import rank_key as search_rank
from graph_cli.services import migration_state
from graph_cli.services.action_rights import need_decisions
from graph_cli.services.describe import describe_model
from graph_cli.services.names import (
    GraphNames,
    MappedSnapshot,
    hinted_findings,
    known_in,
    term_hints,
    unknown_term_of,
    with_hints,
)
from graph_cli.services.read.as_of import snapshot_as_of
from graph_cli.services.read.change_feed import change_page, revision_change, tx_fields
from graph_cli.services.read.graph_read import GraphRead, Page, graph_json
from graph_cli.services.read.json_shapes import (
    edge_json,
    edges_in,
    entities_in,
    entity_json,
    value_json,
)
from graph_cli.services.read.migration_effects import migration_item
from graph_cli.services.read.state import ReadState, query_phase
from graph_cli.services.refs import entity_params, resolve_ref
from graph_cli.services.term_model import term_model
from graph_cli.services.view_call import (
    CARD,
    LazyRows,
    default_view,
    has_default_for,
    has_view_for,
    page_key,
    rows_share,
)
from graph_cli.services.visibility import SIGNATURE_VIEWS, VisibleSnapshot, remember
from graph_cli.services.visible_catalog import hidden_actions, hidden_functions, hidden_views
from graph_cli.temporal_knowledge.changes import FeedPosition, resolve_since

if TYPE_CHECKING:
    from graph_cli.kinetic.cover import Cover
    from graph_cli.query.cypher.ast import Query as CypherQuery
    from graph_cli.query.cypher.execute import Result as QueryResult
    from graph_cli.query.cypher.resolve import ResolvedQuery

__all__ = [
    "ActionCatalog",
    "GraphRead",
    "Page",
    "QuerySpec",
    "ReadService",
    "change_page",
    "denial_error",
    "edge_json",
    "entity_json",
    "graph_json",
    "refuse_affordances_with_rev",
]

SUPERSEDES = "core:supersedes"
RETRACTS = "core:retracts"
_NO_GRAPH = "graph:00000000-0000-0000-0000-000000000000"  # whoami: a graph no policy names
_VISIBLE = AuthzContext(op_count=0, has_reason=False, dry_run=False)
MISSING_REF: Final = "Missing argument 'REF'."  # what the required REF of 0.27 said
ACCESS_DENIED_HINT = "ask the maintainer of this installation; do not work around it"


def refuse_affordances_with_rev() -> GraphCliError:
    """``--affordances`` with ``--rev``: a past revision has no next."""
    return GraphCliError(
        "usage.invalid_argument",
        "--affordances cannot be combined with --rev: a past revision has no next step",
        details={"argument": "--affordances"},
    )


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    """The parsed published action catalog of a graph and what it was published against."""

    entries: tuple[ActionEntry, ...]
    compiled_hash: str
    published_against: OntologyPin
    compiled: JsonObject


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """How to answer a parsed Cypher query: its parameters, the paging of its rows."""

    parameters: Mapping[str, Value] = field(default_factory=dict)
    include_inactive: bool = False
    explain: bool = False
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    budget: int | None = None  # None: the engine's default (``execute.DEFAULT_BUDGET``)


class ReadService(ReadState):
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
        nothing and records nothing (no denial event, like ``graphs``). A type limited actor
        sees only the functions that fit its model view (``services.visible_catalog``); the
        parsed catalog is cached by the hash of the compiled document."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            decision = authorize(actor, "read.function", f"graph:{entry.id}", _VISIBLE, policy)
        if decision.effect != "allow":
            return None
        return self._visible_functions(entry, self.read_view(actor, entry))

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
        """The graph's actions as the actor may see them (A6, A10): ``None`` when the graph has
        no catalog or the actor may run none of its actions, so a caller who sees none gets the
        answers of a graph without actions and learns nothing about them. Records nothing."""
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
        """The actions the actor may run (actions plan A6, write allow lists plan W9): for each
        need of the action, some concrete type allows it, with all the attributes and relations
        the action writes there where the policy has term rules for the type
        (``services.action_rights``). For a type limited actor also: a type the need is met on
        is one it sees, and no term the action names is missing from its model view (type read
        limits plan V7, ``services.visible_catalog``); with an attribute view, an attribute the
        action writes is met only where the actor sees it (field visibility plan F6);
        ``limits=False`` leaves that out (the full model's view of the actor's rights). Records
        nothing, like ``functions``."""
        needs = self.action_needs(entry, catalog)
        view = self.read_view(actor, entry) if limits else UNLIMITED
        hidden = self._hidden_actions(entry, catalog, view) if view.limited else frozenset()
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            fine = self._fine.of(policy, entry.id)
            ctx = AuthzContext(op_count=1, has_reason=True, dry_run=False)
            return tuple(
                a
                for a in catalog.entries
                if a.name not in hidden
                and all(
                    need_decisions(
                        actor, entry.id, need, ctx, policy, fine, view.types, keeps=view.keeps
                    ).met
                    for need in needs[a.name]
                )
            )

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

    def _may(self, actor: ActorContext, entry: GraphEntry, action: str) -> bool:
        """Whether the policy lets ``actor`` do ``action`` on the graph: a probe, nothing is
        decided on the record."""
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            decision = authorize(actor, action, f"graph:{entry.id}", _VISIBLE, policy)
        return decision.effect == "allow"

    def _visible_views(
        self, actor: ActorContext, entry: GraphEntry, view: ReadView
    ) -> tuple[ViewEntry, ...] | None:
        """The views of the graph the actor may see, ``None`` when there are none (or the graph
        has no catalog). A view is visible exactly when everything it names is: the model the
        reader sees has its classes, relations, attributes and parameters, the functions a
        ``rows`` section runs are visible to the reader and it may ``read.function``, and a set
        view also needs ``read.find`` (it lists entities as ``find`` does); ``read.get`` is the
        command's own action. A view that is not visible is a missing one everywhere."""
        found = self._published_views(entry)
        if found is None:
            return None
        entries = found[0]
        if view.limited:
            hidden = self._hidden_views(entry, found, view)
            entries = tuple(e for e in entries if e.name not in hidden)
        runnable: frozenset[str] = frozenset()
        if any(e.functions for e in entries) and self._may(actor, entry, "read.function"):
            visible = self._visible_functions(entry, view)
            runnable = frozenset(f.name for f in visible or ())
        may_find = (
            self._may(actor, entry, "read.find") if any(not e.entity for e in entries) else False
        )
        entries = tuple(
            e for e in entries if runnable.issuperset(e.functions) and (e.entity or may_find)
        )
        return entries or None

    def views(self, actor: ActorContext, entry: GraphEntry) -> tuple[ViewEntry, ...] | None:
        """The graph's published views as the actor may see them: ``None`` when there are none
        to see, so a listing shows nothing and records nothing."""
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
        names = GraphNames(self._names, entry)
        with self.timings.phase("snapshot"):
            snapshot = read.snapshot()
            interrupted = migration_state.read_warning(entry, read)
            if interrupted is not None:
                warnings += (interrupted,)
            state = None if as_of is None else snapshot_as_of(read, snapshot, as_of)
            if state is not None:
                snapshot = state.snapshot
        # Read limits: a limited caller reads views of the model and the snapshot. They wrap
        # the as-of snapshot after it read the raw tx metadata, and judge mapped types.
        with self.timings.phase("authorize"):
            policy = self._policy.current(entry.id)
            # A model that does not match the pin tells nothing about markings: an artifact
            # with a trace of them refuses the read (``ontology.pin_mismatch``), one without
            # is read as before.
            markings = (
                model.used_markings
                if not _mismatched(warnings)
                else (lambda: self._models.used_markings(entry))
            )
            view = self._visibility.lazy(actor, entry, policy, lambda: model, markings)
        full = model
        if view.limited:
            model = self._visibility.model(entry, model, view)
        # Old revisions under the pin's names (migration plan M7); the current revisions of
        # the head are never mapped, those of an earlier state whose names changed since are,
        # and for a reader limited to some attributes also those of a state whose names the
        # map cannot vouch for (R3: they lose their attributes).
        past = False
        if state is not None:
            known = known_in(model, full) if view.limited else None
            warning, past = state.warning(entry, names, known)
            warnings += (warning,)
            if not past and view.attributes_limited and state.version is not None:
                past = state.earlier and names.unmapped_version(state.version)
        # A class the pinned model has and the reader may not see judges the revisions
        # that were stored under it too, though a retype maps them to another class (F1b).
        mapping = MappedSnapshot(
            snapshot,
            names,
            current=past,
            classes=full.classes,
            shown=view.shows_type if view.types_limited else None,
        )
        snapshot = mapping
        if view.limited:
            snapshot = VisibleSnapshot(snapshot, view, mapping.unmapped, mapping.own_class)
        warnings = (*warnings, *snapshot.warnings)
        self.usage.graph = graph_json(entry, snapshot.seq)
        return GraphRead(entry, model, snapshot, warnings, actor, request.command, view, mapping)

    def rename_hint(
        self, actor: ActorContext, entry: GraphEntry, error: GraphCliError
    ) -> GraphCliError:
        """``error`` with the current name of the old term it names, as far as the actor sees
        it: "renamed to <new> in <version>" from the graph's name map (migration plan M7), in
        the message of a ``term.unknown`` or as the ``hint`` of a ``write.invalid`` finding
        that names an unknown type, attribute or relation. Old names are never accepted;
        anything else, and a graph whose name map cannot be read, gives ``error`` back."""
        kind, term = error.details.get("kind"), error.details.get("term")
        if error.code == "term.unknown":
            if not isinstance(kind, str) or not isinstance(term, str):
                return error
        elif error.code != "write.invalid" or not any(map(unknown_term_of, _findings(error))):
            return error
        try:
            name_map = self._names.of(entry)
            if name_map.empty:
                return error
            full, _ = self._models.load_for_read(entry)
            model = full
            view = self.read_view(actor, entry)
            if view.limited:
                model = self._visibility.model(entry, full, view)
        except GraphCliError:
            return error
        if not isinstance(kind, str) or not isinstance(term, str):
            return hinted_findings(error, name_map, model, full)
        return with_hints(error, term_hints(name_map, kind, term, known_in(model, full)))

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
        compiled function catalog and the action catalog when the graph has them. Like the
        type names, the function names and summaries are schema-level text, so the skill lists
        them for every reader of the description (K10). Actions are different on purpose: a
        skill block is a listing, so it holds only the actions the actor may run (actions plan
        A6, as ``actions``); an actor who may run none gets the documents of a graph without
        actions. The rest of the catalog document is kept as published, so the renderer sees
        one shape. For an actor who sees only some types (type read limits plan V7) the
        documents are those of its model view and hold the functions and actions that fit it,
        so the skill names nothing the actor cannot see."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        model, warnings = self._models.load_for_read(entry)
        view = self.read_view(actor, entry)
        if view.limited:
            model = self._visibility.model(entry, model, view)
        return self._skill_documents(actor, entry, model, view, limits=True), warnings

    def skill_names_model(
        self, actor: ActorContext, entry: GraphEntry, request: RequestMeta
    ) -> dict[str, JsonValue] | None:
        """The documents of ``skill_model`` as they are without the actor's read limits, to
        check the curated text of a template against the full model (``skills.sources``): text
        that names a hidden type is the maintainer's, and a problem about it would name the
        type to the reader. ``None`` when the actor has no limit (``skill_model`` is the full
        model then). The documents are used for the check only, never rendered."""
        self.authorize(actor, "read.describe", f"graph:{entry.id}", request, entry)
        if not self.read_view(actor, entry).limited:
            return None
        model, _ = self._models.load_for_read(entry)
        return self._skill_documents(actor, entry, model, UNLIMITED, limits=False)

    def skill_digest(self, actor: ActorContext, entry: GraphEntry) -> str | None:
        """A digest of what the actor sees (``ReadView.digest``), recorded in the claim of an
        installed graph skill so a policy change makes the copy stale; ``None`` for an actor
        without a read limit, whose claims stay as they were. Without an attribute half it is
        the digest of the visible types, as before markings existed."""
        return self.read_view(actor, entry).digest

    def _skill_documents(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        model: CompiledModel,
        view: ReadView,
        *,
        limits: bool,
    ) -> dict[str, JsonValue]:
        """``describe_model`` of ``model`` with the compiled function catalog (the functions
        that fit a reader with ``view``) and the action catalog narrowed to the
        actions ``actor`` may run (within its read limits when ``limits``)."""
        found = self._published_slot(entry)
        catalog: JsonObject | None = None
        if found is not None and found[1].count:
            document = found[1].compiled
            if not view.limited:
                catalog = document
            else:
                hidden = self._hidden_functions(entry, found, view)
                kept = [
                    e
                    for e in req_list(document, "functions")
                    if isinstance(e, dict) and e.get("name") not in hidden
                ]
                catalog = {**document, "functions": kept} if kept else None
        actions = self._visible_catalog(actor, entry, limits=limits)
        return describe_model(
            entry, model, catalog, actions, self._views_catalog(actor, entry, view)
        )

    def _views_catalog(
        self, actor: ActorContext, entry: GraphEntry, view: ReadView
    ) -> JsonObject | None:
        """The compiled view catalog narrowed to the views ``actor`` may see, as the published
        document; ``None`` when there are none (a graph skill then has no views clause)."""
        visible = self._visible_views(actor, entry, view)
        found = self._published_views(entry)
        if not visible or found is None:
            return None
        names = {v.name for v in visible}
        document = found[1].compiled
        kept = [
            e for e in req_list(document, "views") if isinstance(e, dict) and e.get("name") in names
        ]
        return {**document, "views": kept}

    def _visible_catalog(
        self, actor: ActorContext, entry: GraphEntry, *, limits: bool = True
    ) -> JsonObject | None:
        """The compiled action catalog narrowed to the actions ``actor`` may see, as the
        published document (same schema, entries as compiled); ``None`` when there are none."""
        published = self.published_actions(entry)
        if published is None:
            return None
        names = {a.name for a in self.visible_actions(actor, entry, published, limits=limits)}
        if not names:
            return None
        kept = [
            e
            for e in req_list(published.compiled, "actions")
            if isinstance(e, dict) and e.get("name") in names
        ]
        return {**published.compiled, "actions": kept}

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

    @query_phase
    def get(
        self,
        view: GraphRead,
        ref: str,
        *,
        rev: int | None = None,
        fields: Sequence[str] = (),
        affordances: bool = False,
        full: bool = False,
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """The current revision, or revision ``rev``; the status warnings and
        ``superseded_by``/``retracted_by`` always describe the current state.
        ``affordances`` adds what the current revision of the snapshot can do next (T5);
        a past revision has no next, so it is refused with ``rev``."""
        if affordances and rev is not None:
            raise refuse_affordances_with_rev()
        shown = check_fields(view.model, (), fields)
        entity, current, warnings, superseded_by, retracted_by = _current_entity(view, ref)
        revision = current if rev is None else view.snapshot.get_revision(entity, rev)
        if revision is None:
            raise GraphCliError(
                "entity.revision_not_found",
                f"{ref} has no revision {rev}",
                details={"ref": ref, "id": str(entity), "rev": rev, "current_rev": current.rev},
            )
        data: dict[str, JsonValue] = {
            "entity": entity_json(
                revision, shown, full=full, names_mapped=view.names_mapped(revision)
            ),
            "current_rev": current.rev,
            "superseded_by": list(superseded_by),
            "retracted_by": list(retracted_by),
        }
        if affordances:
            data["affordances"] = self._affordances(view, current)
        self._signal(view, [entity])
        return data, warnings

    @query_phase
    def view(
        self,
        view: GraphRead,
        ref: str | None,
        *,
        name: str | None = None,
        pairs: Sequence[tuple[str, str]] = (),
        cursor: str | None = None,
        page_bytes: int | None = None,
        as_of: bool = False,
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """An object view (T17): the generated card of an entity, an authored entity view, or
        a set view that lists entities. All three are built from the reader's own snapshot and
        model.

        ``name`` picks a published view (``card``: the generated card); without it the default
        view of the entity's class answers, else the card. An entity view takes a REF, a set
        view none (``view.takes_entity``, ``view.takes_no_entity``); ``pairs`` are the
        ``--param name=value`` pairs read by the declared types; ``cursor`` and ``page_bytes``
        are the paging of a set view; ``as_of`` says the snapshot is an earlier state, where
        the ``rows`` sections cannot run (``view.section_unavailable``). The entity ids the
        answer names go to the read signal."""
        if ref is None and name is None:
            raise GraphCliError("usage.missing_argument", MISSING_REF)
        chosen: ViewEntry | None = None
        resolved: tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]] | None = None
        notes: list[WarningRecord] = []
        if name is not None and name != CARD:
            chosen = find_view(self._visible_views(view.actor, view.entry, view.read_view), name)
        elif name is None and ref is not None:
            published = self._published_views(view.entry)
            if published is not None and any(v.defn.default for v in published[0]):
                resolved = _current_entity(view, ref)
                found = str(resolved[1].type)
                # judge what the reader sees only when some default fits the entity's class
                if has_default_for(published[0], view.model, found):
                    visible = self._visible_views(view.actor, view.entry, view.read_view)
                    chosen = default_view(visible or (), view.model, found)
                if chosen is not None:
                    problem = self._stale_view(view.entry, published, chosen.name)
                    if problem is not None:  # the card answers; asking by name refuses
                        notes.append(
                            WarningRecord(
                                "view.default_stale",
                                f"the default view {chosen.name} no longer fits the graph's "
                                f"ontology ({problem.code}); the generated card is shown; "
                                "tell the maintainer of this graph",
                                {"view": chosen.name, "hash": chosen.hash, "cause": problem.code},
                            )
                        )
                        chosen = None
        if chosen is None:
            data, warnings = self._card(view, ref, pairs, cursor, page_bytes, resolved)
            return data, [*warnings, *notes]
        return self._authored_view(view, chosen, ref, pairs, cursor, page_bytes, as_of, resolved)

    def _card(
        self,
        view: GraphRead,
        ref: str | None,
        pairs: Sequence[tuple[str, str]],
        cursor: str | None,
        page_bytes: int | None,
        resolved: tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]] | None,
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """The generated entity card (stage 1): key fields, relations with counts and the first
        neighbours, files with pages, what applies next, in one bounded answer built from the
        reader's own snapshot and model. The status warnings and ``superseded_by`` /
        ``retracted_by`` are those of ``get``; the affordances are ``get --affordances`` summed
        up by names."""
        for option, given in (
            ("--param", bool(pairs)),
            ("--cursor", cursor is not None),
            ("--page-bytes", page_bytes is not None),
        ):
            if given:
                raise GraphCliError(
                    "usage.invalid_argument",
                    f"{option}: the generated card takes no parameters and has no pages",
                    details={"argument": option},
                )
        if ref is None:
            raise GraphCliError(
                "view.takes_entity",
                "the generated card shows one entity; give its reference",
                details={"view": CARD},
            )
        _entity, current, warnings, superseded_by, retracted_by = (
            resolved if resolved is not None else _current_entity(view, ref)
        )
        card = render(
            generated_spec(view.model, str(current.type)),
            current,
            view.snapshot,
            view.model,
            language=view.entry.language,
            affordances=self._affordances(view, current, room=False),
            superseded_by=superseded_by,
            retracted_by=retracted_by,
            file_classes=self._file_classes(view),
        )
        self._signal(view, [current.id, *card.shown()])
        return dict(card.to_json()), warnings

    def _authored_view(
        self,
        view: GraphRead,
        chosen: ViewEntry,
        ref: str | None,
        pairs: Sequence[tuple[str, str]],
        cursor: str | None,
        page_bytes: int | None,
        as_of: bool,
        resolved: tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]] | None,
    ) -> tuple[dict[str, JsonValue], list[WarningRecord]]:
        """A published view: the flavour and the class are checked, the view is checked against
        the current model when it was published against another, the parameters are bound, and
        the pure renderers build the answer; the functions of ``rows`` sections run here."""
        name = chosen.name
        self.usage.view = {"name": name, "hash": chosen.hash}
        if chosen.entity and ref is None:
            raise GraphCliError(
                "view.takes_entity",
                f"view {name} shows one entity; give its reference",
                details={"view": name, "takes": chosen.takes},
            )
        if not chosen.entity and ref is not None:
            raise GraphCliError(
                "view.takes_no_entity",
                f"view {name} takes no entity; leave the reference out",
                details={"view": name},
            )
        if chosen.entity:
            for option, given in (("--cursor", cursor), ("--page-bytes", page_bytes)):
                if given is not None:
                    raise GraphCliError(
                        "usage.invalid_argument",
                        f"{option}: view {name} shows one entity and has no pages",
                        details={"argument": option},
                    )
        warnings: list[WarningRecord] = []
        entity: tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]] | None = None
        if chosen.entity:
            assert ref is not None and chosen.takes is not None
            entity = resolved if resolved is not None else _current_entity(view, ref)
            warnings += entity[2]
            found = str(entity[1].type)
            if not view.model.is_a(found, chosen.takes):
                raise GraphCliError(
                    "view.class_mismatch",
                    f"view {name} takes {chosen.takes}; {ref} is a {found}",
                    details={"view": name, "expected": chosen.takes, "found": found},
                )
        published = self._published_views(view.entry)
        assert published is not None
        problem = self._stale_view(view.entry, published, name)
        if problem is not None:
            # the code only: the check ran on the full model, its message may name a term the
            # reader cannot see
            raise GraphCliError(
                "view.stale",
                f"view {name} no longer fits the graph's ontology ({problem.code}); "
                "tell the maintainer of this graph",
                details={"view": name, "hash": chosen.hash, "cause": problem.code},
            )
        warnings += self._view_notes(view, chosen)
        bound = kinetic_params.bind_cli("view", name, chosen.defn.params, pairs, view.model)
        for param in chosen.defn.params:
            if param.type == "ref":
                bound[param.name], found_warnings = entity_params(
                    view.snapshot, view.model, "view", name, param, bound
                )
                warnings += found_warnings
        spec = spec_of(chosen, view.model, bound)
        sections = [s for s in chosen.defn.sections if isinstance(s, RowsSection)]
        failures: list[WarningRecord] = []
        deprecated: set[str] = set()
        by_id = {s.id: s for s in sections}
        share = rows_share(len(sections))
        entity_id = str(entity[1].id) if entity is not None else None

        def run(section_id: str) -> RowsResult:
            return self._section_rows(
                view,
                chosen,
                by_id[section_id],
                bound,
                entity_id,
                share,
                as_of,
                failures,
                deprecated,
            )

        rows = LazyRows(list(by_id), run)
        if entity is not None:
            assert chosen.defn.max_bytes is None or chosen.defn.max_bytes > 0
            wants = any(isinstance(s, AffordancesSpec) for s in spec.sections)
            card = render(
                spec,
                entity[1],
                view.snapshot,
                view.model,
                language=view.entry.language,
                affordances=(
                    self._affordances(view, entity[1], room=False, showing=chosen)
                    if wants
                    else None
                ),
                superseded_by=entity[3],
                retracted_by=entity[4],
                file_classes=self._file_classes(view),
                rows=rows,
            )
            shown = [entity[1].id, *card.shown()]
        else:
            functions = self._published_slot(view.entry)
            key = page_key(
                view_hash=chosen.hash,
                functions_hash=functions[1].compiled_hash if functions is not None else None,
                pin_hash=view.entry.pin.artifact_hash,
                reader=view.read_view.digest,
                bound=bound,
                names=[p.name for p in chosen.defn.params],
            )
            card = render_set(
                spec,
                view.snapshot,
                view.model,
                language=view.entry.language,
                rows=rows,
                page_bytes=page_bytes,
                cursor=None if cursor is None else decode_cursor(cursor),
                key=key,
            )
            shown = list(card.shown())
        self._signal(view, shown)
        return dict(card.to_json()), [*warnings, *failures]

    def _view_notes(self, view: GraphRead, chosen: ViewEntry) -> list[WarningRecord]:
        """``view.deprecated`` for a deprecated view; the name it was replaced by is shown only
        when the reader sees that view too."""
        if not chosen.deprecated:
            return []
        replaced = chosen.defn.replaced_by
        if replaced is not None:
            visible = self._visible_views(view.actor, view.entry, view.read_view)
            replaced = seen_replacement(replaced, visible)
        return [
            WarningRecord(
                "view.deprecated",
                f"view {chosen.name} is deprecated" + (f"; use {replaced}" if replaced else ""),
                {"view": chosen.name, "replaced_by": replaced},
            )
        ]

    def _section_rows(
        self,
        view: GraphRead,
        chosen: ViewEntry,
        section: RowsSection,
        bound: Mapping[str, Value],
        entity_id: str | None,
        budget: int,
        as_of: bool,
        failures: list[WarningRecord],
        deprecated: set[str],
    ) -> RowsResult:
        """The complete result of the function behind a ``rows`` section (the pure code counts
        and pages every row), or its error: errors of the function (``function.*``,
        ``query.*``) and the unavailable section under ``--as-of`` answer in the section and
        warn ``view.section_failed``; any other error ends the call. The warning comes with
        every page that runs the section. The function runs on the reader's snapshot and model
        within ``budget``, the section's share of what one call may spend."""
        visible = self._visible_functions(view.entry, view.read_view)
        function = find_entry(visible, section.function)
        if function.deprecated and function.name not in deprecated:
            deprecated.add(function.name)
            replaced = seen_replacement(function.defn.replaced_by, visible)
            failures.append(
                WarningRecord(
                    "view.function_deprecated",
                    f"view {chosen.name}: function {function.name} is deprecated"
                    + (f"; use {replaced}" if replaced is not None else ""),
                    {"view": chosen.name, "function": function.name, "replaced_by": replaced},
                )
            )
        try:
            if as_of:
                raise GraphCliError(
                    "view.section_unavailable",
                    "a rows section runs a function over the current state; "
                    "it cannot answer for an earlier one (--as-of)",
                    details={"reason": "as_of"},
                )
            result = self._function_rows(view, function, section, bound, entity_id, budget)
        except GraphCliError as exc:
            if exc.code != "view.section_unavailable" and not exc.code.startswith(
                ("function.", "query.")
            ):
                raise
            failures.append(
                WarningRecord(
                    "view.section_failed",
                    f"section {section.id} of view {chosen.name} could not be answered: {exc.code}",
                    {"view": chosen.name, "section": section.id, "code": exc.code},
                )
            )
            return RowsResult(function.name, function.hash, error=(exc.code, exc.message))
        return result

    def _function_rows(
        self,
        view: GraphRead,
        function: FunctionEntry,
        section: RowsSection,
        bound: Mapping[str, Value],
        entity_id: str | None,
        budget: int,
    ) -> RowsResult:
        """Run a published function for a ``rows`` section: the section's ``args`` (literals,
        ``$param`` and ``$entity``) bound by the function's declared types, ``ref`` values
        resolved on the reader's snapshot, and every row of the result as JSON cells."""
        values: dict[str, JsonValue] = {}
        for key, raw in section.args.items():
            holder = placeholder_name(raw)
            if holder is None:
                values[key] = raw
            elif holder == ENTITY:
                values[key] = entity_id
            else:
                values[key] = json_value(bound[holder])
        with self.timings.phase("query"):
            call = kinetic_params.bind_json(
                "function", function.name, function.defn.params, values, view.model
            )
            for param in function.defn.params:
                if param.type == "ref":
                    call[param.name], _found = entity_params(
                        view.snapshot, view.model, "function", function.name, param, call
                    )
        resolved = self._resolve_function(view, function, call)
        result = self._execute(view, resolved, False, budget)
        rows: list[Mapping[str, JsonValue]] = [
            {column: value_json(value) for column, value in zip(result.columns, row, strict=True)}
            for row in result.rows
        ]
        entities = dict.fromkeys([*entities_in(result.rows), *result.touched])
        edges = dict.fromkeys([*edges_in(result.rows), *result.walked])
        self._signal(view, list(entities), list(edges), function.name)
        return RowsResult(function.name, function.hash, tuple(result.columns), tuple(rows))

    def _file_classes(self, view: GraphRead) -> frozenset[str]:
        """The stored-file classes the reader sees. The family comes from the stored model: the
        view of a limited reader lacks a hidden ``files:File`` that a visible class extends (as
        in ``FileService``)."""
        stored = self._models.load_for_read(view.entry)[0] if view.read_view.limited else view.model
        return file_classes_of(stored) & frozenset(view.model.classes)

    def _affordances(
        self,
        view: GraphRead,
        current: Revision,
        *,
        room: bool = True,
        showing: ViewEntry | None = None,
    ) -> dict[str, JsonValue]:
        """The ``affordances`` object of ``get``: the four groups, composed here because
        only the service knows which actions and functions the actor may see, and the entity
        views that take the entity (key ``views``, only when there are any, without the default
        view of its class and ``showing``, the view being shown). Without ``room``
        the relation group is left out: it reads every in-edge source of the entity, and the
        card (which shows counts of its own) never uses it."""
        found: dict[str, JsonValue] = {"record_status": current.record_status}
        found.update(transitions(view.model, current, view.snapshot).to_json())
        if room:
            found.update(relations(view.model, current, view.snapshot).to_json())
        actions = self.actions(view.actor, view.entry) or ()
        found.update(actions_for(current, actions, view.snapshot, view.model).to_json())
        functions = self.functions(view.actor, view.entry) or ()
        found.update(functions_for(current, functions, view.model).to_json())
        published = self._published_views(view.entry)
        # judge what the reader sees only when some view takes the entity's class; a graph
        # without such views is spared the catalog's compile for a limited reader
        fits = published is not None and has_view_for(published[0], view.model, str(current.type))
        views = self._visible_views(view.actor, view.entry, view.read_view) if fits else None
        if views:  # absent for a graph without (visible) views, so its answers do not change
            shown = [default_view(views, view.model, str(current.type)), showing]
            others = [v for v in shown if v is not None]
            found.update(views_for(current, views, view.model, others).to_json())
        return found

    @query_phase
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
        # a limited reader sees what a migration did only as far as its view reaches
        full = self._models.load_for_read(view.entry)[0] if view.read_view.limited else None
        for meta in shown:
            tx = view.snapshot.tx(meta.tx_seq)
            change = revision_change(view.snapshot, entity, meta.rev)
            item: dict[str, JsonValue] = {
                "rev": meta.rev,
                "tx_seq": meta.tx_seq,
                "recorded_at": format_timestamp(meta.recorded_at),
                "record_status": meta.record_status,
                **tx_fields(tx, session),
                "changed": change.changed,
            }
            if tx is not None and tx.request.action is not None:  # additive: set by a ``do``
                item["action"] = tx.request.action.to_json()
            if tx is not None and tx.request.source is not None:  # additive: a source run
                item["source"] = tx.request.source.to_json()
            migration = migration_item(view, entity, meta.rev, meta.tx_seq, full)
            if migration is not None:  # additive: the revision a migration wrote (M7)
                item["migration"] = migration
            items.append(item)
        data: dict[str, JsonValue] = {
            "id": str(entity),
            "current_rev": metas[-1].rev,
            "items": items,
        }
        self._signal(view, [entity])
        return data, Page(len(metas), len(shown), limit, offset), list(warnings)

    @query_phase
    def changes(
        self, view: GraphRead, since: FeedPosition | datetime, *, limit: int
    ) -> dict[str, JsonValue]:
        """The change feed: what changed in the whole graph after ``since``, oldest first, as
        far as the caller sees it, one page of at most ``limit`` entity items (change feed
        plan CF3-CF13). The txs are read from the opened snapshot up to its head, so a write
        after it waits for the next page. No read signal (CF13)."""
        check_paging(limit, 0)
        mapping = view.mapping
        if mapping is None:
            raise GraphCliError("internal.error", "the change feed needs a view opened to read")
        return change_page(
            view.snapshot,
            mapping,  # below the read limits; its txs are those of ``mapping.raw``, memoised
            resolve_since(view.snapshot, since),
            limit,
            session=view.actor.session.id,
            migration=mapping.migration_record,
            digest=view.read_view.digest,
        )

    @query_phase
    def find(
        self,
        view: GraphRead,
        spec: FindSpec,
        *,
        rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        in_rel_refs: Sequence[tuple[str, Sequence[str]]] = (),
        fields: Sequence[str] = (),
        full: bool = False,
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
        items: list[JsonValue] = [
            entity_json(r, shown, full=full, names_mapped=view.names_mapped(r)) for r in page.items
        ]
        data: dict[str, JsonValue] = {"items": items}
        self._signal(view, [r.id for r in page.items])
        return data, Page(page.total, len(page.items), page.limit, page.offset), warnings

    def _use_fit(
        self, view: GraphRead, use: str | None, source: str | None
    ) -> tuple[tuple[str, ...] | None, bool]:
        """The classes ``--for`` and ``--for-source`` allow, intersected, and whether either
        was given. ``--for`` is a relation (the candidate is its target) or
        ``<function or action>.<param>``, a ``ref`` parameter of a published function the
        actor may read (``read.function``) or of an action the actor may run; ``--for-source`` is
        a relation (the candidate is its source). None: any class."""
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
        """The class of a ``ref`` parameter of a function or an action and its subclasses
        (``--for f.p``). Names are unique across both kinds, so a visible action is looked up
        first."""
        name, _, param_name = use.partition(".")
        actions = self.actions(view.actor, view.entry)
        action = next((a for a in actions or () if a.name == name), None)
        if action is not None:
            kind, params = "action", action.defn.params
        else:
            functions = self.functions(view.actor, view.entry)
            if actions is not None and all(f.name != name for f in functions or ()):
                names = sorted([*(f.name for f in functions or ()), *(a.name for a in actions)])
                raise GraphCliError(
                    "function.not_found",
                    f"no function or action {name!r}",
                    details={
                        "name": name,
                        "did_you_mean": difflib.get_close_matches(name, names, n=5),
                    },
                )
            kind, params = "function", find_entry(functions, name).defn.params
        found = next((p for p in params if p.name == param_name), None)
        if found is None:
            known = [p.name for p in params]
            raise GraphCliError(
                f"{kind}.parameter_unknown",
                f"{name} has no parameter {param_name!r}"
                + (f"; it takes {', '.join(known)}" if known else " and takes no parameters"),
                details={
                    kind: name,
                    "parameter": param_name,
                    "expected": known,
                    "did_you_mean": did_you_mean(param_name, known),
                },
            )
        if found.type != "ref" or found.of_class is None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--for: parameter {param_name!r} of {name} is {found.type_text}, not a ref",
                details={"argument": "--for", kind: name, "parameter": param_name},
            )
        return view.model.subtypes(found.of_class)

    @query_phase
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

        ``items[].facts`` tells the candidates of a text apart (``query.lookup.fact_edges``):
        a list when the text has more than one candidate, else null. One ``get_many`` reads
        the targets of every shown item.
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
        pending: list[tuple[dict[str, JsonValue], LookupPlan, tuple[EdgeRecord, ...]]] = []
        targets: set[EntityId] = set()
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
            ambiguous = len(found) > 1
            for _, revision, match in shown:
                item: dict[str, JsonValue] = {
                    "text": text,
                    "id": str(revision.id),
                    "alias": revision.alias,
                    "type": revision.type,
                    "record_status": revision.record_status,
                    "label": main_label(plan, revision, match),
                    "match": match.to_json(),
                    "facts": None,
                }
                if ambiguous:
                    edges = fact_edges(view.model, revision)
                    pending.append((item, plan, edges))
                    targets.update(edge.target for edge in edges)
                items.append(item)
            exact = sum(1 for _, _, match in found if match.kind == "exact")
            counts.append({"text": text, "total": len(found), "exact": exact, "excluded": excluded})
            shown_ids += [revision.id for _, revision, _ in shown]
            total += len(found)
            more += plan.offset + len(shown) < len(found)
        read = view.snapshot.get_many(sorted(targets)) if targets else {}
        for item, plan, edges in pending:
            facts: list[JsonValue] = []
            for edge in edges:
                target = read.get(edge.target)
                facts.append(
                    {
                        "rel": str(edge.rel),
                        "id": str(edge.target),
                        "alias": None if target is None else target.alias,
                        "label": None if target is None else fact_label(view.model, plan, target),
                    }
                )
            item["facts"] = facts
        fact_ids = [edge.edge_id for _, _, edges in pending for edge in edges]
        self._signal(view, [*shown_ids, *(t for t in sorted(targets) if t in read)], fact_ids)
        page = Page(total, len(items), spec.limit, spec.offset, more=bool(more))
        data: dict[str, JsonValue] = {"items": items, "texts": counts}
        data["fit"] = (
            {"for": for_use, "for_source": for_source, "types": None if fit is None else list(fit)}
            if asked
            else None
        )
        return data, page

    @query_phase
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

    @query_phase
    def neighbors(
        self,
        view: GraphRead,
        ref: str,
        *,
        rels: Sequence[str] = (),
        direction: str = "both",
        depth: int = 1,
        fields: Sequence[str] = (),
        full: bool = False,
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
                "edge": edge_json(edge, full=full),
                "entity": entity_json(
                    revision, shown, full=full, names_mapped=view.names_mapped(revision)
                ),
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
    ) -> tuple[dict[str, JsonValue], Page | None, tuple[Cover, ...]]:
        """A parsed query resolved against the graph's model and, unless ``explain``,
        run over its snapshot; the rows are paged like ``find``. The third item is the
        functions the caller may run that cover the query, best first (usage shapes U2,
        U5); the shape and the first three are noted for the access log."""
        from graph_cli.query.cypher.resolve import resolve

        with self.timings.phase("resolve"):
            resolved = resolve(query, view.model, spec.parameters)
        found = self._note_shape(view, resolved)
        if spec.explain:
            return {"plan": resolved.explain()}, None, found
        data, page = self._rows(
            view, resolved, spec.include_inactive, spec.budget, spec.limit, spec.offset
        )
        return data, page, found

    def _note_shape(self, view: GraphRead, resolved: ResolvedQuery) -> tuple[Cover, ...]:
        """Note the query's shape for the access log and find the functions that cover it,
        over the functions the caller may see only: a hidden function is never hinted or
        logged. Before the query runs, so a failing execution still logs its shape."""
        from graph_cli.kinetic.cover import SignatureCache, covers, query_signature
        from graph_cli.query.cypher.shape import shape

        with self.timings.phase("shape"):
            found: tuple[Cover, ...] = ()
            visible = self.functions(view.actor, view.entry)
            if visible:
                signatures = self._signatures.get(view.read_view)
                if signatures is None:
                    signatures = SignatureCache()
                    remember(self._signatures, view.read_view, signatures, SIGNATURE_VIEWS)
                pairs = signatures.pairs(visible, view.model)
                found = covers(query_signature(resolved), pairs, view.model)
            described = shape(resolved)
            self.usage.query = {
                "shape_hash": described.hash,
                "shape": described.text,
                "covered_by": [{"name": c.function, "list_fit": c.list_fit} for c in found[:3]],
            }
        return found

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
        result = self._execute(view, resolved, include_inactive, budget)
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

    def _execute(
        self,
        view: GraphRead,
        resolved: ResolvedQuery,
        include_inactive: bool,
        budget: int | None,
    ) -> QueryResult:
        """A resolved query run over the reader's snapshot and model, every row of it."""
        from graph_cli.query.cypher.execute import DEFAULT_BUDGET, execute

        with self.timings.phase("execute"):
            return execute(
                resolved,
                view.model,
                view.snapshot,
                include_inactive=include_inactive,
                budget=DEFAULT_BUDGET if budget is None else budget,
            )

    def _resolve_function(
        self, view: GraphRead, function: FunctionEntry, bound: Mapping[str, Value]
    ) -> ResolvedQuery:
        """The stored query text of a function parsed and resolved against the reader's model
        with the bound values: text that no longer does is ``function.stale``."""
        from graph_cli.query.cypher.resolve import resolve

        try:
            parsed = self.parse_query(function.query)
            with self.timings.phase("resolve"):
                return resolve(parsed, view.model, bound)
        except GraphCliError as exc:
            raise GraphCliError(
                "function.stale",
                f"function {function.name} no longer fits the graph's ontology ({exc.code}); "
                "tell the maintainer of this graph",
                details={"function": function.name, "hash": function.hash, "cause": exc.code},
            ) from exc

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
        visible = self._visible_functions(view.entry, view.read_view)
        function = find_entry(visible, name)
        self.usage.function = {"name": name, "hash": function.hash}
        warnings: list[WarningRecord] = []
        if function.deprecated:
            replaced = seen_replacement(function.defn.replaced_by, visible)
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
                    bound[param.name], found = entity_params(
                        view.snapshot, view.model, "function", name, param, bound
                    )
                    warnings += found
        resolved = self._resolve_function(view, function, bound)
        data, page = self._rows(view, resolved, False, None, limit, offset, name)
        data = {"function": {"name": name, "hash": function.hash}, **data}
        return data, page, tuple(warnings)

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


def _findings(error: GraphCliError) -> list[JsonValue]:
    found = error.details.get("findings")
    return found if isinstance(found, list) else []


def _mismatched(warnings: Sequence[WarningRecord]) -> bool:
    """Whether ``load_for_read`` read an artifact that does not match the pin."""
    return any(w.code == "ontology.pin_mismatch_read" for w in warnings)


def _current_entity(
    view: GraphRead, ref: str
) -> tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]]:
    """The entity ``ref`` names and its current revision, the warnings (alias notices, and
    superseded or retracted) and the ids of the entities that superseded and retracted it."""
    entity, ref_warnings = resolve_ref(ref, view.snapshot)
    current = view.snapshot.get(entity)
    if current is None:
        raise GraphCliError(
            "entity.not_found", f"no entity {ref}", details={"ref": ref, "id": str(entity)}
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
    return entity, current, warnings, superseded_by, retracted_by


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
