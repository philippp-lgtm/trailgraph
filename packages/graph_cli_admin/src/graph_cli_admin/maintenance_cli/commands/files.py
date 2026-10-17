"""``graph-admin files verify|gc``: the stored files of a graph."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.services_admin import files, graphs

graph_option = click.option("--graph", "graph_ref", required=True, help="Graph name or id.")


@click.group("files")
def files_group() -> None:
    """Verify and clean up the stored files of a graph."""


@files_group.command("verify")
@graph_option
@click.option("--rehash", is_flag=True, help="Read every stored file and compare its hash.")
@data_root_option
@json_option
@leaf
def verify(state: CommandState, graph_ref: str, rehash: bool, data_root: Path | None) -> Result:
    """Check that every file a tx names is stored; list orphans and staging leftovers.

    Exits 1 (admin.integrity_errors) on a missing or, with --rehash, damaged
    file; orphans and staging leftovers are info findings (files gc removes
    them).

    \b
    Examples:
      graph-admin files verify --graph example_graph
      graph-admin files verify --graph example_graph --rehash --json
    """
    outcome = files.verify(admin_context(state, data_root), graph_ref=graph_ref, rehash=rehash)
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


@files_group.command("gc")
@graph_option
@click.option("--reason", help="Why the files are removed; recorded in the audit event.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing it.")
@data_root_option
@json_option
@leaf
def gc(
    state: CommandState, graph_ref: str, reason: str | None, dry_run: bool, data_root: Path | None
) -> Result:
    """Remove stored files no tx names and staging files older than one hour.

    Files that any revision ever named stay, history included. The removal
    is audited (maint.repair); nothing to remove writes nothing.

    \b
    Examples:
      graph-admin files gc --graph example_graph --dry-run
      graph-admin files gc --graph example_graph --reason "after an interrupted add" --json
    """
    outcome = files.gc(
        admin_context(state, data_root), graph_ref=graph_ref, reason=reason, dry_run=dry_run
    )
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))
