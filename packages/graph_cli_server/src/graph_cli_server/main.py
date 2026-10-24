"""Entry point of the ``graph-server`` console script (contracts B14.9).

``graph-server serve`` reads its configuration like ``graph`` (the process
environment through ``catalog.wiring``), then serves the WSGI app with
``waitress``: one process, a thread pool. It binds ``127.0.0.1`` unless
``--host`` names another address, which needs ``--allow-plain-http``: TLS
belongs to a reverse proxy. The address it listens on is printed to stderr,
then one line per request.
"""

from __future__ import annotations

import argparse
import ipaddress
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Final, NoReturn

from graph_cli.catalog import wiring
from graph_cli.cli_kit.exit_codes import exit_code_for
from graph_cli.kernel.errors import GraphCliError
from graph_cli_server import __version__
from graph_cli_server.app import SERVER_PROGRAM, ServerApp, Settings

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8470
DEFAULT_THREADS: Final = 8
CHANNEL_TIMEOUT_S: Final = 120


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog=SERVER_PROGRAM, description="Serve the graph command surface over HTTP."
    )
    root.add_argument("--version", action="version", version=f"{SERVER_PROGRAM} {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the API until stopped.")
    serve.add_argument("--host", default=DEFAULT_HOST, help="Address to bind; default 127.0.0.1.")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port; 0 picks a free one.")
    serve.add_argument(
        "--allow-plain-http",
        action="store_true",
        help="Bind an address other than loopback; TLS is then a reverse proxy's job.",
    )
    serve.add_argument(
        "--threads", type=int, default=DEFAULT_THREADS, help="Requests served at once."
    )
    return root


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    options = parser().parse_args(argv)
    if not is_loopback(options.host) and not options.allow_plain_http:
        _fail(
            f"--host {options.host} is not a loopback address; the API speaks plain HTTP."
            " Put a TLS proxy in front and add --allow-plain-http",
            2,
        )
    if not 0 <= options.port <= 65535 or options.threads < 1:
        _fail("--port must be 0 to 65535 and --threads at least 1", 2)
    try:
        settings = Settings.load(wiring.process_env(), Path.cwd())
    except GraphCliError as exc:
        _fail(f"{exc.code}: {exc.message}", exit_code_for(exc.category))
    from waitress.server import create_server  # the server's own dependency

    app = ServerApp(settings, log=sys.stderr)
    server = create_server(
        app,
        host=options.host,
        port=options.port,
        threads=options.threads,
        channel_timeout=CHANNEL_TIMEOUT_S,
        ident=SERVER_PROGRAM,
    )

    def stop(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    port = getattr(server, "effective_port", options.port)
    print(
        f"{SERVER_PROGRAM} {__version__} listening on http://{options.host}:{port}", file=sys.stderr
    )
    sys.stderr.flush()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        server.task_dispatcher.shutdown()
    print(f"{SERVER_PROGRAM} stopped", file=sys.stderr)


def _fail(message: str, code: int) -> NoReturn:
    print(f"{SERVER_PROGRAM}: error: {message}", file=sys.stderr)
    sys.exit(code)
