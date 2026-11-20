"""graph-admin graph push: a maintainer workspace pushed back to its graph.

The workspace layout, the base file and pull are in ``sources``.

``push`` is one maintenance operation under the graph lock, like a
fast-forward push: the data root must still hold the base, or already what
is pushed (a repeated push), else ``admin.source_conflict``. The highest
version directory is the ontology: when it differs from the base it is
released and pinned (``release.plan|write``); a skill tree that differs
from the base is published (``skill.write``), and so are a ``functions``, an ``actions``
and a ``views`` folder that differ from the base (after the release and the skill, in this
order; a missing folder is an empty one, so deleting every file unpublishes the
catalog). Whenever the ontology or the skill changes, the skill the graph
will have is checked against the ontology it will have; whenever the
ontology or the functions change, so are the functions: the new files
against the model the graph will have, or, when the push does not touch
them, the published catalog (``admin.functions_broken`` in the release); the
actions likewise (``admin.actions_broken``); so are the views, against the model and the
functions the graph will have after the push (the pushed ones, else the published ones
after their own follow): the pushed files (``admin.views_invalid``) or the published
catalog (``admin.views_broken``). A release with renames or a migration file
publishes a catalog it does not replace again when its sources still compile against the new
model (``release.plan``, ``data.ontology.republished``). Functions and actions share one
namespace: each kind is checked against the names of the other kind the graph
will have; views have a namespace of their own. Every check runs before the first write; the
new base is written into the directory afterwards (``dry_run``: answered, not written).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SkillSlot
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli_admin.services_admin import (
    actions,
    authored_views,
    catalog_follow,
    functions,
    marking_warnings,
    release,
    skill,
)
from graph_cli_admin.services_admin.catalog_follow import Republish, function_entries, names_of
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.sources import (
    Base,
    base_document,
    read_base,
    usage_error,
    write_base,
)
from graph_cli_admin.services_admin.sources_push_read import EMPTY, Workspace, read_workspace
from graph_cli_admin.services_admin.sources_push_state import Catalogs, Current, Locked, Publishing


@dataclass(frozen=True, slots=True)
class Pushed:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


@dataclass(frozen=True, slots=True)
class _Authorized:
    """What ``_authorize`` decided: the request and the decisions the writes record."""

    meta: RequestMeta
    configure: tuple[Decision, ...]
    decisions: release.Decisions | None  # only when the ontology is a new release


@dataclass(frozen=True, slots=True)
class _Final:
    """What the graph holds once the push is done (or would hold: a dry run): the published
    slots, else what the release compiles again, else what is published now."""

    skill: SkillSlot | None
    functions: KineticSlot | None
    actions: KineticSlot | None
    views: KineticSlot | None


def push(
    ctx: AdminContext,
    *,
    graph_ref: str,
    source: Path,
    reason: str | None,
    dry_run: bool,
    inventory: Mapping[str, JsonValue],
) -> Pushed:
    entry = ctx.resolve(graph_ref, "graph push")
    release.check_state(entry)
    base = read_base(source)
    if base.graph_id != str(entry.id):
        raise usage_error(
            f"{source} holds graph {base.graph_name} ({base.graph_id}), not {entry.name};"
            " pull this graph into another directory"
        )
    if not dry_run and not os.access(source, os.W_OK):
        raise usage_error(f"{source} is not writable: push records the new base there")
    ws = read_workspace(ctx, entry, source, base)
    auth = _authorize(ctx, entry, ws, reason=reason, dry_run=dry_run)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        release.check_state(entry)
        target_pin = ws.compiled.pin if ws.ontology_changed else ws.base.pin
        current = _read_current(session)
        _check_unmoved(ws, entry, current, target_pin)
        publishes = _publishing(ws, current)
        planned = _plan_release(ctx, session, ws, current, publishes, dry_run=dry_run)
        locked = Locked.of(ctx, entry, ws, current, publishes, planned, target_pin)
        catalogs = _check_catalogs(locked)
        checked = _check_skill(locked, catalogs, inventory=inventory)
        slot = _skill_slot(locked, checked)
        seq = session.head().seq
        after = (
            locked.after
            if dry_run
            else _write_all(ctx, session, locked, catalogs, slot, auth, reason=reason)
        )
        final = _final(locked, catalogs, slot)
        data = _answer(locked, catalogs, slot, final, after, dry_run=dry_run)
    if not dry_run:
        write_base(source, _source_document(after, final))
    return Pushed(after, data, seq, _warnings(locked, catalogs, checked, after))


def _write_all(
    ctx: AdminContext,
    session: MaintenanceSession,
    locked: Locked,
    catalogs: Catalogs,
    slot: SkillSlot | None,
    auth: _Authorized,
    *,
    reason: str | None,
) -> GraphEntry:
    """Write the push, in the order release, skill, functions, actions, views (an interrupted
    push is repeatable from any point); returns the entry after the release."""
    after = locked.after
    current = locked.current
    if locked.releasing:
        assert locked.planned is not None and auth.decisions is not None
        after = release.write(
            ctx, session, locked.planned, auth.decisions, auth.meta, reason=reason
        )
    if slot is not None:
        skill.write(
            ctx, session, after, auth.configure, auth.meta, current.skill, slot, reason=reason
        )
    if catalogs.functions is not None:
        functions.write(
            ctx,
            session,
            after,
            auth.configure,
            auth.meta,
            current.functions,
            catalogs.functions,
            reason=reason,
        )
    if catalogs.actions is not None:
        actions.write(
            ctx,
            session,
            after,
            auth.configure,
            auth.meta,
            current.actions,
            catalogs.actions,
            reason=reason,
        )
    if catalogs.views is not None:
        authored_views.write(
            ctx,
            session,
            after,
            auth.configure,
            auth.meta,
            current.views,
            catalogs.views,
            reason=reason,
        )
    return after


def _final(locked: Locked, catalogs: Catalogs, slot: SkillSlot | None) -> _Final:
    """The slots the graph holds after the push."""
    planned, current = locked.planned, locked.current
    # a catalog the release compiles again (plan M8) is the same sources, compiled anew
    again_functions = planned.functions if planned is not None else None
    again_actions = planned.actions if planned is not None else None
    again_views = planned.views if planned is not None else None
    return _Final(
        skill=slot if slot is not None else current.skill,
        functions=catalogs.functions
        or (again_functions.slot if again_functions else current.functions),
        actions=catalogs.actions or (again_actions.slot if again_actions else current.actions),
        views=catalogs.views or (again_views.slot if again_views else current.views),
    )


def _source_document(after: GraphEntry, final: _Final) -> dict[str, JsonValue]:
    """The new base file: the graph as the push leaves it."""
    return base_document(after, after.pin, final.skill, final.functions, final.actions, final.views)


def _answer(
    locked: Locked,
    catalogs: Catalogs,
    slot: SkillSlot | None,
    final: _Final,
    after: GraphEntry,
    *,
    dry_run: bool,
) -> dict[str, JsonValue]:
    """What the push answers; ``views`` comes last and only for a graph that has views."""
    ws, current = locked.ws, locked.current
    data: dict[str, JsonValue] = {
        "graph_id": str(locked.entry.id),
        "ontology": {k: v for k, v in _ontology_answer(locked).items() if k != "graph_id"},
        "skill": _skill_answer(locked, slot, final, dry_run=dry_run),
        "functions": None
        if not (ws.functions.directory.is_dir() or catalogs.functions is not None)
        else _kind_answer(
            current.functions,
            final.functions,
            "functions",
            ws.functions.files,
            published=catalogs.functions is not None and not dry_run,
        ),
        "actions": None
        if not (ws.actions.directory.is_dir() or catalogs.actions is not None)
        else _kind_answer(
            current.actions,
            final.actions,
            "actions",
            ws.actions.files,
            published=catalogs.actions is not None and not dry_run,
        ),
        "unchanged": not (
            locked.releasing
            or locked.publishing_skill
            or catalogs.functions is not None
            or catalogs.actions is not None
            or catalogs.views is not None
        ),
        "dry_run": dry_run,
        "source": _source_document(after, final),
    }
    if final.views is not None:  # as for a graph without views: follows the slot
        data["views"] = _kind_answer(
            current.views,
            final.views,
            "views",
            ws.views.files,
            published=catalogs.views is not None and not dry_run,
        )
    return data


def _ontology_answer(locked: Locked) -> dict[str, JsonValue]:
    """The release the push makes, or that there is none."""
    if locked.planned is not None:
        return locked.planned.data
    entry = locked.entry
    return {
        "from": entry.pin.to_json(),
        "to": entry.pin.to_json(),
        "released": False,
        "pinned": False,
    }


def _skill_answer(
    locked: Locked, slot: SkillSlot | None, final: _Final, *, dry_run: bool
) -> dict[str, JsonValue] | None:
    """What the push does with the skill; ``None`` without a skill directory."""
    ws, current = locked.ws, locked.current
    if ws.skill.files is None:
        return None
    return {
        "skill": ws.skill.name,
        "from": current.skill.summary() if current.skill is not None else None,
        "to": final.skill.summary() if final.skill is not None else None,
        "files": sorted(ws.skill.files),
        "published": slot is not None and not dry_run,
    }


def _kind_answer(
    current: KineticSlot | None,
    final: KineticSlot | None,
    key: str,
    files: Mapping[str, bytes],
    *,
    published: bool,
) -> dict[str, JsonValue]:
    """What the push does with the functions, actions or views: ``key`` is the name the
    names of the pushed ``files`` are answered under."""
    return {
        "from": current.summary() if current is not None else None,
        "to": final.summary() if final is not None else None,
        key: [n.removesuffix(FILE_SUFFIX) for n in sorted(files)],
        "published": published,
    }


def _warnings(
    locked: Locked, catalogs: Catalogs, checked: skill.Checked | None, after: GraphEntry
) -> tuple[WarningRecord, ...]:
    """The warnings of the push: the release's, then the pushed actions', views' and skill's.
    Computed after the writes and the new base: a warning check that raises fails the command
    late, with the push done."""
    warnings = locked.planned.warnings if locked.planned is not None else ()
    if (
        catalogs.actions is not None
    ):  # the published catalog was not looked at: judge the pushed one
        assert catalogs.action_model is not None
        warnings += marking_warnings.precondition_warnings(
            catalogs.action_model, catalogs.actions, locked.entry
        )
    if catalogs.views is not None:
        warnings += authored_views.deprecated_warnings(catalogs.views, catalogs.view_functions)
    if checked is not None:
        warnings += checked.warnings(after, locked.ws.skill.name)
    return warnings


def _authorize(
    ctx: AdminContext, entry: GraphEntry, ws: Workspace, *, reason: str | None, dry_run: bool
) -> _Authorized:
    """The decisions a push needs, before the lock: ``maint.graph_configure`` for the
    publishes, and the release's own decisions when the ontology is a new release."""
    arguments: dict[str, JsonValue] = {
        "graph": str(entry.id),
        "ontology": ws.compiled.pin.to_json(),
        "skill": ws.skill.hash,
        "functions": ws.functions.hash,
        "actions": ws.actions.hash,
        "dry_run": dry_run,
    }
    if ws.views.hash != EMPTY or ws.base.views is not None:  # a graph without views: as it was
        arguments["views"] = ws.views.hash
    meta = RequestMeta.of("graph push", arguments)
    resource = f"graph:{entry.id}"
    configure = ctx.authorize(
        [("maint.graph_configure", resource)], request=meta, graph=entry.id, reason=reason
    )
    decisions = (
        release.authorize(
            ctx,
            entry,
            meta,
            reason=reason,
            migrate=release.wants_migration(ctx, entry, ws.compiled, ws.migration),
        )
        if ws.ontology_changed
        else None
    )
    return _Authorized(meta=meta, configure=configure, decisions=decisions)


