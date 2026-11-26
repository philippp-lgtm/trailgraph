"""Object views: the generated card, authored entity views and set views."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.kinetic import params as kinetic_params
from graph_cli.kinetic.card import AffordancesSpec, file_classes_of, generated_spec, render, spec_of
from graph_cli.kinetic.entries import FunctionEntry, find_entry, seen_replacement
from graph_cli.kinetic.params import json_value
from graph_cli.kinetic.view_entries import ViewEntry, find_view
from graph_cli.kinetic.view_format import ENTITY, RowsSection, placeholder_name
from graph_cli.kinetic.view_page import decode_cursor, render_set
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.query.cypher.values import Value
from graph_cli.services.read.entities import ReadEntities, current_entity, unknown_entity
from graph_cli.services.read.finding import ENTITY_SLOT
from graph_cli.services.read.graph_read import GraphRead
from graph_cli.services.read.json_shapes import edges_in, entities_in, value_json
from graph_cli.services.read.queries import ReadQueries
from graph_cli.services.read.state import query_phase
from graph_cli.services.refs import Given, bind_params, entity_params
from graph_cli.services.view_call import (
    CARD,
    LazyRows,
    default_view,
    has_default_for,
    page_key,
    rows_share,
)

MISSING_REF: Final = "Missing argument 'REF'."  # what the required REF of 0.27 said

type Resolved = tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]]


class ReadViews(ReadEntities, ReadQueries):
    @query_phase
    def view(
        self,
        view: GraphRead,
        ref: str | None,
        *,
        name: str | None = None,
        pairs: Given = (),
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
        resolved: Resolved | None = None
        notes: list[WarningRecord] = []
        # opened through a catalog entry without a name: only a default view of the entity's class
        # that the caller's grants name may answer, the card never (``read.get`` is not allowed)
        guarded = view.through is not None and name is None
        try:
            if name is not None and name != CARD:
                chosen = self._named_view(view, name)
            elif name is None and ref is not None:
                chosen, resolved = self._default_view(view, ref, notes)
        except GraphCliError:
            if not guarded:
                raise
            chosen = None  # an entity that is not there, or not seen, is refused like any other
        if guarded and chosen is None:
            raise self._card_refused(view, ref)
        if chosen is None:
            data, warnings = self._card(view, ref, pairs, cursor, page_bytes, resolved)
            return data, [*warnings, *notes]
        return self._authored_view(view, chosen, ref, pairs, cursor, page_bytes, as_of, resolved)

    def _default_view(
        self, view: GraphRead, ref: str, notes: list[WarningRecord]
    ) -> tuple[ViewEntry | None, Resolved | None]:
        """The default view of the class of the entity ``ref`` names, if the graph published one
        the caller may use, and the entity it resolved on the way (``None`` for none); a default
        view that no longer fits the ontology adds ``view.default_stale`` to ``notes`` and the
        card answers."""
        chosen: ViewEntry | None = None
        resolved: Resolved | None = None
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
        return chosen, resolved

    def _card_refused(self, view: GraphRead, ref: str | None) -> GraphCliError:
        """The refusal of the card (and of everything that is not a view the caller's grants name)
        for a graph opened through a catalog entry: the ``read.get`` denial on the graph, audited.
        The same answer whether the entity exists, is seen or has a default view, so the call
        tells nothing about the entity."""
        args: dict[str, JsonValue] = {"graph": view.entry.name}
        if ref is not None:
            args["ref"] = ref
        request = RequestMeta.of(view.command, args, via=view.via)
        try:
            self.authorize(view.actor, "read.get", f"graph:{view.entry.id}", request, view.entry)
        except GraphCliError as exc:
            return exc
        return GraphCliError("internal.error", "the card was refused although read.get allows it")

    def _named_view(self, view: GraphRead, name: str) -> ViewEntry:
        """The view ``name`` asked for: ``view.not_found`` unless the caller sees it, and a
        recorded ``access.denied`` (naming the right it lacks) when it sees the view but may
        not use it."""
        classified = self._classified_views(view.actor, view.entry, view.read_view)
        for item in classified:
            if item.entry.name == name and not item.allowed:
                request = RequestMeta.of(
                    view.command, {"graph": view.entry.name, "name": name}, via=view.via
                )
                raise self.refuse_not_allowed(view.actor, view.entry, item, request)
        # an exact name that is not allowed was refused above: what is found here is allowed, and
        # the suggestions of a name that is none are over everything the caller sees
        return find_view([c.entry for c in classified], name)

    def _card(
        self,
        view: GraphRead,
        ref: str | None,
        pairs: Given,
        cursor: str | None,
        page_bytes: int | None,
        resolved: Resolved | None,
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
        pairs: Given,
        cursor: str | None,
        page_bytes: int | None,
        as_of: bool,
        resolved: Resolved | None,
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
        entity: Resolved | None = None
        if chosen.entity:
            assert ref is not None and chosen.takes is not None
            entity = (
                resolved
                if resolved is not None
                else current_entity(view, ref, lookup_for=f"view:{name}.{ENTITY_SLOT}")
            )
            warnings += entity[2]
            found = str(entity[1].type)
            if not view.model.is_a(found, chosen.takes):
                if view.catalog_only or view.through is not None:
                    raise unknown_entity(ref)  # an id of another class is no entity of the one
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
        bound, found_warnings = bind_params(
            "view",
            name,
            chosen.defn.params,
            pairs,
            model=view.model,
            snapshot=view.snapshot,
            exact_ids=view.catalog_only,
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
                restart_only=view.through is not None,  # --as-of needs read.history
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
        # the row function is found over what the read view shows (an allowed view runs its row
        # functions without them being granted), but a replacement is named only when the caller's
        # catalog has it
        visible = self._visible_functions(view.entry, view.read_view)
        function = find_entry(visible, section.function)
        if function.deprecated and function.name not in deprecated:
            deprecated.add(function.name)
            seen = self.callable_functions(view.actor, view.entry, view.read_view)
            replaced = seen_replacement(function.defn.replaced_by, seen)
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
                "function",
                function.name,
                function.defn.params,
                values,
                view.model,
                suffixes=not view.catalog_only,
            )
            for param in function.defn.params:
                if param.type == "ref":
                    call[param.name], _found = entity_params(
                        view.snapshot,
                        view.model,
                        "function",
                        function.name,
                        param,
                        call,
                        exact_ids=view.catalog_only,
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
