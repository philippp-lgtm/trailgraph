"""``graph skills emit|install|check`` (D15 Q1, Q3-Q5).

Skill 1 is rendered in-process from this program's own inventory; graph
skills come from the graphs' published slots, read with ``read.describe``
through the read service, so the pointer allowlist applies (for
``--scope project`` the pointer as seen from the project directory). The
app writes outside the runtime area only into the directory named on the
command line. Every refusal is found before anything is written. ``check``
compares what is installed with what this very process renders now (F6).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from graph_cli import __version__
from graph_cli.app_cli.commands.common import PROGRAM, services
from graph_cli.catalog.pointer import PointerFile
from graph_cli.catalog.wiring import AppContext
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli.skills import sources
from graph_cli.skills.install import (
    STATUSES,
    Installed,
    RenderedSkill,
    SkillTarget,
    apply_install,
    check,
    emit_skills,
    plan_install,
    refusal,
    require_target,
    scan,
)
from graph_cli.skills.template import GENERATED
from graph_cli.skills.tree import GRAPH_SKILL_PREFIX

GRAPHS: Final = ParamSpec(
    "--graph",
    "graph_ref",
    "Also the skill of this graph (name or id); repeatable.",
    repeatable=True,
    keyword="graphs",
)
EMIT_FORCE: Final = ParamSpec("--force", "flag", "Replace skill directories that exist.")
FORCE: Final = ParamSpec(
    "--force", "flag", "Replace skill directories that are not ours or were edited by hand."
)
SCOPE: Final = ParamSpec(
    "--scope",
    "choice",
    "user: the agent's user skills; project: <dir>/.claude/skills.",
    required=True,
    choices=("user", "project"),
)
DIR: Final = ParamSpec(
    "--dir", "directory", "Project directory for --scope project; default: the working directory."
)
DRY_RUN: Final = ParamSpec("--dry-run", "flag", "Show what would be installed; write nothing.")
OUT: Final = ParamSpec(
    "--out", "directory", "Existing directory to write one directory per skill into.", required=True
)
GRAPH_ERRORS: Final = (
    "graph.not_found",
    "graph.not_in_project",
    "config.invalid",
    "config.forbidden_key",
    "access.denied",
    "skills.source_missing",
    "skills.source_invalid",
)


def program_block() -> dict[str, JsonValue]:
    return {"name": PROGRAM, "version": __version__}


def app_inventory() -> dict[str, JsonValue]:
    """The inventory of this very process (F6), never of a subprocess."""
    from graph_cli.app_cli.commands import all_specs
    from graph_cli.app_cli.commands.meta import inventory

    return inventory(all_specs())


def app_skill() -> RenderedSkill:
    """Skill 1 from the inventory of this very process (F6)."""
    return sources.app_skill(app_inventory(), program_block())


def graph_skill(ctx: AppContext, entry: GraphEntry, warnings: list[WarningRecord]) -> RenderedSkill:
    """The published skill of ``entry``; ``skills.source_missing`` when there is none. A
    template (kind ``generated``) is rendered with the model of the graph's current pin."""
    request = RequestMeta.of("skills", {"graph": entry.name})
    slot = ctx.reader.skill(ctx.actor, entry, request)
    if slot is None:
        raise GraphCliError(
            "skills.source_missing",
            f"graph {entry.name} has no published skill",
            details={"graph": entry.name},
        )
    warning = sources.behind_ontology(entry, slot)
    if warning is not None:
        warnings.append(warning)
    model, inventory = None, None
    if slot.kind == GENERATED:
        model, model_warnings = ctx.reader.skill_model(ctx.actor, entry, request)
        warnings += model_warnings
        inventory = app_inventory()
    skill, found = sources.graph_skill(entry, slot, program_block(), model, inventory)
    warnings += found
    return skill


def named_graph_skills(
    ctx: AppContext, refs: Sequence[str], warnings: list[WarningRecord]
) -> list[RenderedSkill]:
    """The skills of the graphs named with ``--graph``, selected like any ``--graph``."""
    entries: dict[str, GraphEntry] = {}
    for ref in refs:
        entry = ctx.select(ref)
        entries.setdefault(str(entry.id), entry)
    return [graph_skill(ctx, entry, warnings) for entry in entries.values()]