def _publishing(ws: Workspace, current: Current) -> Publishing:
    """Which of the function, action and view folders the push publishes, and the names the
    functions and the actions will have."""
    functions_published = ws.functions.changed and current.functions_hash != ws.functions.hash
    actions_published = ws.actions.changed and current.actions_hash != ws.actions.hash
    views_published = ws.views.changed and current.views_hash != ws.views.hash
    function_names = (
        _stems(ws.functions.files) if functions_published else names_of(current.functions)
    )
    action_names = _stems(ws.actions.files) if actions_published else names_of(current.actions)
    return Publishing(
        functions=functions_published,
        actions=actions_published,
        views=views_published,
        function_names=function_names,
        action_names=action_names,
    )


def _views_for_release(
    ws: Workspace, current: Current, publishes: Publishing
) -> tuple[bool, tuple[FunctionEntry, ...] | None]:
    """Whether the release leaves the published views to the push, and else the functions it
    judges them with. The published views are checked in the release with the functions the
    graph will have; pushed functions that do not compile, or take the name of an action, are
    refused later (``functions.check_files``), before any write, so the release leaves the
    views to that refusal and the user sees the function problems first."""
    if not (
        ws.ontology_changed
        and publishes.functions
        and not publishes.views
        and current.views is not None
    ):
        return publishes.views, None
    built, found = compile_catalog(ws.functions.files, ws.compiled.model)
    if found or ws.functions.problems or _stems(ws.functions.files) & publishes.action_names:
        return True, None
    return False, load_entries(built)


