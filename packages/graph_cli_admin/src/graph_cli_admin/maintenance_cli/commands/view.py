"""``graph-admin view``: a local live view of one graph (graph view plan)."""

from __future__ import annotations

import contextlib
import webbrowser
from importlib.resources import files
from pathlib import Path

import click

from graph_cli.catalog.activity import ActivityListener
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.maintenance_cli.view_server import ViewServer
from graph_cli_admin.services_admin import view


def page() -> bytes:
    return files("graph_cli_admin.maintenance_cli").joinpath("static", "view.html").read_bytes()


@click.command("view")
@click.option("--graph", "graph_ref", required=True, help="Graph name or id.")
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=0,
    show_default=True,
    help="Port on 127.0.0.1; 0 picks a free one.",
)
@click.option("--no-open", is_flag=True, help="Print the address only; open no browser.")
@data_root_option
@json_option
@leaf
def view_command(
    state: CommandState, graph_ref: str, port: int, no_open: bool, data_root: Path | None
) -> Result:
    """Show a graph in the browser; reads, updates and new entities light up live.

    Serves a read-only page on 127.0.0.1 until Ctrl+C and prints its
    address on stderr; the address carries a token, keep it to yourself.
    There is no JSON mode. Writes show up by themselves (green: new,
    blue: changed). Reads (yellow) show only when the app sends them:
    [activity] signal_reads = true in config.toml.

    \b
    Examples:
      graph-admin view --graph example_graph
      graph-admin view --graph example_graph --port 8765 --no-open
    """
    if click.get_current_context().params.get("json_mode") or state.invocation.json_default:
        raise GraphCliError(  # B9.1: the address goes to stderr, which JSON mode keeps empty
            "usage.invalid_argument",
            "graph-admin view runs until Ctrl+C and has no JSON mode; "
            "run it without --json (GRAPH_CLI_OUTPUT=text)",
            details={"argument": "--json"},
        )
    ctx = admin_context(state, data_root)
    source = view.open_view(ctx, graph_ref=graph_ref)
    state.warnings += source.warnings
    listener: ActivityListener | None = None
    if ctx.config.signal_reads:
        try:
            listener = ActivityListener.open(source.activity_dir)
        except OSError as exc:
            state.warnings.append(
                WarningRecord(
                    "admin.view_reads_unavailable",
                    f"reads cannot be shown: {exc.strerror or exc}",
                    {"directory": str(source.activity_dir)},
                )
            )
    try:
        server = ViewServer(
            source, page=page(), port=port, listener=listener, signal_reads=ctx.config.signal_reads
        )
    except OSError as exc:
        if listener is not None:
            listener.close()
        raise GraphCliError(
            "admin.view_port_unavailable",
            f"cannot serve on 127.0.0.1:{port}: {exc.strerror or exc}",
            details={"graph": source.entry.name, "port": port},
        ) from exc
    try:
        server.start()
        click.echo(f"Graph view of {source.entry.name}: {server.url}", err=True)
        click.echo("Press Ctrl+C to stop.", err=True)
        if not no_open:
            webbrowser.open(server.url)
        with contextlib.suppress(KeyboardInterrupt):
            server.wait()
    finally:
        server.stop()
        if listener is not None:
            listener.close()
    return Result(
        data={"graph_id": str(source.entry.id), "address": server.address, "stopped": True},
        text=f"Graph view of {source.entry.name} stopped.",
    )
