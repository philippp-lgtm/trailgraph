"""Skill 1's freshness, judged by the program that runs the command (contracts B14.17).

``graph-cli`` is rendered from this program's inventory, so this program
judges an installed copy, locally and in the remote mode alike: a copy
rendered from another inventory refuses a write (``skills.outdated``) and
warns on a read (``skills.stale``) before the command runs. Meta commands
(``describe commands``, ``skills ...``, ``whoami``) are never checked: they
are how a stale skill is found and refreshed. The server leaves skill 1 to
its client; graph skills are judged where the graph lives
(``catalog.skill_claims``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from graph_cli.app_cli.commands.common import services
from graph_cli.catalog.skill_claims import app_stale, report
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec


def guarded(specs: Sequence[CommandSpec]) -> tuple[CommandSpec, ...]:
    """The declarations with every read and write checking skill 1 first."""
    return tuple(
        spec if spec.kind == "meta" else replace(spec, handler=_guard(spec)) for spec in specs
    )


def _guard(spec: CommandSpec) -> Callable[..., Result]:
    handler = spec.handler

    def guard(state: CommandState, **values: Any) -> Result:
        check_app_skill(state, write=spec.kind == "write")
        return handler(state, **values)

    return guard


def check_app_skill(state: CommandState, *, write: bool) -> None:
    app = services(state)
    if app.request is not None:
        return  # on the server: the client judged its own skill
    claims, project = app.skill_claims()
    apps = [c for c in claims if c.graph is None]
    if not apps:
        return
    from graph_cli.app_cli.commands.skills import app_inventory  # only with a skill installed
    from graph_cli.skills.app_skill import inventory_hash

    stale = app_stale(apps, inventory_hash(app_inventory()))
    report(stale, write=write, warnings=state.warnings, project_dir=project)