def _plan_release(
    ctx: AdminContext,
    session: MaintenanceSession,
    ws: Workspace,
    current: Current,
    publishes: Publishing,
    *,
    dry_run: bool,
) -> release.Planned | None:
    """The release the push makes, ``None`` when the ontology is not a new release."""
    if not ws.ontology_changed:
        return None
    leave_views, pushed_entries = _views_for_release(ws, current, publishes)
    return release.plan(
        ctx,
        session,
        ws.compiled,
        migration=ws.migration,
        dry_run=dry_run,
        functions_replaced=publishes.functions,
        actions_replaced=publishes.actions,
        function_names=publishes.function_names,
        views_replaced=leave_views,
        function_entries=pushed_entries,
    )


def _functions_slot(locked: Locked) -> KineticSlot | None:
    """The functions the push publishes, checked against the model the graph will have;
    ``None`` when it publishes none (the folder must still be valid)."""
    ws = locked.ws
    if not locked.publishes.functions:
        functions.refuse(ws.functions.directory, locked.after, ws.functions.problems)
        return None
    model = locked.model()
    checked = functions.check_files(
        locked.after,
        ws.functions.files,
        ws.functions.problems,
        model,
        label=ws.functions.directory,
        # one namespace per graph: no function named like a published action
        taken=locked.publishes.action_names,
        hints=catalog_follow.rename_hints(locked.ctx, locked.entry, model, locked.pending),
        # the published views must hold with the functions: the ones the release
        # compiled again, the pushed ones replace them
        views=None
        if locked.publishes.views
        else _after(
            locked.planned.views if locked.planned is not None else None, locked.current.views
        ),
    )
    return functions.slot_of(checked, locked.after)


