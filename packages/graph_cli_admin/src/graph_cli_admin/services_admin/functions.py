"""``graph-admin graph functions publish|show``: the function catalog of a graph (K6, K7).

The maintainer's ``functions/`` folder (one ``<name>.yaml`` per function,
``graph_cli.function/1``) is checked against the graph's pinned ontology by
``graph_cli.kinetic.compile_catalog`` and published into the graph's runtime
slot together with the compiled catalog (``graph_cli.functions/1``) and the
pin at publish time. The app reads only the compiled catalog.

Publishing is one maintenance operation in the W.e shape, authorized as
``maint.graph_configure`` like a skill: intent in the graph stream with
``change.functions{from, to}`` (tree hashes of the sources), the slot switch
(the commit point), then an installation event anchoring the intent. Refused
before any event: an archived graph (``graph.archived``) and invalid files
(``admin.functions_invalid``, every problem of every function in
``details.problems``). Publishing the same files against the same pin again
changes nothing; an empty folder publishes an empty catalog (it unpublishes
every function) and, with nothing published, changes nothing. ``--dry-run``
stops after the checks.

``show`` is read-only for the data root, like ``graph show``: no lock, no
event, authorized ``maint.graph_configure``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_bool, req_list, req_str
from graph_cli.kernel.ports.catalog import FunctionsSlot, GraphEntry, functions_hash
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.format import FILE_SUFFIX, Problem
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import pinned_model
from graph_cli_admin.services_admin.release import check_state

DAMAGED = frozenset({"functions.slot_invalid", "format.unsupported_version"})


@dataclass(frozen=True, slots=True)
class Published:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


@dataclass(frozen=True, slots=True)
class Checked:
    """A function folder judged against a model: the sources the slot keeps and the compiled
    catalog."""

    files: dict[str, bytes]
    compiled: JsonObject


def read_dir(source: Path) -> tuple[dict[str, bytes], list[Problem]]:
    """The ``<name>.yaml`` files of a folder; anything else in it is a problem, never followed."""
    files: dict[str, bytes] = {}
    problems: list[Problem] = []
    try:
        names = sorted(os.listdir(source))
        for name in names:
            path = source / name
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode) or not name.endswith(FILE_SUFFIX) or not stat.S_ISREG(mode):
                problems.append(
                    Problem(
                        name,
                        "function.invalid_file",
                        f"{name} is not a regular {FILE_SUFFIX} file; the folder holds "
                        f"one <name>{FILE_SUFFIX} per function and nothing else",
                        name,
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


def check_files(
    entry: GraphEntry,
    files: dict[str, bytes],
    problems: list[Problem],
    model: CompiledModel,
    *,
    label: Path | str,
) -> Checked:
    """``admin.functions_invalid`` when the files cannot become the graph's catalog."""
    compiled, found = compile_catalog(files, model)
    refuse(label, entry, [*problems, *found])
    return Checked(files, compiled)


def current_slot(session: MaintenanceSession) -> FunctionsSlot | None:
    """The published catalog; a damaged slot is replaced like a missing one."""
    try:
        return session.read_functions()
    except GraphCliError as exc:
        if exc.code not in DAMAGED:
            raise
        return None


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    current: FunctionsSlot | None,
    slot: FunctionsSlot,
    *,
    reason: str | None,
) -> None:
    """Switch the slot from ``current`` to ``slot`` in the W.e shape."""
    change = events.functions_change(current.hash if current is not None else None, slot.hash)
    receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
    session.replace_functions(slot, receipt)
    ctx.anchor(entry, decisions, meta, receipt, reason=reason)


def slot_of(checked: Checked, entry: GraphEntry) -> FunctionsSlot:
    return FunctionsSlot(
        functions_hash(checked.files),
        hash_value(dict(checked.compiled)),
        entry.pin,
        checked.compiled,
        checked.files,
    )


def publish(
    ctx: AdminContext, *, graph_ref: str, source: Path, reason: str | None, dry_run: bool
) -> Published:
    entry = ctx.resolve(graph_ref, "graph functions publish")
    check_state(entry)
    files, problems = read_dir(source)
    checked_against = entry.pin
    checked = check_files(entry, files, problems, pinned_model(ctx.catalog, entry), label=source)
    digest = functions_hash(files)
    meta = RequestMeta.of(
        "graph functions publish", {"graph": str(entry.id), "hash": digest, "dry_run": dry_run}
    )
    decisions = ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=meta,
        graph=entry.id,
        reason=reason,
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        check_state(entry)
        if entry.pin != checked_against:
            # A release came in between: the functions must resolve against the new pin.
            checked = check_files(
                entry, files, problems, pinned_model(ctx.catalog, entry), label=source
            )
        current = current_slot(session)
        slot = slot_of(checked, entry)
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "from": current.summary() if current is not None else None,
            "to": slot.summary(),
            "functions": _names(slot),
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


def show(ctx: AdminContext, *, graph_ref: str) -> tuple[GraphEntry, dict[str, JsonValue]]:
    """The published catalog: the slot summary and one line per function."""
    entry = ctx.resolve(graph_ref, "graph functions show")
    ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=RequestMeta.of("graph functions show", {"graph": graph_ref}),
        graph=entry.id,
    )
    slot = ctx.catalog.read_functions(entry.id)
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "slot": slot.summary() if slot is not None else None,
        "functions": _listed(slot) if slot is not None else [],
    }
    return entry, data


def _names(slot: FunctionsSlot) -> list[JsonValue]:
    return [name.removesuffix(FILE_SUFFIX) for name in sorted(slot.files)]


def _listed(slot: FunctionsSlot) -> list[JsonValue]:
    """One line per function of the compiled catalog."""
    listed: list[JsonValue] = []
    for item in req_list(slot.compiled, "functions"):
        entry = as_object(item, "functions")
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


def refuse(source: Path | str, entry: GraphEntry, problems: list[Problem]) -> None:
    if problems:
        raise GraphCliError(
            "admin.functions_invalid",
            f"{source} is not a valid function folder for graph {entry.name}: {_summary(problems)}",
            details={"graph": entry.name, "problems": [p.to_json() for p in problems]},
        )


def _summary(problems: list[Problem]) -> str:
    shown = "; ".join(f"{p.function}: {p.message}" for p in problems[:5])
    more = len(problems) - 5
    return f"{shown}; and {more} more" if more > 0 else shown
