"""``graph-admin self update``: install the server's build of graph-admin (contracts B14.18).

Only in the remote mode, where the admin API is the origin: when the
server's build of ``graph-admin`` differs from this one, its wheel (with
the ``graph`` wheel it needs) is fetched with the admin token and
installed into the uv tool environment this program runs from
(``catalog.dist``). graph-admin installs no skills. Where the data lives
(``mode = "local"``) there is no origin to follow, so nothing is done.
"""

from __future__ import annotations

from typing import Final

import graph_cli_admin
from graph_cli.catalog.dist import Installer, program_step
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.maintenance_cli.commands.meta import PROGRAM
from graph_cli_admin.maintenance_cli.wiring import Services

CHECK: Final = ParamSpec(
    "--check", "flag", "Only report whether the server runs another build; change nothing."
)


def update(state: CommandState, check: bool) -> Result:
    services = state.services
    assert isinstance(services, Services)
    program: dict[str, JsonValue] = {"name": PROGRAM, "action": "skipped", "reason": "local"}
    if services.remote_mode():
        session = services.remote(state.warnings)
        program = program_step(
            session.client,
            PROGRAM,
            package_init=graph_cli_admin.__file__,
            state_root=services.state_root(),
            installer=Installer(services.env),
            check=check,
        )
        if program["action"] != "skipped":  # the step says it; the warning is stale
            state.warnings[:] = [w for w in state.warnings if w.code != "remote.version_differs"]
    error = None
    if check and program["action"] == "outdated":
        error = GraphCliError(
            "update.available",
            f"the server runs another build of {PROGRAM}; {PROGRAM} self update installs it",
            details={"program": PROGRAM},
        )
    data: dict[str, JsonValue] = {
        "check": check,
        "program": program,
        "changed": program["action"] == "updated",
    }
    return Result(data=data, error=error)


OUTPUT: Final = obj({"check": BOOLEAN, "program": ref("program_step"), "changed": BOOLEAN})


SPECS: Final = (
    CommandSpec(
        path=("self", "update"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Install the server's build of graph-admin (remote mode).",
        params=(CHECK,),
        examples=(
            Example("graph-admin self update", "Install the build the server runs."),
            Example("graph-admin self update --check --json", "Only report it."),
        ),
        errors=(
            "config.invalid",
            "remote.unreachable",
            "remote.unauthorized",
            "remote.protocol",
            "update.available",
            "update.unavailable",
            "update.unsupported_install",
            "update.install_failed",
        ),
        handler=update,
        output=OUTPUT,
        remote="client",
        description='Only with mode = "remote": the wheel comes from the server\'s'
        " /v1/dist/ with the admin token and replaces the uv tool environment graph-admin"
        " runs from (a checkout is left as it is). Where the data lives nothing is done.",
    ),
)