def _actions_slot(locked: Locked) -> tuple[KineticSlot | None, CompiledModel | None]:
    """The actions the push publishes and the model they were checked against."""
    ws = locked.ws
    if not locked.publishes.actions:
        actions.refuse(ws.actions.directory, locked.after, ws.actions.problems)
        return None, None
    model = locked.model()
    checked = actions.check_files(
        locked.after,
        ws.actions.files,
        ws.actions.problems,
        model,
        label=ws.actions.directory,
        function_names=locked.publishes.function_names,
        hints=catalog_follow.rename_hints(locked.ctx, locked.entry, model, locked.pending),
    )
    return actions.slot_of(checked, locked.after), model


def _views_slot(
    locked: Locked, function_slot: KineticSlot | None
) -> tuple[KineticSlot | None, tuple[FunctionEntry, ...]]:
    """The views the push publishes and the functions they were checked with: the pushed
    ones, else the published ones as the release leaves them."""
    ws = locked.ws
    if not locked.publishes.views:
        authored_views.refuse(ws.views.directory, locked.after, ws.views.problems)
        return None, ()
    model = locked.model()
    entries = function_entries(
        function_slot
        if function_slot is not None
        else _after(
            locked.planned.functions if locked.planned is not None else None,
            locked.current.functions,
        )
    )
    checked = authored_views.check_files(
        locked.after,
        ws.views.files,
        ws.views.problems,
        model,
        label=ws.views.directory,
        functions=entries,
        hints=catalog_follow.rename_hints(locked.ctx, locked.entry, model, locked.pending),
    )
    return authored_views.slot_of(checked, locked.after), entries


def _check_catalogs(locked: Locked) -> Catalogs:
    """Check the catalogs the graph will have, in the order functions, actions, views: the
    first refusal is the one the user sees."""
    function_slot = _functions_slot(locked)
    action_slot, action_model = _actions_slot(locked)
    view_slot, view_functions = _views_slot(locked, function_slot)
    return Catalogs(
        functions=function_slot,
        actions=action_slot,
        views=view_slot,
        action_model=action_model,
        view_functions=view_functions,
    )


