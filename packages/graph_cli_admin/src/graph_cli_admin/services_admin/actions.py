"""``graph-admin graph actions publish|show``: the action catalog of a graph (actions plan
A8, A9).

The maintainer's ``actions/`` folder (one ``<name>.yaml`` per action,
``graph_cli.action/1``) is checked against the graph's pinned ontology by
``graph_cli.kinetic.compile_actions`` and published into the graph's runtime slot
``actions/`` together with the compiled catalog (``graph_cli.action_catalog/1``) and the
pin at publish time. The app reads only the compiled catalog.

Publishing is the same maintenance operation as for functions (``kinetic_slots``): a
``maint.graph_configure`` intent with ``change.actions{from, to, compiled_hash}``, the slot
switch (the commit point), then the anchoring installation event. Refused before any event:
an archived graph (``graph.archived``) and invalid files (``admin.actions_invalid``, every
problem of every action in ``details.problems``). Actions and functions share one namespace
per graph: an action named like a published function is refused (``action.name_taken``),
and the other way round (``function.name_taken``). Publishing the same files against the
same pin again changes nothing; an empty folder publishes an empty catalog (it unpublishes
every action) and, with nothing published, changes nothing. ``--dry-run`` stops after the
checks.

``show`` is read-only for the data root: no lock, no event, authorized
``maint.graph_configure``.
"""

from __future__ import annotations

from pathlib import Path

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin import kinetic_slots
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.kinetic_slots import Checked, Published

__all__ = [
    "Checked",
    "Published",
    "check_files",
    "current_slot",
    "publish",
    "read_dir",
    "refuse",
    "show",
    "slot_of",
]


def read_dir(source: Path) -> tuple[dict[str, bytes], list[Problem]]:
    """The ``<name>.yaml`` files of a folder (see ``kinetic_slots.read_dir``)."""
    return kinetic_slots.read_dir(source, "actions")


def check_files(
    entry: GraphEntry,
    files: dict[str, bytes],
    problems: list[Problem],
    model: CompiledModel,
    *,
    label: Path | str,
    function_names: frozenset[str] = frozenset(),
) -> Checked:
    """``admin.actions_invalid`` when the files cannot become the graph's catalog.
    ``function_names`` are the published functions: an action may not share a name."""
    compiled, found = compile_actions(files, model, function_names=function_names)
    refuse(label, entry, [*problems, *found])
    return Checked(files, compiled)


def current_slot(session: MaintenanceSession) -> KineticSlot | None:
    """The published catalog; a damaged slot is replaced like a missing one."""
    return kinetic_slots.current_slot(session, "actions")


def slot_of(checked: Checked, entry: GraphEntry) -> KineticSlot:
    return kinetic_slots.slot_of(checked, entry, "actions")


def refuse(source: Path | str, entry: GraphEntry, problems: list[Problem]) -> None:
    kinetic_slots.refuse("actions", source, entry, problems)


def publish(
    ctx: AdminContext, *, graph_ref: str, source: Path, reason: str | None, dry_run: bool
) -> Published:
    return kinetic_slots.publish(
        ctx,
        "actions",
        lambda entry, files, problems, model, label, others: check_files(
            entry, files, problems, model, label=label, function_names=others
        ),
        graph_ref=graph_ref,
        source=source,
        reason=reason,
        dry_run=dry_run,
    )


def show(ctx: AdminContext, *, graph_ref: str) -> tuple[GraphEntry, dict[str, JsonValue]]:
    """The published catalog: the slot summary and one line per action."""
    return kinetic_slots.show(ctx, "actions", graph_ref=graph_ref)
