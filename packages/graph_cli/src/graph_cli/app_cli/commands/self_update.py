"""``graph self update``: catch up with the origin (contracts B14.18).

In the remote mode the program is compared with the server's build first
and, when they differ, replaced by it (``catalog.dist``). Then the
installed program, run as a new process, refreshes the agent skills in
every scope that has a skill of this program: the project's (with the
skills of the graphs its pointer names that are not installed yet) and the
user's; and checks them. After a new program is installed this process
runs no code of it and imports nothing more: everything the answer needs
is imported before. ``--check`` only reads: the program's build and a dry
run of the skill refresh, exit 0 only when nothing is outdated.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import graph_cli
from graph_cli.app_cli.commands.common import PROGRAM, services
from graph_cli.catalog.dist import Installer, program_step, run_program
from graph_cli.catalog.remote import error_of
from graph_cli.catalog.wiring import AppServices
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, array, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

CHECK: Final = ParamSpec(
    "--check", "flag", "Only report what is outdated; change nothing. Exit 0 when nothing is."
)
DIR: Final = ParamSpec(
    "--dir", "directory", "Project directory whose skills to refresh; default: the working one."
)
FORCE: Final = ParamSpec("--force", "flag", "Also replace skills that were edited by hand.")
CHANGED: Final = frozenset({"create", "replace"})  # skill steps that change a directory


def skill_scopes(app: AppServices, directory: Path) -> list[tuple[str, Path | None]]:
    """``(scope, project directory)`` of every scope that has a skill of this program: the
    project at the pointer's directory (else ``directory``), and the user's."""
    from graph_cli.skills.install import SkillTarget, scan

    pointer = app.pointer_at(directory)[0]
    project = pointer.path.parent if pointer is not None else directory
    found: list[tuple[str, Path | None]] = []
    for scope, target in (("project", project), ("user", None)):
        try:
            config_dir, project_dir = app.skill_dirs(scope, str(target) if target else None)
        except GraphCliError:
            continue  # no HOME: no user scope
        installed = scan(SkillTarget("project" if target else "user", config_dir, project_dir))
        if any(item.marker_data is not None or item.legacy for item in installed.values()):
            found.append((scope, project_dir))
    return found


def program_of(app: AppServices) -> list[str]:
    """How to run the installed program: its launcher, else this interpreter's module."""
    launcher = Path(app.executable) if app.executable else None
    if launcher is not None and launcher.name == PROGRAM and launcher.exists():
        return [str(launcher)]
    return [sys.executable, "-m", "graph_cli"]


def refresh(
    app: AppServices,
    scope: str,
    project: Path | None,
    *,
    check: bool,
    force: bool,
    command: list[str] | None = None,
) -> tuple[dict[str, JsonValue], GraphCliError | None]:
    """One scope through the installed program (``command``, default ``program_of``):
    ``skills install`` (a dry run for ``check``), then ``skills check``; the first error
    either answers."""
    words = ["skills", "install", "--scope", scope]
    if project is not None:
        words += ["--dir", str(project), "--project-graphs"]
    if force:
        words.append("--force")
    if check:
        words.append("--dry-run")
    command = command or program_of(app)
    installed, _ = run_program([*command, *words], app.env, app.cwd)
    item: dict[str, JsonValue] = {"scope": scope, "dir": str(project) if project else None}
    data = installed.get("data")
    if isinstance(data, Mapping):
        for key in ("skills", "unavailable", "skipped_graphs"):
            item[key] = data.get(key, [])
    error = error_of(installed) if installed.get("ok") is not True else None
    if check or (error is not None and not isinstance(data, Mapping)):
        return item, error
    target = ["--scope", scope, *(["--dir", str(project)] if project is not None else [])]
    checked, _ = run_program([*command, "skills", "check", *target], app.env, app.cwd)
    counts = checked.get("data")
    item["counts"] = counts.get("counts") if isinstance(counts, Mapping) else None
    if error is None and checked.get("ok") is not True:
        error = error_of(checked)
    return item, error


def changed(item: Mapping[str, JsonValue]) -> bool:
    steps = item.get("skills")
    return isinstance(steps, list) and any(
        isinstance(s, Mapping) and s.get("action") in CHANGED for s in steps
    )


def update(state: CommandState, check: bool, dir: str | None, force: bool) -> Result:
    app = services(state)
    directory = app.path(dir) if dir is not None else app.cwd
    scopes = skill_scopes(app, directory)
    program: dict[str, JsonValue] = {"name": PROGRAM, "action": "skipped", "reason": "local"}
    if app.remote_mode():
        session = app.remote(state.warnings)
        _, roots, _ = app.remote_roots()
        program = program_step(
            session.client,
            PROGRAM,
            package_init=graph_cli.__file__,
            state_root=roots.state_root,
            installer=Installer(app.env),
            check=check,
        )
        if program["action"] != "skipped":  # the step says it; the warning is stale
            state.warnings[:] = [w for w in state.warnings if w.code != "remote.version_differs"]
    skills: list[dict[str, JsonValue]] = []
    errors: list[GraphCliError] = []
    if not (check and program["action"] == "outdated"):  # else judged by the old program
        for scope, project in scopes:
            item, error = refresh(app, scope, project, check=check, force=force)
            skills.append(item)
            errors += [error] if error is not None else []
    outdated = program["action"] in ("outdated", "updated") or any(changed(s) for s in skills)
    data: dict[str, JsonValue] = {
        "check": check,
        "program": program,
        "skills": skills,
        "changed": outdated if not check else False,
    }
    if check and outdated and not errors:
        errors.append(
            GraphCliError(
                "update.available",
                f"the server runs another build of {PROGRAM}; {PROGRAM} self update installs it"
                if program["action"] == "outdated"
                else f"installed skills are outdated or missing; {PROGRAM} self update"
                " refreshes them",
                details={"dir": str(directory)} if dir is not None else {},
            )
        )
    return Result(data=data, error=errors[0] if errors else None)


OUTPUT: Final = obj(
    {
        "check": BOOLEAN,
        "program": ref("program_step"),
        "skills": array(ref("skill_refresh")),
        "changed": BOOLEAN,
    }
)

SPECS: Final = (
    CommandSpec(
        path=("self", "update"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Install the server's build of this program and refresh the agent skills.",
        params=(CHECK, DIR, FORCE),
        examples=(
            Example("graph self update", "Catch up with the server: program and skills."),
            Example("graph self update --check --json", "Only report what is outdated."),
        ),
        errors=(
            "config.invalid",
            "config.forbidden_key",
            "remote.unreachable",
            "remote.unauthorized",
            "remote.protocol",
            "update.available",
            "update.unavailable",
            "update.unsupported_install",
            "update.install_failed",
            "skills.modified",
            "skills.not_owned",
            "skills.check_failed",
        ),
        handler=update,
        output=OUTPUT,
        remote="client",
        description="In the remote mode the program is replaced by the server's build when"
        " the builds differ (a program installed with uv tool only; a checkout is left as"
        " it is). Then the installed program refreshes the skills in every scope that has a"
        " skill of this program: the project's (at the pointer file found from --dir), with"
        " the skills of the pointer's graphs that are not installed yet, and the user's."
        " Skills edited by hand stay unless --force. Locally only the skills are refreshed.",
    ),
)
