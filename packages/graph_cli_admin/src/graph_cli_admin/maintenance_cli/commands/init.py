"""``graph-admin init``."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.services_admin.init import init


@click.command("init")
@click.option("--device-name", help="Human-readable name of this device (stored in the config).")
@data_root_option
@json_option
@leaf
def init_command(state: CommandState, device_name: str | None, data_root: Path | None) -> Result:
    """Create the data root and, if missing, the config file with a device id.

    \b
    Examples:
      graph-admin init
      graph-admin init --data-root /srv/graphs --device-name workstation --json
    """
    ctx = admin_context(state, data_root)
    return Result(data=init(ctx, device_name=device_name))
