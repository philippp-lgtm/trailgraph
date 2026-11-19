"""``graph-admin graph views publish|show``: the view catalog of a graph (views plan V9).

The maintainer's ``views/`` folder (one ``<name>.yaml`` per view, ``graph_cli.view/1``) is
checked against the graph's pinned ontology and the published function catalog by
``graph_cli.kinetic.compile_views`` and published into the graph's runtime slot ``views/``
together with the compiled catalog (``graph_cli.view_catalog/1``) and the pin at publish time.
The app reads only the compiled catalog.

The name ``views.py`` is taken by the live view of the app; this module is the admin side of the
authored views of ``graph view``.

Publishing is the same maintenance operation as for functions and actions
(``kinetic_slots``): a ``maint.graph_configure`` intent with ``change.views{from, to,
compiled_hash}``, the slot switch (the commit point), then the anchoring installation event.
Refused before any event: an archived graph (``graph.archived``) and invalid files
(``admin.views_invalid``, every problem of every view in ``details.problems``). Views have a
namespace of their own (``card`` is reserved), so a view may share the name of a function. A
``rows`` section runs a published function: the views are checked against the functions the
graph has, and publishing functions is refused when a published view would break
(``admin.views_broken``, see ``broken_by_functions``). Publishing the same files against the
same pin again changes nothing; an empty folder publishes an empty catalog (it unpublishes every
view) and, with nothing published, changes nothing. ``--dry-run`` stops after the checks. A view
that runs a deprecated function warns with ``view.function_deprecated``, also on a dry run.

``show`` is read-only for the data root: no lock, no event, authorized ``maint.graph_configure``.
"""

from __future__ import annotations

from pathlib import Path

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.params import Problem
from graph_cli.kinetic.view_compile import check_views, compile_views, deprecated_function_uses
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin import kinetic_slots
from graph_cli_admin.services_admin.catalog_follow import ProblemHints, function_entries
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.kinetic_slots import Checked, Published
from graph_cli_admin.services_admin.release import FUNCTIONS_DAMAGED, VIEWS_DAMAGED

DAMAGED = VIEWS_DAMAGED

__all__ = [
    "DAMAGED",
    "Checked",
    "Published",
    "broken_by_functions",
    "check_files",
    "current_slot",
    "function_entries",
    "publish",
    "read_dir",
    "refuse",
    "show",
    "slot_of",
    "write",
]

MAX_LISTED = 20

write = kinetic_slots.write


def read_dir(source: Path) -> tuple[dict[str, bytes], list[Problem]]:
    """The ``<name>.yaml`` files of a folder (see ``kinetic_slots.read_dir``)."""
    return kinetic_slots.read_dir(source, "views")


def published_functions(ctx: AdminContext, entry: GraphEntry) -> tuple[FunctionEntry, ...]:
    """The function catalog the graph has now, read without the lock."""
    try:
        return function_entries(ctx.catalog.read_functions(entry.id))
    except GraphCliError as exc:
        if exc.code not in FUNCTIONS_DAMAGED:
            raise
        return ()


def check_files(
    entry: GraphEntry,
    files: dict[str, bytes],
    problems: list[Problem],
    model: CompiledModel,
    *,
    label: Path | str,
    functions: tuple[FunctionEntry, ...] = (),
    hints: ProblemHints | None = None,
) -> Checked:
    """``admin.views_invalid`` when the files cannot become the graph's catalog; ``functions``
    are the published functions the ``rows`` sections run."""
    compiled, found = compile_views(files, model, functions)
    refuse(label, entry, [*problems, *found], hints)
    return Checked(files, compiled)


def current_slot(session: MaintenanceSession) -> KineticSlot | None:
    """The published catalog; a damaged slot is replaced like a missing one."""
    return kinetic_slots.current_slot(session, "views")


def slot_of(checked: Checked, entry: GraphEntry) -> KineticSlot:
    return kinetic_slots.slot_of(checked, entry, "views")


def refuse(
    source: Path | str,
    entry: GraphEntry,
    problems: list[Problem],
    hints: ProblemHints | None = None,
) -> None:
    kinetic_slots.refuse("views", source, entry, problems, hints)


def broken_by_functions(
    views: KineticSlot | None,
    model: CompiledModel,
    functions: tuple[FunctionEntry, ...],
    hints: ProblemHints | None = None,
) -> None:
    """``admin.views_broken`` when a published view would not hold against ``functions`` (the
    catalog a function publish is about to write): a view that loses its function, a bound
    parameter or the type of one. Nothing for a graph without views."""
    if views is None:
        return
    problems = check_views(views.compiled, model, functions)
    if problems and hints is not None:
        problems = hints(problems)
    if not problems:
        return
    names = sorted({p.name for p in problems})
    shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
    plural = "s" if len(names) != 1 else ""
    raise GraphCliError(
        "admin.views_broken",
        f"{len(names)} published view{plural} would not hold with these functions: {shown};"
        " fix or unpublish them first, or push them together with the functions",
        details={
            "count": len(problems),
            "views": list[JsonValue](names[:MAX_LISTED]),
            "problems": [p.to_json() for p in problems[:MAX_LISTED]],
        },
    )


def deprecated_warnings(
    slot: KineticSlot, functions: tuple[FunctionEntry, ...]
) -> tuple[WarningRecord, ...]:
    """``view.function_deprecated`` for each ``rows`` section of ``slot`` that runs a
    deprecated function."""
    return tuple(
        WarningRecord(
            p.code,
            f"view {p.name}: {p.message}",
            {"view": p.name, "path": p.path, **p.details},
        )
        for p in deprecated_function_uses(slot.compiled, functions)
    )


def publish(
    ctx: AdminContext, *, graph_ref: str, source: Path, reason: str | None, dry_run: bool
) -> Published:
    return kinetic_slots.publish(
        ctx,
        "views",
        lambda entry, files, problems, model, label, _names, hints: check_files(
            entry,
            files,
            problems,
            model,
            label=label,
            functions=published_functions(ctx, entry),
            hints=hints,
        ),
        graph_ref=graph_ref,
        source=source,
        reason=reason,
        dry_run=dry_run,
        warn=lambda entry, _model, slot: deprecated_warnings(slot, published_functions(ctx, entry)),
    )


def show(ctx: AdminContext, *, graph_ref: str) -> tuple[GraphEntry, dict[str, JsonValue]]:
    """The published catalog: the slot summary and one line per view."""
    return kinetic_slots.show(ctx, "views", graph_ref=graph_ref)
