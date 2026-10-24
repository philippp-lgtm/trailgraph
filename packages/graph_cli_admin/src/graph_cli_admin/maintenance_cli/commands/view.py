"""``graph-admin view``: a local live view of one graph (graph view plan)."""

from __future__ import annotations

import contextlib
import webbrowser
from importlib.resources import files
from typing import Final

import click

from graph_cli.catalog.activity import ActivityListener
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli_admin.maintenance_cli.commands.common import (
    DATA_ROOT,
    GRAPH_ERRORS,
    GRAPH_OPTION,
    admin_context,
)
from graph_cli_admin.maintenance_cli.view_server import ViewServer
from graph_cli_admin.services_admin import view


def page() -> bytes:
    return files("graph_cli_admin.maintenance_cli").joinpath("static", "view.html").read_bytes()


def view_command(
    state: CommandState, graph_ref: str, port: int, no_open: bool, data_root: str | None
) -> Result:
    if click.get_current_context().params.get("json_mode") or state.invocation.json_default:
        raise GraphCliError(  # B9.1: the address goes to stderr, which JSON mode keeps empty
            "usage.invalid_argument",
            "graph-admin view runs until Ctrl+C and has no JSON mode; "
            "run it without --json (GRAPH_CLI_OUTPUT=text)",
            details={"argument": "--json"},
        )
    if not 0 <= port <= 65535:
        raise GraphCliError(
            "usage.invalid_argument",
            f"--port {port}: not a port (0 to 65535)",
            details={"argument": "--port", "actual": port},
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
        if ctx.config.signal_reads and listener is None:
            click.echo("Reads are not shown: the activity socket cannot be bound.", err=True)
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


SPECS: Final = (
    CommandSpec(
        path=("view",),
        kind="read",
        surface="retrieve",
        action=None,
        summary="Show a graph in the browser; reads, updates and new entities light up live.",
        params=(
            GRAPH_OPTION,
            ParamSpec("--port", "integer", "Port on 127.0.0.1; 0 picks a free one.", default=0),
            ParamSpec("--no-open", "flag", "Print the address only; open no browser."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin view --graph <graph>", "Open the live view."),
            Example(
                "graph-admin view --graph <graph> --port <value> --no-open",
                "Serve on a fixed port without opening a browser.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "admin.view_port_unavailable"),
        handler=view_command,
        description="Serves a read-only page on 127.0.0.1 until Ctrl+C and prints its address"
        " on stderr; the address carries a token, keep it to yourself. There is no JSON mode."
        " Writes show up by themselves (green: new, blue: changed). Reads (yellow) show only"
        " when the app sends them: [activity] signal_reads = true in config.toml.",
    ),
)
