"""``graph-admin graph functions publish|show``: the function catalog of a graph (K6, K7).

The maintainer's ``functions/`` folder (one ``<name>.yaml`` per function,
``graph_cli.function/1``) is checked against the graph's pinned ontology by
``graph_cli.kinetic.compile_catalog`` and published into the graph's runtime
slot together with the compiled catalog (``graph_cli.functions/1``) and the
pin at publish time. The app reads only the compiled catalog.

Publishing is one maintenance operation in the W.e shape, authorized as
``maint.graph_configure`` like a skill: intent in the graph stream with
``change.functions{from, to, compiled_hash}`` (tree hashes of the sources, and the
hash of the compiled catalog), the slot switch (the commit point), then an installation
event anchoring the intent. Refused
before any event: an archived graph (``graph.archived``) and invalid files
(``admin.functions_invalid``, every problem of every function in
``details.problems``). Publishing the same files against the same pin again
changes nothing; an empty folder publishes an empty catalog (it unpublishes
every function) and, with nothing published, changes nothing. ``--dry-run``
stops after the checks.

The flow is shared with ``actions`` and ``views`` (``kinetic_slots``). Functions and actions
share one namespace per graph: a function named like a published action is refused
(``function.name_taken`` among the problems). A published view that runs a function depends on
the catalog: publishing functions that would break a view (a function or a bound parameter
gone, a parameter of another type) is refused with ``admin.views_broken``; fix the view first
or push both together.

Publishing warns, never refuses, with ``admin.function_single_ref`` (one record per publish) about
active functions whose single-value ``ref`` is only compared with ``=``: a ``many: true`` ref and
``IN`` would answer several entities per run. Functions that a published view runs are exempt (a
view hands them one entity). The warning also covers ``graph push`` (``single_ref_warnings``).

``show`` is read-only for the data root, like ``graph show``: no lock, no
event, authorized ``maint.graph_configure``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kinetic.compile import compile_catalog, single_refs
from graph_cli.kinetic.entries import load_entries
from graph_cli.kinetic.format import FILE_SUFFIX, Problem
from graph_cli.kinetic.view_entries import load_view_entries
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin import authored_views, kinetic_slots
from graph_cli_admin.services_admin.catalog_follow import ProblemHints
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.kinetic_slots import Checked, Published
from graph_cli_admin.services_admin.release import FUNCTIONS_DAMAGED

DAMAGED = FUNCTIONS_DAMAGED
MAX_LISTED: Final = 20

__all__ = [
    "DAMAGED",
    "Checked",
    "Published",
    "check_files",
    "current_slot",
    "publish",
    "read_dir",
    "refuse",
    "show",
    "single_ref_warnings",
    "slot_of",
    "write",
]

write = kinetic_slots.write


def read_dir(source: Path) -> tuple[dict[str, bytes], list[Problem]]:
    """The ``<name>.yaml`` files of a folder (see ``kinetic_slots.read_dir``)."""
    return kinetic_slots.read_dir(source, "functions")


def check_files(
    entry: GraphEntry,
    files: dict[str, bytes],
    problems: list[Problem],
    model: CompiledModel,
    *,
    label: Path | str,
    taken: frozenset[str] = frozenset(),
    hints: ProblemHints | None = None,
    views: KineticSlot | None = None,
) -> Checked:
    """``admin.functions_invalid`` when the files cannot become the graph's catalog.
    ``taken`` are the names of the published actions: a function may not share one.
    ``views`` is the published view catalog: ``admin.views_broken`` when it would not hold
    with these functions."""
    clashes = [
        Problem(
            name,
            "function.name_taken",
            f"{stem!r} is the name of a published action; functions and actions share one "
            "namespace per graph",
            name,
            details={"name": stem},
        )
        for name in sorted(files)
        if (stem := name.removesuffix(FILE_SUFFIX)) in taken
    ]
    compiled, found = compile_catalog(files, model)
    refuse(label, entry, [*problems, *clashes, *found], hints)
    authored_views.broken_by_functions(views, model, load_entries(compiled), hints)
    return Checked(files, compiled)


def current_slot(session: MaintenanceSession) -> KineticSlot | None:
    """The published catalog; a damaged slot is replaced like a missing one."""
    return kinetic_slots.current_slot(session, "functions")


def slot_of(checked: Checked, entry: GraphEntry) -> KineticSlot:
    return kinetic_slots.slot_of(checked, entry, "functions")


def refuse(
    source: Path | str,
    entry: GraphEntry,
    problems: list[Problem],
    hints: ProblemHints | None = None,
) -> None:
    kinetic_slots.refuse("functions", source, entry, problems, hints)


def published_views(ctx: AdminContext, entry: GraphEntry) -> KineticSlot | None:
    """The published view catalog, read without the lock; a damaged one counts as none."""
    try:
        return ctx.catalog.read_views(entry.id)
    except GraphCliError as exc:
        if exc.code not in authored_views.DAMAGED:
            raise
        return None


def single_ref_warnings(
    entry: GraphEntry, model: CompiledModel, slot: KineticSlot, views: KineticSlot | None
) -> tuple[WarningRecord, ...]:
    """``admin.function_single_ref`` for the function catalog ``slot`` judged against ``model``:
    one record naming up to ``MAX_LISTED`` of the functions (``function``, ``parameter``,
    ``class``) and how many more there are. ``views`` is the view catalog the graph will have;
    the functions its views run are left out."""
    exempt = (
        frozenset(f for view in load_view_entries(views.compiled) for f in view.functions)
        if views is not None
        else frozenset()
    )
    found = single_refs(slot.compiled, model, exempt)
    if not found:
        return ()
    listed: list[JsonValue] = [
        {"function": function, "parameter": parameter, "class": cls}
        for function, parameter, cls in found[:MAX_LISTED]
    ]
    first = ", ".join(f"{function}({parameter})" for function, parameter, _ in found[:3])
    more = " and more" if len(found) > 3 else ""
    return (
        WarningRecord(
            "admin.function_single_ref",
            f"{len(found)} active function(s) of graph {entry.name} take one reference per run "
            f"and only compare it with =: {first}{more}. A parameter with many: true and "
            "IN answers several entities in one call; `graph-admin usage report` shows which "
            "functions agents run often",
            {"graph": entry.name, "functions": listed, "more": max(len(found) - MAX_LISTED, 0)},
        ),
    )


def publish(
    ctx: AdminContext, *, graph_ref: str, source: Path, reason: str | None, dry_run: bool
) -> Published:
    return kinetic_slots.publish(
        ctx,
        "functions",
        lambda entry, files, problems, model, label, others, hints: check_files(
            entry,
            files,
            problems,
            model,
            label=label,
            taken=others,
            hints=hints,
            views=published_views(ctx, entry),
        ),
        graph_ref=graph_ref,
        source=source,
        reason=reason,
        dry_run=dry_run,
        warn=lambda entry, model, slot: single_ref_warnings(
            entry, model, slot, published_views(ctx, entry)
        ),
    )


def show(ctx: AdminContext, *, graph_ref: str) -> tuple[GraphEntry, dict[str, JsonValue]]:
    """The published catalog: the slot summary and one line per function."""
    return kinetic_slots.show(ctx, "functions", graph_ref=graph_ref)
