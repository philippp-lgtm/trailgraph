"""Publishing and showing the function and action catalogs of a graph: one implementation
parametrized by kind (K6, K7; actions plan A8).

``functions.py`` and ``actions.py`` hold what differs (the checks); the folder reading, the
slot switch in the W.e shape, the refusal summary and the publish and show flows are here.
The two kinds share one namespace per graph (actions plan A1): the names of the other
kind's published catalog go into the checks of a publish, which refuse a clash.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_bool, req_list, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SlotKind, sources_hash
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.release import check_state

DAMAGED: Final = frozenset(
    {"functions.slot_invalid", "actions.slot_invalid", "format.unsupported_version"}
)
SINGULAR: Final[dict[SlotKind, str]] = {"functions": "function", "actions": "action"}
INVALID_FILE: Final[dict[SlotKind, str]] = {
    "functions": "function.invalid_file",
    "actions": "action.invalid_file",
}


@dataclass(frozen=True, slots=True)
class Published:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


@dataclass(frozen=True, slots=True)
class Checked:
    """A folder judged against a model: the sources the slot keeps and the compiled
    catalog."""

    files: dict[str, bytes]
    compiled: JsonObject


# What a kind contributes to a publish: the files, the folder problems, the model, the label
# of the folder for messages and the names of the other kind's published catalog.
Check = Callable[
    [GraphEntry, dict[str, bytes], list[Problem], CompiledModel, Path | str, frozenset[str]],
    Checked,
]


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
    try:
        return session.read_functions() if kind == "functions" else session.read_actions()
    except GraphCliError as exc:
        if exc.code not in DAMAGED:
            raise
        return None


def names_of(slot: KineticSlot | None) -> frozenset[str]:
    """The names in a published catalog (deprecated ones too: they still hold the name)."""
    if slot is None:
        return frozenset()
    key = slot.spec.list_key
    return frozenset(req_str(as_object(e, key), "name") for e in req_list(slot.compiled, key))


def published_names(ctx: AdminContext, entry: GraphEntry, kind: SlotKind) -> frozenset[str]:
    """The names of the published ``kind`` catalog, read without the lock; a damaged slot has
    none."""
    try:
        slot = (
            ctx.catalog.read_functions(entry.id)
            if kind == "functions"
            else ctx.catalog.read_actions(entry.id)
        )
    except GraphCliError as exc:
        if exc.code not in DAMAGED:
            raise
        return frozenset()
    return names_of(slot)


def other_kind(kind: SlotKind) -> SlotKind:
    return "actions" if kind == "functions" else "functions"


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    current: KineticSlot | None,
    slot: KineticSlot,
    *,
    reason: str | None,
) -> None:
    """Switch the slot from ``current`` to ``slot`` in the W.e shape."""
    build = events.functions_change if slot.kind == "functions" else events.actions_change
    change = build(current.hash if current is not None else None, slot.hash, slot.compiled_hash)
    receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
    if slot.kind == "functions":
        session.replace_functions(slot, receipt)
    else:
        session.replace_actions(slot, receipt)
    ctx.anchor(entry, decisions, meta, receipt, reason=reason)


def slot_of(checked: Checked, entry: GraphEntry, kind: SlotKind) -> KineticSlot:
    return KineticSlot(
        sources_hash(checked.files),
        hash_value(dict(checked.compiled)),
        entry.pin,
        checked.compiled,
        checked.files,
        kind,
    )


def refuse(kind: SlotKind, source: Path | str, entry: GraphEntry, problems: list[Problem]) -> None:
    """``admin.<kind>_invalid`` when there are problems."""
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
) -> Published:
    """The publish flow of ``graph <kind> publish`` (see ``functions`` and ``actions``)."""
    command = f"graph {kind} publish"
    entry = ctx.resolve(graph_ref, command)
    check_state(entry)
    files, problems = read_dir(source, kind)
    checked_against = entry.pin
    others = published_names(ctx, entry, other_kind(kind))
    checked = check(entry, files, problems, pinned_model(ctx.catalog, entry), source, others)
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
        locked_others = names_of(current_slot(session, other_kind(kind)))
        if entry.pin != checked_against or locked_others != others:
            # A release or a publish of the other kind came in between: check again.
            checked = check(
                entry, files, problems, pinned_model(ctx.catalog, entry), source, locked_others
            )
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
        unchanged = current.summary() == slot.summary() if current is not None else not slot.files
        if unchanged or dry_run:
            return Published(entry, data, seq)
        write(ctx, session, entry, decisions, meta, current, slot, reason=reason)
        data["published"] = True
        return Published(entry, data, seq)


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
    slot = (
        ctx.catalog.read_functions(entry.id)
        if kind == "functions"
        else ctx.catalog.read_actions(entry.id)
    )
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "slot": slot.summary() if slot is not None else None,
        kind: _listed(slot) if slot is not None else [],
    }
    return entry, data


def _names(slot: KineticSlot) -> list[JsonValue]:
    return [name.removesuffix(FILE_SUFFIX) for name in sorted(slot.files)]


def _listed(slot: KineticSlot) -> list[JsonValue]:
    """One line per entry of the compiled catalog."""
    key = slot.spec.list_key
    listed: list[JsonValue] = []
    for item in req_list(slot.compiled, key):
        entry = as_object(item, key)
        module = entry.get("module")
        listed.append(
            {
                "name": req_str(entry, "name"),
                "module": module if isinstance(module, str) else None,
                "summary": req_str(entry, "summary"),
                "status": req_str(entry, "status"),
                "featured": req_bool(entry, "featured"),
                "hash": req_str(entry, "hash"),
            }
        )
    return listed
