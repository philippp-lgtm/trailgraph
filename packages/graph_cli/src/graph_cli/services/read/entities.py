"""Reads of single entities: get and neighbors, and what a reader may do next with one."""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import Revision
from graph_cli.kinetic.affordances import actions_for, functions_for, views_for
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.ontology_layer.affordances import relations, transitions
from graph_cli.query.plan import check_fields, plan_neighbors
from graph_cli.services.read.catalogs import ReadCatalogs
from graph_cli.services.read.graph_read import GraphRead
from graph_cli.services.read.json_shapes import edge_json, entity_json
from graph_cli.services.read.state import query_phase
from graph_cli.services.refs import resolve_ref
from graph_cli.services.view_call import default_view, has_view_for

SUPERSEDES = "core:supersedes"
RETRACTS = "core:retracts"


def refuse_affordances_with_rev() -> GraphCliError:
    """``--affordances`` with ``--rev``: a past revision has no next."""
    return GraphCliError(
        "usage.invalid_argument",
        "--affordances cannot be combined with --rev: a past revision has no next step",
        details={"argument": "--affordances"},
    )


def current_entity(
    view: GraphRead, ref: str
) -> tuple[EntityId, Revision, list[WarningRecord], list[str], list[str]]:
    """The entity ``ref`` names and its current revision, the warnings (alias notices, and
    superseded or retracted) and the ids of the entities that superseded and retracted it."""
    entity, ref_warnings = resolve_ref(ref, view.snapshot, argument="ref")
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


class ReadEntities(ReadCatalogs):
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
        entity, current, warnings, superseded_by, retracted_by = current_entity(view, ref)
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
        start, warnings = resolve_ref(ref, view.snapshot, argument="ref")
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
