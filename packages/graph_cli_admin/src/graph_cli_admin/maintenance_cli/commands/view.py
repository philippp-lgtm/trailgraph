"""``graph-admin view``: a local live view of the graphs of a data root (graph view plan)."""

from __future__ import annotations

import contextlib
import dataclasses
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Final

import click

from graph_cli.catalog.activity import ActivityListener, activity_dir
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, STRING, nullable, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.actor import Assurance, Facet, is_valid_asserted_value
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli_admin.maintenance_cli.commands.common import (
    DATA_ROOT,
    GRAPH_ERRORS,
    admin_context,
)
from graph_cli_admin.maintenance_cli.view_server import ViewServer
from graph_cli_admin.services_admin.view import ViewGraphs

MIN_TOKEN: Final = 32  # characters; a random token of 24 bytes has 32


def page() -> bytes:
    return files("graph_cli_admin.maintenance_cli").joinpath("static", "view.html").read_bytes()


def read_token_file(path: str) -> str:
    """The token of ``--token-file``, stripped; never named in an error."""

    def refused(why: str) -> GraphCliError:
        return GraphCliError(
            "usage.invalid_argument",
            f"--token-file {path}: {why}",
            details={"argument": "--token-file"},
        )

    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        raise refused("is not UTF-8") from None  # the error would quote the file's bytes
    except OSError as exc:
        raise refused(f"cannot be read: {exc.strerror or exc}") from None
    if not token:
        raise refused("is empty")
    if len(token) < MIN_TOKEN:
        raise refused(f"the token is shorter than {MIN_TOKEN} characters")
    return token


def view_command(
    state: CommandState,
    graph_ref: str | None,
    port: int,
    host: str,
    no_open: bool,
    token_file: str | None,
    project: str | None,
    data_root: str | None,
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
    token = read_token_file(token_file) if token_file is not None else None
    if project is not None and not is_valid_asserted_value(project):
        raise GraphCliError(
            "usage.invalid_argument",
            f"--project {project!r}: not a valid project name",
            details={"argument": "--project"},
        )
    ctx = admin_context(state, data_root)
    # It serves for long: a policy applied meanwhile decides the next request. An invalid
    # one refuses to start and every request, instead of falling back as graph-admin does.
    policy = FilePolicySource(ctx.roots.data_root, watch=True)
    policy.installation()
    ctx = dataclasses.replace(ctx, policy=policy)
    if project is not None:  # asserted like a pointer's project: the policy's scope for it applies
        facet = Facet(Assurance.ASSERTED, id=project, source="option")
        ctx = dataclasses.replace(ctx, actor=dataclasses.replace(ctx.actor, project=facet))
    graphs = ViewGraphs(ctx, default=graph_ref)
    first = graphs.get(None).source if graph_ref is not None else None
    if first is not None:
        state.warnings += first.warnings
    of = f" of {first.entry.name}" if first is not None else ""
    # A graph opened later warns now, once: the envelope's warnings print only at the stop.
    graphs.on_warning = lambda graph, warning: click.echo(
        f"Warning for {graph} ({warning.code}): {warning.message}", err=True
    )
    listener: ActivityListener | None = None
    no_reads = ""  # why the activity socket cannot be bound
    if ctx.config.signal_reads:
        directory = activity_dir(ctx.roots.state_root)
        try:
            listener = ActivityListener.open(directory)
        except OSError as exc:
            no_reads = f"{exc.strerror or exc} in {directory}"
            state.warnings.append(
                WarningRecord(
                    "admin.view_reads_unavailable",
                    f"reads cannot be shown: {exc.strerror or exc}",
                    {"directory": str(directory)},
                )
            )
    try:
        server = ViewServer(
            graphs,
            page=page(),
            port=port,
            host=host,
            listener=listener,
            signal_reads=ctx.config.signal_reads,
            token=token,
        )
    except OSError as exc:
        if listener is not None:
            listener.close()
        details: dict[str, JsonValue] = {"port": port}
        if first is not None:
            details["graph"] = first.entry.name
        raise GraphCliError(
            "admin.view_port_unavailable",
            f"cannot serve on {host}:{port}: {exc.strerror or exc}",
            details=details,
        ) from exc
    try:
        server.start()
        if token is None:
            click.echo(f"Graph view{of}: {server.url}", err=True)
        else:
            click.echo(
                f"Graph view{of}: {server.address} (add ?t= with the token of --token-file)",
                err=True,
            )
        if host != "127.0.0.1":
            click.echo(
                f"Bound to {host}: the page and its token travel in clear; publish the port"
                " to 127.0.0.1 only, or put a proxy in front.",
                err=True,
            )
        if no_reads:  # the warnings print only when the view stops
            click.echo(
                f"Reads are not shown: the activity socket cannot be bound ({no_reads}).", err=True
            )
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
        data={
            "graph_id": str(first.entry.id) if first is not None else None,
            "address": server.address,
            "stopped": True,
        },
        text=f"Graph view{of} stopped.",
    )


OUTPUT: Final = obj({"graph_id": nullable(STRING), "address": STRING, "stopped": BOOLEAN})


SPECS: Final = (
    CommandSpec(
        path=("view",),
        kind="read",
        surface="retrieve",
        action=None,
        summary="Show the graphs in the browser; reads, updates and new entities light up live.",
        params=(
            ParamSpec(
                "--graph",
                "graph_ref",
                "Graph to show first, name or id; without it the page opens on the list of graphs.",
                keyword="graph_ref",
            ),
            ParamSpec("--port", "integer", "Port to serve on; 0 picks a free one.", default=0),
            ParamSpec(
                "--host",
                "string",
                "Address to bind, for a container (0.0.0.0); the page still needs its token.",
                default="127.0.0.1",
            ),
            ParamSpec("--no-open", "flag", "Print the address only; open no browser."),
            ParamSpec(
                "--token-file",
                "file",
                "Read the page's token from this file (32 characters or more) instead of"
                " a random one per start; the printed address leaves it out.",
            ),
            ParamSpec(
                "--project",
                "string",
                "Show what this project may read: the view acts for it, so the installation"
                " policy's scope for the project applies instead of the one for no project.",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin view", "Open the live view; pick a graph in the page."),
            Example("graph-admin view --graph <graph>", "Open the live view on one graph."),
            Example(
                "graph-admin view --graph <graph> --port <value> --no-open",
                "Serve on a fixed port without opening a browser.",
            ),
            Example(
                "graph-admin view --host 0.0.0.0 --port <value> --no-open --token-file <file>",
                "Serve in a container behind a proxy that adds the token.",
            ),
            Example(
                "graph-admin view --project <value>",
                "Show the graphs as the policy's scope for one project allows.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "admin.view_port_unavailable"),
        handler=view_command,
        output=OUTPUT,
        description="Serves a read-only page on 127.0.0.1 (or --host) until Ctrl+C and prints"
        " its address on stderr; the address carries a token, keep it to yourself. There is"
        " no JSON mode. A side bar lists every graph you may read; the page opens on the"
        " graph of --graph, else on none. GET /health answers ok without the token, for"
        " probes."
        " Writes show up by themselves (green: new, blue: changed). Reads (yellow) show only"
        " when the app sends them: [activity] signal_reads = true in config.toml.",
        remote="local",
    ),
)