def _check_skill(
    locked: Locked, catalogs: Catalogs, *, inventory: Mapping[str, JsonValue]
) -> skill.Checked | None:
    """The skill the graph will have, checked against the ontology and the catalogs it will
    have; ``None`` when neither the release nor the skill changes it, or there is no skill."""
    if not (locked.releasing or locked.publishing_skill):
        return None
    ws, current = locked.ws, locked.current
    label: Path | str = ws.skill.directory or f"the published skill of graph {locked.entry.name}"
    files = (
        ws.skill.files
        if ws.skill.files is not None
        else (current.skill.files if current.skill else None)
    )
    if files is None:
        return None
    checked = skill.check_files(
        locked.ctx,
        locked.after,
        dict(files),
        ws.skill.problems,
        label=label,
        directory=ws.skill.name if ws.skill.files is not None else None,
        inventory=inventory,
        model=ws.compiled.model if locked.releasing else None,
        # the catalogs the graph will have: `graph run <name>` and `graph do <name>` in
        # the text are checked
        functions=catalogs.functions.compiled if catalogs.functions is not None else None,
        actions=catalogs.actions.compiled if catalogs.actions is not None else None,
        views=catalogs.views.compiled if catalogs.views is not None else None,
    )
    skill.refuse(label, locked.after, ws.skill.name, checked.problems)
    return checked


def _skill_slot(locked: Locked, checked: skill.Checked | None) -> SkillSlot | None:
    """The slot of the skill the push publishes, ``None`` when it publishes none."""
    if not locked.publishing_skill:
        return None
    ws = locked.ws
    assert checked is not None and ws.skill.files is not None and ws.skill.hash is not None
    return SkillSlot(checked.kind, ws.skill.hash, locked.after.pin, ws.skill.files)


def _read_current(session: MaintenanceSession) -> Current:
    """The published slots, read under the lock in the order skill, functions, actions, views."""
    return Current(
        skill=skill.current_slot(session),
        functions=functions.current_slot(session),
        actions=actions.current_slot(session),
        views=authored_views.current_slot(session),
    )


def _check_unmoved(
    ws: Workspace, entry: GraphEntry, current: Current, target_pin: OntologyPin
) -> None:
    """``admin.source_conflict`` unless every part of the graph is still what the base says
    or already what the push makes of it (a repeated push)."""
    base = ws.base
    target_skill = ws.skill.hash if ws.skill.changed else base.skill
    target_functions = ws.functions.hash if ws.functions.changed else base.functions
    target_actions = ws.actions.hash if ws.actions.changed else base.actions
    target_views = ws.views.hash if ws.views.changed else base.views
    if (
        entry.pin not in (base.pin, target_pin)
        or current.skill_hash not in (base.skill, target_skill)
        or current.functions_hash not in (base.functions or EMPTY, target_functions)
        or current.actions_hash not in (base.actions or EMPTY, target_actions)
        or current.views_hash not in (base.views or EMPTY, target_views)
    ):
        raise _conflict(ws.source, entry, base, current)


def _after(republish: Republish | None, published: KineticSlot | None) -> KineticSlot | None:
    """The catalog the graph has after the release: the one the release compiles again, else
    the published one."""
    return republish.slot if republish is not None else published


def _stems(files: Mapping[str, bytes]) -> frozenset[str]:
    """The names the files of a kinetic folder claim (``<name>.yaml``)."""
    return frozenset(n.removesuffix(FILE_SUFFIX) for n in files)


def _conflict(source: Path, entry: GraphEntry, base: Base, current: Current) -> GraphCliError:
    base_details: dict[str, JsonValue] = {
        "ontology": base.pin.to_json(),
        "skill": base.skill,
        "functions": base.functions,
        "actions": base.actions,
    }
    current_details: dict[str, JsonValue] = {
        "ontology": entry.pin.to_json(),
        "skill": current.skill.hash if current.skill is not None else None,
        "functions": current.functions.hash if current.functions is not None else None,
        "actions": current.actions.hash if current.actions is not None else None,
    }
    if base.views is not None or current.views is not None:  # a graph without views: as it was
        base_details["views"] = base.views
        current_details["views"] = current.views.hash if current.views is not None else None
    return GraphCliError(
        "admin.source_conflict",
        f"graph {entry.name} changed since {source} was pulled or last pushed; pull it into"
        " another directory, carry your changes over and push from there",
        details={
            "graph": entry.name,
            "dir": str(source),
            "base": base_details,
            "current": current_details,
        },
    )
