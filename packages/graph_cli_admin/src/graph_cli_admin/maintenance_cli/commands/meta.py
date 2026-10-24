"""``graph-admin describe commands``: the maintenance inventory (contracts B11.2)."""

from __future__ import annotations

from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example
from graph_cli.kernel.canonical import JsonValue
from graph_cli_admin import __version__

PROGRAM: Final = "graph-admin"


def describe_commands(state: CommandState) -> Result:
    from graph_cli_admin.maintenance_cli.commands import all_specs

    return Result(data=inventory(all_specs()))


# What every command the admin API runs can meet in the remote mode (B14.15).
REMOTE_ERRORS: Final = (
    "remote.unreachable",
    "remote.unauthorized",
    "remote.protocol",
    "remote.client_outdated",
)


def inventory(specs: tuple[CommandSpec, ...]) -> dict[str, JsonValue]:
    return build_inventory(
        specs,
        program=PROGRAM,
        version=__version__,
        surface="maint",
        remote=True,
        remote_errors=REMOTE_ERRORS,
    )


SPECS: Final = (
    CommandSpec(
        path=("describe", "commands"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Describe every command of this program (graph_cli.inventory/1).",
        params=(),
        examples=(
            Example("graph-admin describe commands --json", "The command inventory."),
            Example("graph-admin describe commands", "The inventory as text."),
        ),
        errors=(),
        handler=describe_commands,
        remote="client",
    ),
)
