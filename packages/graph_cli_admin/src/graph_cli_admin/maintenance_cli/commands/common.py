"""Options and helpers shared by every graph-admin command."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.runner import CommandState
from graph_cli_admin.services_admin.context import AdminContext

data_root_option = click.option(
    "--data-root",
    type=click.Path(path_type=Path, file_okay=False),
    help="Data root to use instead of GRAPH_CLI_ROOT, config or the XDG default.",
)


def admin_context(state: CommandState, data_root: Path | None) -> AdminContext:
    context: AdminContext = state.services.context(data_root=data_root, warnings=state.warnings)
    return context
