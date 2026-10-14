"""``graph-admin audit verify|anchor``."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.services_admin import audit


@click.group("audit")
def audit_group() -> None:
    """Verify and anchor the audit trail."""


@audit_group.command("verify")
@data_root_option
@json_option
@leaf
def verify(state: CommandState, data_root: Path | None) -> Result:
    """Check every audit chain, tx log, revision file and manifest pin.

    Exits 1 (admin.integrity_errors) if any error finding exists; warnings
    and info findings (such as interrupted writes) do not fail.

    \b
    Examples:
      graph-admin audit verify
      graph-admin audit verify --json
    """
    ctx = admin_context(state, data_root)
    verification = audit.verify(ctx)
    state.warnings += verification.warnings
    verification.raise_on_errors()
    return Result(data=verification.data)


@audit_group.command("anchor")
@data_root_option
@json_option
@leaf
def anchor(state: CommandState, data_root: Path | None) -> Result:
    """Record the current head of every graph stream in the installation stream.

    \b
    Examples:
      graph-admin audit anchor
      graph-admin audit anchor --json
    """
    return Result(data=audit.anchor(admin_context(state, data_root)))
