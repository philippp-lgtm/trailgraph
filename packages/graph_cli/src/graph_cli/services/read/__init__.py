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

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

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
from graph_cli.kernel.actions import READ_ACTIONS, RESERVED_ACTIONS, WRITE_ACTIONS
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.json_access import req_list
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.kinetic import params as kinetic_params
from graph_cli.kinetic.card import AffordancesSpec, file_classes_of, generated_spec, render, spec_of
from graph_cli.kinetic.entries import FunctionEntry, find_entry, seen_replacement
from graph_cli.kinetic.params import json_value
from graph_cli.kinetic.view_entries import ViewEntry, find_view
from graph_cli.kinetic.view_format import ENTITY, RowsSection, placeholder_name
from graph_cli.kinetic.view_page import decode_cursor, render_set
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.values import Value
from graph_cli.query.plan import check_paging
from graph_cli.services import migration_state
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
from graph_cli.services.read.access import denial_error
from graph_cli.services.read.as_of import snapshot_as_of
from graph_cli.services.read.catalogs import ActionCatalog
from graph_cli.services.read.change_feed import change_page, revision_change, tx_fields
from graph_cli.services.read.entities import (
    ReadEntities,
    current_entity,
    refuse_affordances_with_rev,
)
from graph_cli.services.read.finding import ReadFinding
from graph_cli.services.read.graph_read import GraphRead, Page, graph_json
from graph_cli.services.read.json_shapes import (
    edge_json,
    edges_in,
    entities_in,
    entity_json,
    value_json,
)
from graph_cli.services.read.migration_effects import migration_item
from graph_cli.services.read.queries import QuerySpec, ReadQueries
from graph_cli.services.read.state import query_phase
from graph_cli.services.refs import entity_params, resolve_ref
from graph_cli.services.term_model import term_model
from graph_cli.services.view_call import (
    CARD,
    LazyRows,
    default_view,
    has_default_for,
    page_key,
    rows_share,
)
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.temporal_knowledge.changes import FeedPosition, resolve_since

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

_NO_GRAPH = "graph:00000000-0000-0000-0000-000000000000"  # whoami: a graph no policy names
MISSING_REF: Final = "Missing argument 'REF'."  # what the required REF of 0.27 said


class ReadService(ReadQueries, ReadEntities, ReadFinding):
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
                resolved = current_entity(view, ref)
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
            resolved if resolved is not None else current_entity(view, ref)
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
            entity = resolved if resolved is not None else current_entity(view, ref)
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