def emit(state: CommandState, out: str, graphs: tuple[str, ...], force: bool) -> Result:
    target = services(state).path(out)
    if not target.is_dir():
        raise GraphCliError(
            "usage.invalid_argument",
            f"--out {out}: no such directory; create it first",
            details={"argument": "out"},
        )
    skills = [app_skill()]
    if graphs:
        ctx = services(state).context(state.warnings, state.command)
        skills += named_graph_skills(ctx, graphs, state.warnings)
    details: dict[str, JsonValue] = {"out": str(target), "graphs": list(graphs)}
    steps = emit_skills(target, skills, force=force, details=details)
    return Result(
        data={
            "out": str(target),
            "program": program_block(),
            "skills": [
                {
                    "skill": step.skill.name,
                    "kind": step.skill.kind,
                    "action": step.action,
                    "files": sorted(step.skill.files),
                }
                for step in steps
            ],
        }
    )


# ---- install -----------------------------------------------------------------------

# A marked graph skill whose graph cannot be refreshed is reported and left alone.
UNAVAILABLE: Final = frozenset(
    {
        "graph.not_found",
        "graph.not_in_project",
        "access.denied",
        "backend.unavailable",
        "skills.source_missing",
        "skills.source_invalid",
    }
)


def target_of(state: CommandState, scope: str, dir: str | None) -> SkillTarget:
    if scope == "user" and dir is not None:
        raise GraphCliError(
            "usage.invalid_argument",
            "--dir names a project; it does not go with --scope user",
            details={"argument": "dir"},
        )
    chosen: Literal["user", "project"] = "user" if scope == "user" else "project"
    config_dir, project = services(state).skill_dirs(chosen, dir)
    target = SkillTarget(chosen, config_dir, project)
    require_target(target)
    return target


def pointer_of(state: CommandState, ctx: AppContext, target: SkillTarget) -> PointerFile | None:
    """Q3 visibility: a project gets the skills of the graphs it may use."""
    if target.dir is None:
        return ctx.pointer
    pointer, warnings = services(state).pointer_at(target.dir)
    state.warnings += warnings
    return pointer


def installed_graph_ref(found: Installed) -> str | None:
    """Which graph a marked graph skill belongs to: the marker's id, else (legacy marker)
    the name ``graph-<graph>``."""
    if found.integrity not in ("intact", "legacy", "modified", "unexpected"):
        return None
    if found.marker is not None:
        graph = found.marker.source.get("graph")
        if isinstance(graph, dict) and isinstance(graph.get("id"), str):
            return str(graph["id"])
    if found.kind == "graph" and found.name.startswith(GRAPH_SKILL_PREFIX):
        return found.name.removeprefix(GRAPH_SKILL_PREFIX).replace("-", "_")
    return None


def wanted_skills(
    state: CommandState,
    target: SkillTarget,
    installed: dict[str, Installed],
    graphs: Sequence[str],
) -> tuple[list[RenderedSkill], dict[str, dict[str, JsonValue]]]:
    """Skill 1, the named graph skills and the graph skills already installed by us.

    A named graph that cannot be installed is an error; an installed one becomes
    ``unavailable`` (``{skill: {graph, reason}}``).
    """
    wanted = [app_skill()]
    unavailable: dict[str, dict[str, JsonValue]] = {}
    refresh = {
        name: ref
        for name, found in installed.items()
        if found.kind == "graph" and (ref := installed_graph_ref(found)) is not None
    }
    if not graphs and not refresh:
        return wanted, unavailable
    ctx = services(state).context(state.warnings, state.command)
    pointer = pointer_of(state, ctx, target)
    entries: dict[str, GraphEntry] = {}
    for ref in graphs:
        entry = ctx.select_for(ref, pointer)
        entries.setdefault(str(entry.id), entry)
    named = [graph_skill(ctx, entry, state.warnings) for entry in entries.values()]
    wanted += named
    for name, ref in sorted(refresh.items()):
        if name in {skill.name for skill in named}:
            continue
        try:
            entry = ctx.select_for(ref, pointer)
            wanted.append(graph_skill(ctx, entry, state.warnings))
        except GraphCliError as exc:
            if exc.code not in UNAVAILABLE:
                raise
            unavailable[name] = {"graph": ref, "reason": exc.code}
    return wanted, unavailable


