"""What ``graph push`` answers, records as the new base, and warns about.

``final_slots`` gives the slots the graph holds once the push is done (or
would hold: a dry run); the answer (``answer``), the new base file
(``source_document``) and the warnings of the release, the actions, the views
and the skill (``push_warnings``) are built from the frame of the checks
(``sources_push_state``) and those slots. Nothing here changes the graph or
checks it: ``sources_push`` calls these after the checks and the writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SkillSlot
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli_admin.services_admin import authored_views, functions, marking_warnings, skill
from graph_cli_admin.services_admin.sources import base_document
from graph_cli_admin.services_admin.sources_push_state import Catalogs, Locked


@dataclass(frozen=True, slots=True)
class FinalSlots:
    """What the graph holds once the push is done (or would hold: a dry run): the published
    slots, else what the release compiles again, else what is published now."""

    skill: SkillSlot | None
    functions: KineticSlot | None
    actions: KineticSlot | None
    views: KineticSlot | None


def final_slots(locked: Locked, catalogs: Catalogs, slot: SkillSlot | None) -> FinalSlots:
    """The slots the graph holds after the push."""
    planned, current = locked.planned, locked.current
    # a catalog the release compiles again (plan M8) is the same sources, compiled anew
    again_functions = planned.functions if planned is not None else None
    again_actions = planned.actions if planned is not None else None
    again_views = planned.views if planned is not None else None
    return FinalSlots(
        skill=slot if slot is not None else current.skill,
        functions=catalogs.functions
        or (again_functions.slot if again_functions else current.functions),
        actions=catalogs.actions or (again_actions.slot if again_actions else current.actions),
        views=catalogs.views or (again_views.slot if again_views else current.views),
    )


def source_document(after: GraphEntry, final: FinalSlots) -> dict[str, JsonValue]:
    """The new base file: the graph as the push leaves it."""
    return base_document(after, after.pin, final.skill, final.functions, final.actions, final.views)


def answer(
    locked: Locked,
    catalogs: Catalogs,
    slot: SkillSlot | None,
    final: FinalSlots,
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
        "source": source_document(after, final),
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
    locked: Locked, slot: SkillSlot | None, final: FinalSlots, *, dry_run: bool
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


def push_warnings(
    locked: Locked,
    catalogs: Catalogs,
    checked: skill.Checked | None,
    after: GraphEntry,
    linted: tuple[WarningRecord, ...] = (),
) -> tuple[WarningRecord, ...]:
    """The warnings of the push: the release's, then the pushed functions', actions', views' and
    skill's; ``linted`` (the lint of the published skill against pushed functions, when the push
    leaves the skill alone) comes last. Computed after the writes and the new base: a warning
    check that raises fails the command late, with the push done."""
    warnings = locked.planned.warnings if locked.planned is not None else ()
    if catalogs.functions is not None:
        warnings += functions.single_ref_warnings(
            after, locked.model(), catalogs.functions, final_slots(locked, catalogs, None).views
        )
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
    return warnings + linted
