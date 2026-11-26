"""Opening a graph for one actor and command, and the hint a refused write may carry."""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.records import RequestMeta
from graph_cli.services import migration_state
from graph_cli.services.catalog_access import EntryPath
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
from graph_cli.services.read.catalogs import ReadCatalogs
from graph_cli.services.read.graph_read import GraphRead, graph_json
from graph_cli.services.visibility import VisibleSnapshot


def _findings(error: GraphCliError) -> list[JsonValue]:
    found = error.details.get("findings")
    return found if isinstance(found, list) else []


def _mismatched(warnings: Sequence[WarningRecord]) -> bool:
    """Whether ``load_for_read`` read an artifact that does not match the pin."""
    return any(w.code == "ontology.pin_mismatch_read" for w in warnings)


class ReadOpen(ReadCatalogs):
    def open(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        action: str,
        request: RequestMeta,
        *,
        as_of: AsOf | None = None,
        through: EntryPath | None = None,
    ) -> GraphRead:
        """The graph's model and one snapshot: the head, or the state ``as_of`` names;
        an earlier state is history, so it also needs ``read.history``.

        ``through`` names the entries the command works through (``run F``, ``view --name V``):
        when the caller's rights on the graph do not allow ``action``, an entry its grants name
        may (``authorize_through``) and the opened graph says which (``GraphRead.through``)."""
        opened = None
        if through is None:
            self.authorize(actor, action, f"graph:{entry.id}", request, entry)
        else:
            opened = self.authorize_through(actor, entry, action, through, request)
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
        return GraphRead(
            entry,
            model,
            snapshot,
            warnings,
            actor,
            request.command,
            view,
            mapping,
            request.via,
            opened,
        )

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
