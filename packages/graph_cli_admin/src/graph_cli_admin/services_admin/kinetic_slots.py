"""Publishing and showing the function, action and view catalogs of a graph: one implementation
parametrized by kind (K6, K7; actions plan A8; views plan V9).

``functions.py``, ``actions.py`` and ``authored_views.py`` hold what differs (the checks); the
folder reading, the slot switch in the W.e shape, the refusal summary and the publish and show
flows are here. Functions and actions share one namespace per graph (actions plan A1): the
names of the other kind's published catalog go into the checks of a publish, which refuse a
clash. Views have a namespace of their own, but depend on the functions their ``rows``
sections run and a function catalog depends on the views that use it: a publish of either
re-checks under the lock when the other's slot changed in between (``watched``).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.json_access import as_object, req_bool, req_list, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SlotKind, sources_hash
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin import catalog_follow, granted_entries
from graph_cli_admin.services_admin.catalog_follow import ProblemHints, names_of, rename_hints
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.release import DAMAGED, check_state, current_catalog

SINGULAR: Final[dict[SlotKind, str]] = {
    "functions": "function",
    "actions": "action",
    "views": "view",
}
INVALID_FILE: Final[dict[SlotKind, str]] = {
    "functions": "function.invalid_file",
    "actions": "action.invalid_file",
    "views": "view.invalid_file",
}


@dataclass(frozen=True, slots=True)
class Published:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class Checked:
    """A folder judged against a model: the sources the slot keeps and the compiled
    catalog."""

    files: dict[str, bytes]
    compiled: JsonObject


# What a kind contributes to a publish: the files, the folder problems, the model, the label
# of the folder for messages, the names of the other kind's published catalog and the rename
# hints for the problems.
Check = Callable[
    [
        GraphEntry,
        dict[str, bytes],
        list[Problem],
        CompiledModel,
        Path | str,
        frozenset[str],
        "ProblemHints | None",
    ],
    Checked,
]
# What a kind warns about the slot it would publish, judged against the model it was checked
# against (actions: marked attributes in preconditions).
Warn = Callable[[GraphEntry, CompiledModel, KineticSlot], tuple[WarningRecord, ...]]


def read_dir(source: Path, kind: SlotKind) -> tuple[dict[str, bytes], list[Problem]]:
    """The ``<name>.yaml`` files of a folder; anything else in it is a problem, never followed
    (hidden files such as ``.DS_Store`` are ignored)."""
    one = SINGULAR[kind]
    files: dict[str, bytes] = {}
    problems: list[Problem] = []
    try:
        names = sorted(n for n in os.listdir(source) if not n.startswith("."))  # .DS_Store
        for name in names:
            path = source / name
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode) or not name.endswith(FILE_SUFFIX) or not stat.S_ISREG(mode):
                problems.append(
                    Problem(
                        name,
                        INVALID_FILE[kind],
                        f"{name} is not a regular {FILE_SUFFIX} file; the folder holds "
                        f"one <name>{FILE_SUFFIX} per {one} and nothing else",
                        name,
                        kind=one,
                    )
                )
                continue
            files[name] = path.read_bytes()
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"cannot read {source}: {exc.strerror or exc}",
            details={"argument": "dir"},
        ) from exc
    return files, problems


def current_slot(session: MaintenanceSession, kind: SlotKind) -> KineticSlot | None:
    """The published catalog; a damaged slot is replaced like a missing one."""
    return current_catalog(session, kind)


def published_names(ctx: AdminContext, entry: GraphEntry, kind: SlotKind) -> frozenset[str]:
    """The names of the published ``kind`` catalog, read without the lock; a damaged slot has
    none."""
    try:
        slot = read_published(ctx, entry, kind)
    except GraphCliError as exc:
        if exc.code not in DAMAGED:
            raise
        return frozenset()
    return names_of(slot)


def read_published(ctx: AdminContext, entry: GraphEntry, kind: SlotKind) -> KineticSlot | None:
    """The published catalog of ``kind`` as the catalog port gives it, without the lock."""
    if kind == "functions":
        return ctx.catalog.read_functions(entry.id)
    if kind == "actions":
        return ctx.catalog.read_actions(entry.id)
    return ctx.catalog.read_views(entry.id)


def other_kind(kind: SlotKind) -> SlotKind | None:
    """The kind whose names a publish of ``kind`` must not take: functions and actions share
    one namespace; views have a namespace of their own (``None``)."""
    if kind == "views":
        return None
    return "actions" if kind == "functions" else "functions"


def watched(
    read: Callable[[SlotKind], KineticSlot | None], kind: SlotKind
) -> tuple[str | None, ...]:
    """What a publish of ``kind`` depends on besides names: a view catalog is checked against
    the function catalog (its compiled hash), a function catalog against the published views
    (their sources hash). A change in between sends the publish back to its checks."""
    if kind == "views":
        slot = read("functions")
        return (None if slot is None else slot.compiled_hash,)
    if kind == "functions":
        slot = read("views")
        return (None if slot is None else slot.hash,)
    return ()


def _readable(read: Callable[[], KineticSlot | None], kind: SlotKind) -> KineticSlot | None:
    try:
        return read()
    except GraphCliError as exc:
        if exc.code not in DAMAGED[kind]:
            raise
        return None


write = catalog_follow.write  # one definition: a release that republishes writes the same way


def slot_of(checked: Checked, entry: GraphEntry, kind: SlotKind) -> KineticSlot:
    return KineticSlot(
        sources_hash(checked.files),
        hash_value(dict(checked.compiled)),
        entry.pin,
        checked.compiled,
        checked.files,
        kind,
    )


def refuse(
    kind: SlotKind,
    source: Path | str,
    entry: GraphEntry,
    problems: list[Problem],
    hints: ProblemHints | None = None,
) -> None:
    """``admin.<kind>_invalid`` when there are problems (with ``hints`` added)."""
    if problems and hints is not None:
        problems = hints(problems)
    if problems:
        raise GraphCliError(
            f"admin.{kind}_invalid",
            f"{source} is not a valid {SINGULAR[kind]} folder for graph {entry.name}: "
            f"{_summary(problems)}",
            details={"graph": entry.name, "problems": [p.to_json() for p in problems]},
        )


def _summary(problems: list[Problem]) -> str:
    shown = "; ".join(f"{p.name}: {p.message}" for p in problems[:5])
    more = len(problems) - 5
    return f"{shown}; and {more} more" if more > 0 else shown


def publish(
    ctx: AdminContext,
    kind: SlotKind,
    check: Check,
    *,
    graph_ref: str,
    source: Path,
    reason: str | None,
    dry_run: bool,
    warn: Warn | None = None,
) -> Published:
    """The publish flow of ``graph <kind> publish`` (see ``functions`` and ``actions``).
    ``warn`` judges the slot it would publish, also on a dry run and when nothing changes
    (the warnings describe a state, like those of a release). A publish that removes or changes
    an entry a catalog grant names also warns (``granted_entries``)."""
    command = f"graph {kind} publish"
    entry = ctx.resolve(graph_ref, command)
    check_state(entry)
    files, problems = read_dir(source, kind)
    checked_against = entry.pin
    names_kind = other_kind(kind)
    others = published_names(ctx, entry, names_kind) if names_kind else frozenset()
    stamp = watched(lambda k: _readable(lambda: read_published(ctx, entry, k), k), kind)
    model = pinned_model(ctx.catalog, entry)
    hints = rename_hints(ctx, entry, model)
    checked = check(entry, files, problems, model, source, others, hints)
    digest = sources_hash(files)
    meta = RequestMeta.of(command, {"graph": str(entry.id), "hash": digest, "dry_run": dry_run})
    decisions = ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=meta,
        graph=entry.id,
        reason=reason,
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        check_state(entry)
        locked_others = names_of(current_slot(session, names_kind)) if names_kind else frozenset()
        locked_stamp = watched(lambda k: current_slot(session, k), kind)
        if entry.pin != checked_against or locked_others != others or locked_stamp != stamp:
            # A release or a publish of the other kind came in between: check again.
            model = pinned_model(ctx.catalog, entry)
            hints = rename_hints(ctx, entry, model)
            checked = check(entry, files, problems, model, source, locked_others, hints)
        current = current_slot(session, kind)
        slot = slot_of(checked, entry, kind)
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "from": current.summary() if current is not None else None,
            "to": slot.summary(),
            kind: _names(slot),
            "published": False,
            "dry_run": dry_run,
        }
        seq = session.head().seq
        warnings = warn(entry, model, slot) if warn is not None else ()
        warnings += granted_entries.warnings(
            ctx.policy.current(entry.id), entry, kind, current, slot
        )
        unchanged = current.summary() == slot.summary() if current is not None else not slot.files
        if unchanged or dry_run:
            return Published(entry, data, seq, warnings)
        write(ctx, session, entry, decisions, meta, current, slot, reason=reason)
        data["published"] = True
        return Published(entry, data, seq, warnings)


def show(
    ctx: AdminContext, kind: SlotKind, *, graph_ref: str
) -> tuple[GraphEntry, dict[str, JsonValue]]:
    """The published catalog: the slot summary and one line per entry."""
    command = f"graph {kind} show"
    entry = ctx.resolve(graph_ref, command)
    ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=RequestMeta.of(command, {"graph": graph_ref}),
        graph=entry.id,
    )
    slot = read_published(ctx, entry, kind)
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "slot": slot.summary() if slot is not None else None,
        kind: listed(slot) if slot is not None else [],
    }
    return entry, data


def _names(slot: KineticSlot) -> list[JsonValue]:
    return [name.removesuffix(FILE_SUFFIX) for name in sorted(slot.files)]


def listed(slot: KineticSlot) -> list[JsonValue]:
    """One line per entry of the compiled catalog."""
    key = slot.spec.list_key
    rows: list[JsonValue] = []
    for item in req_list(slot.compiled, key):
        entry = as_object(item, key)
        module = entry.get("module")
        row: dict[str, JsonValue] = {
            "name": req_str(entry, "name"),
            "module": module if isinstance(module, str) else None,
            "summary": req_str(entry, "summary"),
            "status": req_str(entry, "status"),
        }
        if slot.kind == "views":  # a view takes a class or none; it is not featured
            takes = entry.get("takes")
            row["takes"] = takes if isinstance(takes, str) else None
            row["default"] = req_bool(entry, "default")
        else:
            row["featured"] = req_bool(entry, "featured")
        row["hash"] = req_str(entry, "hash")
        rows.append(row)
    return rows