def install(
    state: CommandState,
    scope: str,
    dir: str | None,
    graphs: tuple[str, ...],
    force: bool,
    dry_run: bool,
) -> Result:
    target = target_of(state, scope, dir)
    installed = scan(target)
    wanted, unavailable = wanted_skills(state, target, installed, graphs)
    plan = plan_install(target, installed, wanted, force=force)
    error = refusal(plan, {"graphs": list(graphs)})
    if error is not None:
        raise error
    if not dry_run:
        apply_install(plan)
    return Result(
        data={
            "target": target.to_json(),
            "dry_run": dry_run,
            "program": program_block(),
            "skills": [step.to_json() for step in plan.steps],
            "unavailable": [
                {"skill": name, **details} for name, details in sorted(unavailable.items())
            ],
        }
    )


# ---- check ---------------------------------------------------------------------------


def check_command(state: CommandState, scope: str, dir: str | None) -> Result:
    target = target_of(state, scope, dir)
    installed = scan(target)
    wanted, unavailable = wanted_skills(state, target, installed, ())
    statuses = check(installed, wanted, unavailable=unavailable)
    counts = {status: sum(s.status == status for s in statuses) for status in STATUSES}
    program = {**program_block(), "executable": services(state).executable}
    data: dict[str, JsonValue] = {
        "target": target.to_json(),
        "program": program,
        "skills": [s.to_json() for s in statuses],
        "ok_count": counts["fresh"],
        "counts": {status: n for status, n in counts.items() if n},
    }
    bad = [s for s in statuses if s.status != "fresh"]
    if not bad:
        return Result(data=data)
    error = GraphCliError(
        "skills.check_failed",
        f"{len(bad)} of {len(statuses)} skills are not fresh: "
        + ", ".join(f"{s.name} {s.status}" for s in bad),
        details={**target.details(), "counts": data["counts"]},
    )
    return Result(data=data, error=error)


SPECS: Final = (
    CommandSpec(
        path=("skills", "emit"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Write the agent skills of this program and named graphs into a directory.",
        params=(OUT, GRAPHS, EMIT_FORCE),
        examples=(
            Example("graph skills emit --out <dir>", "Write the skill of this program."),
            Example(
                "graph skills emit --out <dir> --graph <graph> --json",
                "Also the skill of one graph.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "skills.not_owned", "skills.target_invalid"),
        handler=emit,
        description="One directory per skill, without an install marker. Install skills"
        " for an agent with `graph skills install` instead.",
    ),
    CommandSpec(
        path=("skills", "install"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Install or refresh the agent skills of this program and its graphs.",
        params=(SCOPE, DIR, GRAPHS, FORCE, DRY_RUN),
        examples=(
            Example("graph skills install --scope project --dir <dir>", "Into one project."),
            Example(
                "graph skills install --scope user --graph <graph> --json",
                "For every project of the user, with one graph skill.",
            ),
            Example("graph skills install --scope project --dry-run --json", "Show the plan."),
        ),
        errors=(
            *GRAPH_ERRORS,
            "usage.missing_argument",
            "skills.not_owned",
            "skills.modified",
            "skills.target_invalid",
        ),
        handler=install,
        description="--scope user installs into $CLAUDE_CONFIG_DIR/skills, else"
        " ~/.claude/skills; --scope project into <dir>/.claude/skills (<dir>/.claude must"
        " exist). Skill 1 is always installed, graph skills named with --graph, and every"
        " graph skill installed here before is refreshed. Directories without our marker"
        " (.graph_cli_skill.json) or edited by hand are replaced only with --force.",
    ),
    CommandSpec(
        path=("skills", "check"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Check that installed agent skills equal what this program installs now.",
        params=(SCOPE, DIR),
        examples=(
            Example("graph skills check --scope project --dir <dir>", "Check one project."),
            Example("graph skills check --scope user --json", "Per-skill status as JSON."),
        ),
        errors=(
            "config.invalid",
            "config.forbidden_key",
            "usage.missing_argument",
            "skills.check_failed",
            "skills.target_invalid",
        ),
        handler=check_command,
        description="Statuses: fresh, stale (installed unchanged, but the program, the"
        " published graph skill or, for a generated one, the graph's ontology changed since),"
        " modified (edited by hand), unexpected (extra"
        " files), foreign (not installed by this program), missing, unavailable (the graph"
        " is gone, not visible here or has no published skill). Exit 0 only when every"
        " skill is fresh. Only reads.",
    ),
)
