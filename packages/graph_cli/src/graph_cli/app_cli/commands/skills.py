"""``graph skills emit|install|check`` (D15 Q1, Q3-Q5).

Skill 1 is rendered in-process from this program's own inventory; graph
skills come from the graphs' published slots, read with ``read.describe``
through the read service, so the pointer allowlist applies (for
``--scope project`` the pointer as seen from the project directory). The
app writes outside the runtime area only into the directory named on the
command line. Every refusal is found before anything is written. ``check``
compares what is installed with what this very process renders now (F6).
The ``graph_cli.skills`` modules are imported where they are used, so no
other command loads them. In the remote mode (B14.10) a graph skill comes
from the server, rendered there for the graph's current pin; this program
checks it against its pointer and marks it as its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Literal

from graph_cli import __version__
from graph_cli.app_cli.commands.common import PROGRAM, services
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, STRINGS
from graph_cli.catalog.pointer import PointerFile
from graph_cli.catalog.wiring import AppContext
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta

if TYPE_CHECKING:
    from graph_cli.catalog.remote import RemoteSession
    from graph_cli.skills.install import Installed, RenderedSkill, SkillTarget

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
PROJECT_GRAPHS: Final = ParamSpec(
    "--project-graphs",
    "flag",
    "Also the skill of every graph the project's pointer file names that has one.",
)
OUT: Final = ParamSpec(
    "--out", "directory", "Existing directory to write one directory per skill into.", required=True
)
GRAPH_ERRORS: Final = (
    "remote.unreachable",  # graph skills come from the server in the remote mode (B14.10)
    "remote.unauthorized",
    "remote.protocol",
    "remote.client_outdated",
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
    from graph_cli.skills import sources

    return sources.app_skill(app_inventory(), program_block())


def app_compat() -> str:
    """This program's compatibility id (B14.16): its inventory with answer schemas and
    skill 1 as it installs it."""
    from graph_cli.app_cli.commands import all_specs
    from graph_cli.app_cli.commands.meta import inventory
    from graph_cli.catalog.compat import compat_id

    skill = app_skill()
    return compat_id(inventory(all_specs(), output_schemas=True), {skill.name: skill.files})


def graph_skill(ctx: AppContext, entry: GraphEntry, warnings: list[WarningRecord]) -> RenderedSkill:
    """The published skill of ``entry``; ``skills.source_missing`` when there is none. A
    template (kind ``generated``) is rendered with the model of the graph's current pin."""
    from graph_cli.kernel.skill_marker import GENERATED
    from graph_cli.skills import sources

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
    model, inventory, names, digest, featured = None, None, None, None, None
    if slot.kind == GENERATED:
        model, model_warnings = ctx.reader.skill_model(ctx.actor, entry, request)
        warnings += model_warnings
        inventory = app_inventory()
        names = ctx.reader.skill_names_model(ctx.actor, entry, request)
        digest = ctx.reader.skill_digest(ctx.actor, entry)
        featured = ctx.reader.skill_featured(entry)
    skill, found = sources.graph_skill(
        entry,
        slot,
        program_block(),
        model,
        inventory,
        names,
        digest,
        featured,
    )
    warnings += found
    return skill


class SkillSource:
    """Where graph skills come from: this installation, or the server (remote mode)."""

    def __init__(self, state: CommandState) -> None:
        self.state = state
        app = services(state)
        self.remote = app.remote(state.warnings) if app.remote_mode() else None
        self._context: AppContext | None = None

    @property
    def context(self) -> AppContext:
        if self._context is None:
            self._context = services(self.state).context(self.state.warnings, self.state.command)
        return self._context

    @property
    def pointer(self) -> PointerFile | None:
        """The invoking project's pointer."""
        if self.remote is None:
            return self.context.pointer
        return services(self.state).pointer_at(services(self.state).cwd)[0]

    def skills(self, refs: Sequence[str], pointer: PointerFile | None) -> list[RenderedSkill]:
        """The skills of ``refs``, each graph once, selected like ``--graph`` as a project
        with ``pointer`` would see it."""
        if self.remote is None:
            entries: dict[str, GraphEntry] = {}
            for ref in refs:
                entry = self.context.select_for(ref, pointer)
                entries.setdefault(str(entry.id), entry)
            return [graph_skill(self.context, e, self.state.warnings) for e in entries.values()]
        found: dict[str, RenderedSkill] = {}
        for ref in refs:
            skill = remote_graph_skill(self.remote, ref, pointer, self.state.warnings)
            found.setdefault(skill.name, skill)
        return list(found.values())


