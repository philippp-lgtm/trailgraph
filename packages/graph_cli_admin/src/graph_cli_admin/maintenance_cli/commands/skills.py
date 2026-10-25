"""``graph-admin skills install|check``: the admin skills for an agent.

The skills that ship with this program (``maintenance_cli.admin_skills``)
are copied into an agent skill directory with the installer of ``graph
skills install``: the same targets, marker, staged swap and statuses. Both
run here, in the remote mode too: the skills come from this program, not
from the server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, array, enum, nullable, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.maintenance_cli import admin_skills
from graph_cli_admin.maintenance_cli.wiring import Services

if TYPE_CHECKING:
    from graph_cli.skills.install import InstallPlan, SkillTarget

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
FORCE: Final = ParamSpec(
    "--force", "flag", "Replace skill directories that are not ours or were edited by hand."
)
DRY_RUN: Final = ParamSpec("--dry-run", "flag", "Show what would be installed; write nothing.")


def target_of(state: CommandState, scope: str, dir: str | None) -> SkillTarget:
    from graph_cli.skills.install import SkillTarget, require_target

    if scope == "user" and dir is not None:
        raise GraphCliError(
            "usage.invalid_argument",
            "--dir names a project; it does not go with --scope user",
            details={"argument": "dir"},
        )
    services = state.services
    assert isinstance(services, Services)
    chosen: Literal["user", "project"] = "user" if scope == "user" else "project"
    config_dir, project = services.skill_dirs(chosen, dir)
    target = SkillTarget(chosen, config_dir, project)
    require_target(target)
    return target


def plan(target: SkillTarget, *, force: bool) -> InstallPlan:
    from graph_cli.skills.install import plan_install, scan

    installed = scan(target, admin_skills.owner())
    return plan_install(target, installed, admin_skills.admin_skills(), force=force)


def install(state: CommandState, scope: str, dir: str | None, force: bool, dry_run: bool) -> Result:
    from graph_cli.skills.install import apply_install, refusal

    target = target_of(state, scope, dir)
    planned = plan(target, force=force)
    if not dry_run:
        apply_install(planned)
    return Result(
        data={
            "target": target.to_json(),
            "dry_run": dry_run,
            "program": admin_skills.program_block(),
            "skills": [step.to_json() for step in planned.steps],
        },
        error=refusal(planned),
    )


def check_command(state: CommandState, scope: str, dir: str | None) -> Result:
    from graph_cli.skills.install import STATUSES, check, scan

    target = target_of(state, scope, dir)
    installed = scan(target, admin_skills.owner())
    statuses = check(installed, admin_skills.admin_skills())
    counts = {status: sum(s.status == status for s in statuses) for status in STATUSES}
    data: dict[str, JsonValue] = {
        "target": target.to_json(),
        "program": admin_skills.program_block(),
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


TARGET: Final = obj({"scope": enum("user", "project"), "path": STRING}, {"dir": STRING})
PROGRAM_BLOCK: Final = obj({"name": STRING, "version": STRING})
STEP: Final = obj(  # skills.install.Step.to_json
    {
        "skill": STRING,
        "kind": enum("admin"),
        "action": enum("create", "replace", "unchanged", "refused"),
        "installed": nullable(enum("intact", "legacy", "modified", "unexpected", "foreign")),
    }
)
STATUS: Final = enum(
    "fresh", "stale", "modified", "unexpected", "foreign", "missing", "unavailable"
)
INSTALL_OUTPUT: Final = obj(
    {"target": TARGET, "dry_run": BOOLEAN, "program": PROGRAM_BLOCK, "skills": array(STEP)}
)
CHECK_OUTPUT: Final = obj(
    {
        "target": TARGET,
        "program": PROGRAM_BLOCK,
        "skills": array(  # skills.install.SkillStatus.to_json
            obj({"skill": STRING, "kind": nullable(enum("admin")), "status": STATUS})
        ),
        "ok_count": INTEGER,
        "counts": obj(extra=INTEGER),
    }
)
SKILL_ERRORS: Final = ("config.invalid", "usage.invalid_argument", "skills.target_invalid")

SPECS: Final = (
    CommandSpec(
        path=("skills", "install"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Install or refresh the admin skills of this program for an agent.",
        params=(SCOPE, DIR, FORCE, DRY_RUN),
        examples=(
            Example("graph-admin skills install --scope project --dir <dir>", "Into one folder."),
            Example("graph-admin skills install --scope user --json", "For every project."),
            Example(
                "graph-admin skills install --scope project --dry-run --json", "Show the plan."
            ),
        ),
        errors=(*SKILL_ERRORS, "skills.not_owned", "skills.modified"),
        handler=install,
        output=INSTALL_OUTPUT,
        remote="client",
        description="The skills for maintainers: ontology-best-practices (authoring an"
        " ontology), maintaining-graphs (graphs, workspaces, releases) and"
        " authoring-graph-skills (graph skill templates). --scope user installs into"
        " $CLAUDE_CONFIG_DIR/skills, else ~/.claude/skills; --scope project into"
        " <dir>/.claude/skills (<dir>/.claude must exist). Directories without our marker"
        " (.graph_cli_skill.json) or edited by hand are replaced only with --force; without"
        " it they are left as they are, the other skills are still installed, and the"
        " answer ends with the refusal (exit 1). The skills of graph are never touched.",
    ),
    CommandSpec(
        path=("skills", "check"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Check that installed admin skills equal what this program installs now.",
        params=(SCOPE, DIR),
        examples=(
            Example("graph-admin skills check --scope project --dir <dir>", "Check one folder."),
            Example("graph-admin skills check --scope user --json", "Per-skill status as JSON."),
        ),
        errors=(*SKILL_ERRORS, "skills.check_failed"),
        handler=check_command,
        output=CHECK_OUTPUT,
        remote="client",
        description="Statuses: fresh, stale (installed unchanged, but this program ships"
        " other files now), modified (edited by hand), unexpected (extra files), foreign"
        " (not installed by this program), missing, unavailable (a skill this program no"
        " longer ships). Exit 0 only when every skill is fresh."
        " Only reads.",
    ),
)
