"""``graph skills emit`` (D15 Q1, Q3).

Skill 1 is rendered in-process from this program's own inventory; graph
skills come from the graphs' published slots, read with ``read.describe``
through the read service, so the pointer allowlist applies. The app writes
outside the runtime area only into the directory named on the command line.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from graph_cli import __version__
from graph_cli.app_cli.commands.common import PROGRAM, services
from graph_cli.catalog.wiring import AppContext
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli.skills import sources
from graph_cli.skills.install import RenderedSkill, emit_skills

GRAPHS: Final = ParamSpec(
    "--graph",
    "graph_ref",
    "Also the skill of this graph (name or id); repeatable.",
    repeatable=True,
    keyword="graphs",
)
EMIT_FORCE: Final = ParamSpec("--force", "flag", "Replace skill directories that exist.")
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


def app_skill() -> RenderedSkill:
    """Skill 1 from the inventory of this very process (F6), never from a subprocess."""
    from graph_cli.app_cli.commands import all_specs
    from graph_cli.app_cli.commands.meta import inventory

    return sources.app_skill(inventory(all_specs()), program_block())


def graph_skill(ctx: AppContext, entry: GraphEntry, warnings: list[WarningRecord]) -> RenderedSkill:
    """The published skill of ``entry``; ``skills.source_missing`` when there is none."""
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
    return sources.graph_skill(entry, slot, program_block())


def named_graph_skills(
    ctx: AppContext, refs: Sequence[str], warnings: list[WarningRecord]
) -> list[RenderedSkill]:
    """The skills of the graphs named with ``--graph``, selected like any ``--graph``."""
    entries: dict[str, GraphEntry] = {}
    for ref in refs:
        entry = ctx.select(ref)
        entries.setdefault(str(entry.id), entry)
    return [graph_skill(ctx, entry, warnings) for entry in entries.values()]


def directory(state: CommandState, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else services(state).cwd / path


def emit(state: CommandState, out: str, graphs: tuple[str, ...], force: bool) -> Result:
    target = directory(state, out)
    if not target.is_dir():
        raise GraphCliError(
            "usage.invalid_argument",
            f"--out {out}: no such directory; create it first",
            details={"argument": "out"},
        )
    skills = [app_skill()]
    if graphs:
        ctx = services(state).context(state.warnings)
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
        description="One directory per skill, without an install marker.",
    ),
)