def remote_graph_skill(
    session: RemoteSession, ref: str, pointer: PointerFile | None, warnings: list[WarningRecord]
) -> RenderedSkill:
    """A graph skill rendered by the server, checked against this project's pointer."""
    import base64
    from urllib.parse import quote

    from graph_cli.catalog.remote import envelope_of, error_of, protocol
    from graph_cli.skills.install import rendered

    answer = session.client.call(
        "POST", f"/v1/graphs/{quote(ref, safe='')}/skill", body=session.body()
    )
    document = envelope_of(answer)
    found = document.get("warnings")
    for warning in found if isinstance(found, list) else []:
        if isinstance(warning, dict):
            record = WarningRecord(
                str(warning.get("code")), str(warning.get("message")), warning.get("details") or {}
            )
            if record not in warnings:
                warnings.append(record)
    if document.get("ok") is not True:
        raise error_of(document)
    data = document.get("data")
    try:
        assert isinstance(data, dict)
        graph, skill = data["graph"], data["skill"]
        assert isinstance(graph, dict) and isinstance(skill, dict)
        files = skill["files"]
        assert isinstance(files, dict)
        contents = {str(p): base64.b64decode(str(c)) for p, c in files.items()}
        name, kind, source = str(skill["name"]), skill["kind"], skill["source"]
        assert kind in ("app", "graph") and isinstance(source, dict)
    except (AssertionError, KeyError, ValueError):
        raise protocol(answer.status, "the answer is not a graph skill") from None
    if pointer is not None and not (
        graph["name"] in pointer.graphs or graph["id"] in pointer.graphs
    ):
        raise GraphCliError(
            "graph.not_in_project",
            f"graph {graph['name']} is not allowed by this project's pointer file",
            details={
                "graph": graph["name"],
                "allowed": list(pointer.graphs),
                "file": str(pointer.path),
            },
        )
    return rendered(name, kind, contents, program=program_block(), source=source)


def named_graph_skills(
    source: SkillSource, refs: Sequence[str], warnings: list[WarningRecord]
) -> list[RenderedSkill]:
    """The skills of the graphs named with ``--graph``, selected like any ``--graph``."""
    return source.skills(refs, source.pointer)


def emit(state: CommandState, out: str, graphs: tuple[str, ...], force: bool) -> Result:
    from graph_cli.skills.install import emit_skills

    target = services(state).path(out)
    if not target.is_dir():
        raise GraphCliError(
            "usage.invalid_argument",
            f"--out {out}: no such directory; create it first",
            details={"argument": "out"},
        )
    skills = [app_skill()]
    if graphs:
        skills += named_graph_skills(SkillSource(state), graphs, state.warnings)
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
    from graph_cli.skills.install import SkillTarget, require_target

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


def pointer_of(state: CommandState, source: SkillSource, target: SkillTarget) -> PointerFile | None:
    """Q3 visibility: a project gets the skills of the graphs it may use."""
    if target.dir is None:
        return source.pointer
    pointer, warnings = services(state).pointer_at(target.dir)
    state.warnings += warnings
    return pointer


def installed_graph_ref(found: Installed) -> str | None:
    """Which graph a marked graph skill belongs to: the marker's id, else (legacy marker)
    the name ``graph-<graph>``."""
    from graph_cli.kernel.skill_marker import GRAPH_SKILL_PREFIX

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
    skipped: list[dict[str, JsonValue]] | None = None,
) -> tuple[list[RenderedSkill], dict[str, dict[str, JsonValue]]]:
    """Skill 1, the named graph skills and the graph skills already installed by us; with
    ``skipped`` also those of the pointer's graphs.

    A named graph that cannot be installed is an error; an installed one becomes
    ``unavailable`` (``{skill: {graph, reason}}``); a pointer's graph without a skill or
    out of reach goes to ``skipped`` (``{graph, reason}``).
    """
    wanted = [app_skill()]
    unavailable: dict[str, dict[str, JsonValue]] = {}
    refresh = {
        name: ref
        for name, found in installed.items()
        if found.kind == "graph" and (ref := installed_graph_ref(found)) is not None
    }
    if not graphs and not refresh and skipped is None:
        return wanted, unavailable
    source = SkillSource(state)
    pointer = pointer_of(state, source, target)
    named = source.skills(graphs, pointer)
    wanted += named
    for name, ref in sorted(refresh.items()):
        if name in {skill.name for skill in wanted}:
            continue
        try:
            wanted += source.skills([ref], pointer)
        except GraphCliError as exc:
            if exc.code not in UNAVAILABLE:
                raise
            unavailable[name] = {"graph": ref, "reason": exc.code}
    if skipped is not None and pointer is not None:
        for ref in pointer.graphs:
            try:
                found = source.skills([ref], pointer)
            except GraphCliError as exc:
                if exc.code not in UNAVAILABLE:
                    raise
                skipped.append({"graph": ref, "reason": exc.code})
                continue
            wanted += [s for s in found if s.name not in {w.name for w in wanted}]
    return wanted, unavailable


