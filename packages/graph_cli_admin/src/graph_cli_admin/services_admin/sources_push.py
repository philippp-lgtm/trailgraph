"""graph-admin graph push: a maintainer workspace pushed back to its graph.

The workspace layout, the base file and pull are sources.

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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import (
    GraphEntry,
    KineticSlot,
    SkillSlot,
    SlotKind,
    skill_hash,
    sources_hash,
)
from graph_cli.kernel.records import RequestMeta
from graph_cli.kernel.skill_marker import graph_skill_name
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli.kinetic.params import Problem as KineticProblem
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.semver import Version, is_version
from graph_cli.skills.tree import Problem, read_tree
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.services_admin import (
    actions,
    authored_views,
    catalog_follow,
    functions,
    kinetic_slots,
    marking_warnings,
    release,
    skill,
)
from graph_cli_admin.services_admin.catalog_follow import Republish, function_entries, names_of
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import (
    Compiled,
    compile_source,
    pinned_model,
    read_migration,
)
from graph_cli_admin.services_admin.sources import (
    ACTIONS,
    FUNCTIONS,
    ONTOLOGY,
    SKILL,
    VIEWS,
    Base,
    base_document,
    read_base,
    release_sources,
    usage_error,
    write_base,
)

_EMPTY: Final = sources_hash({})  # a missing slot and an empty one are the same


@dataclass(frozen=True, slots=True)
class Pushed:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


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
    compiled, ontology_dir = _ontology(source, base)
    migration = read_migration(ontology_dir)
    name = graph_skill_name(entry.name)
    skill_dir = _skill_dir(source, name)
    files: dict[str, bytes] | None = None
    problems: list[Problem] = []
    if skill_dir is not None:
        try:
            files, problems = read_tree(skill_dir)
        except OSError as exc:
            raise usage_error(f"cannot read {skill_dir}: {exc.strerror or exc}") from exc
    pushed = skill_hash(files) if files is not None else None
    skill_changed = pushed is not None and pushed != base.skill
    sources_dir = source / FUNCTIONS
    function_files, function_problems = _kinetic_files(sources_dir, "functions")
    pushed_functions = sources_hash(function_files)
    functions_changed = pushed_functions != (base.functions or _EMPTY)
    actions_dir = source / ACTIONS
    action_files, action_problems = _kinetic_files(actions_dir, "actions")
    pushed_actions = sources_hash(action_files)
    actions_changed = pushed_actions != (base.actions or _EMPTY)
    views_dir = source / VIEWS
    view_files, view_problems = _kinetic_files(views_dir, "views")
    pushed_views = sources_hash(view_files)
    views_changed = pushed_views != (base.views or _EMPTY)
    ontology_changed = _ontology_changed(ctx, entry, base, compiled, source / ONTOLOGY)
    arguments: dict[str, JsonValue] = {
        "graph": str(entry.id),
        "ontology": compiled.pin.to_json(),
        "skill": pushed,
        "functions": pushed_functions,
        "actions": pushed_actions,
        "dry_run": dry_run,
    }
    if pushed_views != _EMPTY or base.views is not None:  # a graph without views: as it was
        arguments["views"] = pushed_views
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
            migrate=release.wants_migration(ctx, entry, compiled, migration),
        )
        if ontology_changed
        else None
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        release.check_state(entry)
        current = skill.current_slot(session)
        target_pin = compiled.pin if ontology_changed else base.pin
        target_skill = pushed if skill_changed else base.skill
        current_skill = current.hash if current is not None else None
        published = functions.current_slot(session)
        current_functions = published.hash if published is not None else _EMPTY
        target_functions = pushed_functions if functions_changed else base.functions
        catalog = actions.current_slot(session)
        current_actions = catalog.hash if catalog is not None else _EMPTY
        target_actions = pushed_actions if actions_changed else base.actions
        view_catalog = authored_views.current_slot(session)
        current_views = view_catalog.hash if view_catalog is not None else _EMPTY
        target_views = pushed_views if views_changed else base.views
        if (
            entry.pin not in (base.pin, target_pin)
            or current_skill not in (base.skill, target_skill)
            or current_functions not in (base.functions or _EMPTY, target_functions)
            or current_actions not in (base.actions or _EMPTY, target_actions)
            or current_views not in (base.views or _EMPTY, target_views)
        ):
            raise _conflict(source, entry, base, current, published, catalog, view_catalog)
        publishing_functions = functions_changed and current_functions != pushed_functions
        publishing_actions = actions_changed and current_actions != pushed_actions
        publishing_views = views_changed and current_views != pushed_views
        # One namespace per graph: each kind is checked against the names of the other kind
        # the graph will have (the pushed files if the push publishes them, else the published).
        function_names = _stems(function_files) if publishing_functions else names_of(published)
        action_names = _stems(action_files) if publishing_actions else names_of(catalog)
        # The published views are checked in the release with the functions the graph will have;
        # pushed functions that do not compile, or take the name of an action, are refused below
        # (``functions.check_files``), before any write, so the release leaves the views to that
        # refusal and the user sees the function problems first.
        pushed_entries: tuple[FunctionEntry, ...] | None = None
        leave_views = publishing_views
        if (
            ontology_changed
            and publishing_functions
            and not publishing_views
            and view_catalog is not None
        ):
            built, found = compile_catalog(function_files, compiled.model)
            if found or function_problems or _stems(function_files) & action_names:
                leave_views = True
            else:
                pushed_entries = load_entries(built)
        planned = (
            release.plan(
                ctx,
                session,
                compiled,
                migration=migration,
                dry_run=dry_run,
                functions_replaced=publishing_functions,
                actions_replaced=publishing_actions,
                function_names=function_names,
                views_replaced=leave_views,
                function_entries=pushed_entries,
            )
            if ontology_changed
            else None
        )
        releasing = planned is not None and not planned.unchanged
        after = replace(entry, pin=target_pin) if releasing else entry
        publishing = skill_changed and current_skill != pushed
        # The functions the graph will have must resolve against the ontology it will have.
        pending = _pending_hop(entry, planned) if releasing else None
        function_slot = None
        if publishing_functions:
            label_f: Path | str = sources_dir
            model_f = compiled.model if releasing else pinned_model(ctx.catalog, after)
            checked_f = functions.check_files(
                after,
                function_files,
                function_problems,
                model_f,
                label=label_f,
                # one namespace per graph: no function named like a published action
                taken=action_names,
                hints=catalog_follow.rename_hints(ctx, entry, model_f, pending),
                # the published views must hold with the functions: the ones the release
                # compiled again, the pushed ones replace them
                views=None
                if publishing_views
                else _after(planned.views if planned is not None else None, view_catalog),
            )
            function_slot = functions.slot_of(checked_f, after)
        else:
            functions.refuse(sources_dir, after, function_problems)
        action_slot = None
        if publishing_actions:
            model_a = compiled.model if releasing else pinned_model(ctx.catalog, after)
            checked_a = actions.check_files(
                after,
                action_files,
                action_problems,
                model_a,
                label=actions_dir,
                function_names=function_names,
                hints=catalog_follow.rename_hints(ctx, entry, model_a, pending),
            )
            action_slot = actions.slot_of(checked_a, after)
        else:
            actions.refuse(actions_dir, after, action_problems)
        view_slot = None
        if publishing_views:
            model_v = compiled.model if releasing else pinned_model(ctx.catalog, after)
            # the functions the graph will have: the pushed ones, else the published ones as
            # the release leaves them
            entries = function_entries(
                function_slot
                if function_slot is not None
                else _after(planned.functions if planned is not None else None, published)
            )
            checked_v = authored_views.check_files(
                after,
                view_files,
                view_problems,
                model_v,
                label=views_dir,
                functions=entries,
                hints=catalog_follow.rename_hints(ctx, entry, model_v, pending),
            )
            view_slot = authored_views.slot_of(checked_v, after)
        else:
            authored_views.refuse(views_dir, after, view_problems)
        checked: skill.Checked | None = None
        if releasing or publishing:
            # The skill the graph will have must fit the ontology it will have.
            model = compiled.model if releasing else None
            label: Path | str = skill_dir or f"the published skill of graph {entry.name}"
            check = files if files is not None else (current.files if current else None)
            if check is not None:
                checked = skill.check_files(
                    ctx,
                    after,
                    dict(check),
                    problems,
                    label=label,
                    directory=name if files is not None else None,
                    inventory=inventory,
                    model=model,
                    # the catalogs the graph will have: `graph run <name>` and `graph do <name>` in
                    # the text are checked
                    functions=function_slot.compiled if function_slot is not None else None,
                    actions=action_slot.compiled if action_slot is not None else None,
                    views=view_slot.compiled if view_slot is not None else None,
                )
                skill.refuse(label, after, name, checked.problems)
        slot = None
        if publishing:
            assert checked is not None and files is not None and pushed is not None
            slot = SkillSlot(checked.kind, pushed, after.pin, files)
        seq = session.head().seq
        if not dry_run:
            if releasing:
                assert planned is not None and decisions is not None
                after = release.write(ctx, session, planned, decisions, meta, reason=reason)
            if slot is not None:
                skill.write(ctx, session, after, configure, meta, current, slot, reason=reason)
            if function_slot is not None:
                functions.write(
                    ctx, session, after, configure, meta, published, function_slot, reason=reason
                )
            if action_slot is not None:
                actions.write(
                    ctx, session, after, configure, meta, catalog, action_slot, reason=reason
                )
            if view_slot is not None:
                authored_views.write(
                    ctx, session, after, configure, meta, view_catalog, view_slot, reason=reason
                )
        ontology: dict[str, JsonValue] = (
            planned.data
            if planned is not None
            else {
                "from": entry.pin.to_json(),
                "to": entry.pin.to_json(),
                "released": False,
                "pinned": False,
            }
        )
        final = slot if slot is not None else current
        # a catalog the release compiles again (plan M8) is the same sources, compiled anew
        again_functions = planned.functions if planned is not None else None
        again_actions = planned.actions if planned is not None else None
        again_views = planned.views if planned is not None else None
        final_functions = function_slot or (again_functions.slot if again_functions else published)
        final_actions = action_slot or (again_actions.slot if again_actions else catalog)
        final_views = view_slot or (again_views.slot if again_views else view_catalog)
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "ontology": {k: v for k, v in ontology.items() if k != "graph_id"},
            "skill": None
            if files is None
            else {
                "skill": name,
                "from": current.summary() if current is not None else None,
                "to": final.summary() if final is not None else None,
                "files": sorted(files),
                "published": slot is not None and not dry_run,
            },
            "functions": None
            if not (sources_dir.is_dir() or function_slot is not None)
            else {
                "from": published.summary() if published is not None else None,
                "to": final_functions.summary() if final_functions is not None else None,
                "functions": [n.removesuffix(FILE_SUFFIX) for n in sorted(function_files)],
                "published": function_slot is not None and not dry_run,
            },
            "actions": None
            if not (actions_dir.is_dir() or action_slot is not None)
            else {
                "from": catalog.summary() if catalog is not None else None,
                "to": final_actions.summary() if final_actions is not None else None,
                "actions": [n.removesuffix(FILE_SUFFIX) for n in sorted(action_files)],
                "published": action_slot is not None and not dry_run,
            },
            "unchanged": not (
                releasing
                or publishing
                or function_slot is not None
                or action_slot is not None
                or view_slot is not None
            ),
            "dry_run": dry_run,
            "source": base_document(
                after, after.pin, final, final_functions, final_actions, final_views
            ),
        }
        if final_views is not None:  # as for a graph without views: follows the slot
            data["views"] = {
                "from": view_catalog.summary() if view_catalog is not None else None,
                "to": final_views.summary() if final_views is not None else None,
                "views": [n.removesuffix(FILE_SUFFIX) for n in sorted(view_files)],
                "published": view_slot is not None and not dry_run,
            }
    if not dry_run:
        write_base(
            source,
            base_document(after, after.pin, final, final_functions, final_actions, final_views),
        )
    warnings = planned.warnings if planned is not None else ()
    if action_slot is not None:  # the published catalog was not looked at: judge the pushed one
        warnings += marking_warnings.precondition_warnings(model_a, action_slot, entry)
    if view_slot is not None:
        warnings += authored_views.deprecated_warnings(view_slot, entries)
    if checked is not None:
        warnings += checked.warnings(after, name)
    return Pushed(after, data, seq, warnings)


def _ontology(source: Path, base: Base) -> tuple[Compiled, Path]:
    """The highest version directory of ``ontology/``, compiled with the base's main
    module, and the directory; its name must be the version its main module declares."""
    directory = source / ONTOLOGY
    try:
        versions = sorted(
            (Version.parse(p.name) for p in directory.iterdir() if is_version(p.name)),
            reverse=True,
        )
    except OSError as exc:
        raise usage_error(f"cannot read {directory}: {exc.strerror or exc}") from exc
    if not versions:
        raise usage_error(f"{directory} holds no version directory (<major>.<minor>.<patch>)")
    latest = directory / str(versions[0])
    main = base.pin.bundle
    compiled = compile_source(latest, main if (latest / f"{main}.yaml").is_file() else None)
    declared = compiled.model.bundle_version
    if declared != latest.name:
        raise usage_error(
            f"{latest} declares version {declared}; name the directory after the version"
            " (bump the module version in a new directory)"
        )
    return compiled, latest


def _kinetic_files(
    directory: Path, kind: SlotKind
) -> tuple[dict[str, bytes], list[KineticProblem]]:
    """The files of ``functions/`` or ``actions/``; a missing folder is an empty one."""
    if not directory.is_dir():
        return {}, []
    return kinetic_slots.read_dir(directory, kind)


def _after(republish: Republish | None, published: KineticSlot | None) -> KineticSlot | None:
    """The catalog the graph has after the release: the one the release compiles again, else
    the published one."""
    return republish.slot if republish is not None else published


def _stems(files: Mapping[str, bytes]) -> frozenset[str]:
    """The names the files of a kinetic folder claim (``<name>.yaml``)."""
    return frozenset(n.removesuffix(FILE_SUFFIX) for n in files)


def _skill_dir(source: Path, name: str) -> Path | None:
    """``skill/<name>/`` when it exists; any other entry of ``skill/`` is a usage error
    (a renamed graph, a typo), except hidden files such as ``.DS_Store``."""
    directory = source / SKILL
    try:
        found = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise usage_error(f"cannot read {directory}: {exc.strerror or exc}") from exc
    other = [n for n in found if n != name]
    if other:
        raise usage_error(
            f"{directory} holds {', '.join(other)}; the skill of this graph is {name}"
        )
    return directory / name if found else None


def _ontology_changed(
    ctx: AdminContext, entry: GraphEntry, base: Base, compiled: Compiled, directory: Path
) -> bool:
    """Whether the workspace's ontology is a new release. The base version is compared by
    its authored modules (the compiled hash may change with the compiler while they do
    not); edited modules under the base version are ``admin.release_exists``: a release
    never changes, so even an edited comment needs a new version."""
    if compiled.pin.version != base.pin.version:
        return True
    installed = release_hashes(GraphLayout(ctx.catalog.graphs_dir / str(entry.id)))
    if base.pin.version not in installed:
        return True  # cannot happen after a pull; the release decides
    _, modules = release_sources(ctx, entry, base.pin.version, installed[base.pin.version])
    if dict(compiled.sources) == modules:
        return False
    raise GraphCliError(
        "admin.release_exists",
        f"{directory} differs from release {base.pin.version}; copy it to a new version"
        " directory and bump the module version there",
        details={
            "version": base.pin.version,
            "expected": installed[base.pin.version],
            "actual": compiled.pin.artifact_hash,
        },
    )


def _conflict(
    source: Path,
    entry: GraphEntry,
    base: Base,
    current: SkillSlot | None,
    published: KineticSlot | None,
    catalog: KineticSlot | None,
    views: KineticSlot | None,
) -> GraphCliError:
    base_details: dict[str, JsonValue] = {
        "ontology": base.pin.to_json(),
        "skill": base.skill,
        "functions": base.functions,
        "actions": base.actions,
    }
    current_details: dict[str, JsonValue] = {
        "ontology": entry.pin.to_json(),
        "skill": current.hash if current is not None else None,
        "functions": published.hash if published is not None else None,
        "actions": catalog.hash if catalog is not None else None,
    }
    if base.views is not None or views is not None:  # a graph without views: as it was
        base_details["views"] = base.views
        current_details["views"] = views.hash if views is not None else None
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


def _pending_hop(entry: GraphEntry, planned: release.Planned | None) -> Hop | None:
    """The release a push makes, as the last step of the graph's name map: the rename hints
    of the functions and actions it publishes name its version (migration plan M7)."""
    if planned is None or planned.old is None:
        return None
    old, new = planned.old.model, planned.compiled.model
    run = planned.migration
    renames = run.renames if run is not None else derive_renames(old, new)
    doc = run.migration.doc if run is not None and run.migration is not None else None
    return Hop(entry.pin.version, planned.compiled.pin.version, old, new, renames, doc)
