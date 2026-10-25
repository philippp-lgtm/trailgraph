"""``graph-admin describe commands``: the maintenance inventory (contracts B11.2)."""

from __future__ import annotations

from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.output_schemas import ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli_admin import __version__
from graph_cli_admin.maintenance_cli.output_schemas import DEFS

PROGRAM: Final = "graph-admin"


def describe_commands(state: CommandState, output_schemas: bool) -> Result:
    from graph_cli_admin.maintenance_cli.commands import all_specs

    return Result(data=inventory(all_specs(), output_schemas=output_schemas))


# What every command the admin API runs can meet in the remote mode (B14.15).
REMOTE_ERRORS: Final = (
    "remote.unreachable",
    "remote.unauthorized",
    "remote.protocol",
    "remote.client_outdated",
)


def inventory(
    specs: tuple[CommandSpec, ...], *, output_schemas: bool = False
) -> dict[str, JsonValue]:
    """The maintenance inventory; ``output_schemas`` adds the schemas of the answers
    (B11.10)."""
    return build_inventory(
        specs,
        program=PROGRAM,
        version=__version__,
        surface="maint",
        remote=True,
        remote_errors=REMOTE_ERRORS,
        output_defs=DEFS if output_schemas else None,
    )


SPECS: Final = (
    CommandSpec(
        path=("describe", "commands"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Describe every command of this program (graph_cli.inventory/1).",
        params=(
            ParamSpec(
                "--output-schemas",
                "flag",
                "Add the JSON Schema of every command's data and their shared $defs.",
            ),
        ),
        examples=(
            Example("graph-admin describe commands --json", "The command inventory."),
            Example("graph-admin describe commands", "The inventory as text."),
            Example(
                "graph-admin describe commands --output-schemas --json",
                "The inventory with the schema of every answer.",
            ),
        ),
        errors=(),
        handler=describe_commands,
        remote="client",
        output=ref("inventory"),
    ),
)