def install(
    state: CommandState,
    scope: str,
    dir: str | None,
    graphs: tuple[str, ...],
    force: bool,
    dry_run: bool,
    project_graphs: bool,
) -> Result:
    from graph_cli.skills.install import apply_install, plan_install, refusal, scan

    target = target_of(state, scope, dir)
    installed = scan(target)
    skipped: list[dict[str, JsonValue]] | None = [] if project_graphs else None
    wanted, unavailable = wanted_skills(state, target, installed, graphs, skipped)
    plan = plan_install(target, installed, wanted, force=force)
    error = refusal(plan, {"graphs": list(graphs)})
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
            "skipped_graphs": skipped or [],
        },
        error=error,
    )


# ---- check ---------------------------------------------------------------------------


def check_command(state: CommandState, scope: str, dir: str | None) -> Result:
    from graph_cli.skills.install import STATUSES, check, scan

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


_KIND: Final = enum("app", "graph")
SKILL_STATUS: Final = enum(  # skills.install.STATUSES, which loads only when it is needed
    "fresh", "stale", "modified", "unexpected", "foreign", "missing", "unavailable"
)
EMIT_OUTPUT: Final = obj(
    {
        "out": STRING,
        "program": ref("program"),
        "skills": array(
            obj(
                {
                    "skill": STRING,
                    "kind": _KIND,
                    "action": enum("create", "replace"),
                    "files": STRINGS,
                }
            )
        ),
    }
)
INSTALL_OUTPUT: Final = obj(
    {
        "target": ref("skill_target"),
        "dry_run": BOOLEAN,
        "program": ref("program"),
        "skills": array(ref("skill_step")),
        "unavailable": array(ref("skill_unavailable")),
        "skipped_graphs": array(ref("graph_reason")),
    }
)
CHECK_OUTPUT: Final = obj(
    {
        "target": ref("skill_target"),
        "program": obj({"name": STRING, "version": STRING, "executable": NULLABLE_STRING}),
        "skills": array(
            obj(  # skills.install.SkillStatus.to_json; unavailable ones say why
                {"skill": STRING, "kind": nullable(_KIND), "status": SKILL_STATUS},
                {"graph": STRING, "reason": STRING},
            )
        ),
        "ok_count": INTEGER,
        "counts": ref("skill_counts"),
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
        output=EMIT_OUTPUT,
        remote="client",
        description="One directory per skill, without an install marker. Install skills"
        " for an agent with `graph skills install` instead.",
    ),
    CommandSpec(
        path=("skills", "install"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Install or refresh the agent skills of this program and its graphs.",
        params=(SCOPE, DIR, GRAPHS, PROJECT_GRAPHS, FORCE, DRY_RUN),
        examples=(
            Example("graph skills install --scope project --dir <dir>", "Into one project."),
            Example(
                "graph skills install --scope user --graph <graph> --json",
                "For every project of the user, with one graph skill.",
            ),
            Example("graph skills install --scope project --dry-run --json", "Show the plan."),
            Example(
                "graph skills install --scope project --dir <dir> --project-graphs",
                "Skill 1 and the skills of every graph the project uses.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "usage.missing_argument",
            "skills.not_owned",
            "skills.modified",
            "skills.target_invalid",
        ),
        handler=install,
        output=INSTALL_OUTPUT,
        remote="client",
        description="--scope user installs into $CLAUDE_CONFIG_DIR/skills, else"
        " ~/.claude/skills; --scope project into <dir>/.claude/skills (<dir>/.claude must"
        " exist). Skill 1 is always installed, graph skills named with --graph, and every"
        " graph skill installed here before is refreshed. --project-graphs adds the graphs"
        " of the pointer file (seen from <dir> for --scope project): a graph without a"
        " published skill or out of reach is listed in skipped_graphs, not refused."
        " Directories without our marker (.graph_cli_skill.json) or edited by hand are"
        " replaced only with --force; without it they are left as they are, the other"
        " skills are still installed, and the answer lists every skill and ends with the"
        " refusal (exit 1).",
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
            "remote.unreachable",
            "remote.unauthorized",
            "remote.protocol",
            "remote.client_outdated",
        ),
        handler=check_command,
        output=CHECK_OUTPUT,
        remote="client",
        description="Statuses: fresh, stale (installed unchanged, but the program, the"
        " published graph skill or, for a generated one, the graph's ontology changed since),"
        " modified (edited by hand), unexpected (extra"
        " files), foreign (not installed by this program), missing, unavailable (the graph"
        " is gone, not visible here or has no published skill). Exit 0 only when every"
        " skill is fresh. Only reads.",
    ),
)
