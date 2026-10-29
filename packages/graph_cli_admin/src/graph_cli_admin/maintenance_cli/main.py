"""Entry point of the ``graph-admin`` console script.

The click program is generated from the command declarations
(``maintenance_cli.commands``); ``main`` hands it to ``cli_kit.runner.run``,
which renders every outcome as the B9 envelope (text or ``--json``) with
its exit code. On a host in remote mode the commands the server runs go to
its admin API instead (``maintenance_cli.remote``, B14.15).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from graph_cli.cli_kit.runner import run
from graph_cli.cli_kit.spec import build_click
from graph_cli_admin import __version__
from graph_cli_admin.maintenance_cli import hints, wiring
from graph_cli_admin.maintenance_cli.commands import all_specs
from graph_cli_admin.services_admin import error_codes


def program(*, remote: bool = False) -> click.Group:
    """The click program; ``remote`` sends every command the server runs to its admin API."""
    specs = all_specs()
    if remote:
        from graph_cli_admin.maintenance_cli.remote import remote_specs

        specs = remote_specs(specs)

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.version_option(__version__, prog_name="graph-admin")
    def cli() -> None:
        """Create, version and archive knowledge graphs and their ontologies."""

    return build_click(
        cli,
        specs,
        group_help={
            "audit": "Verify and anchor the audit trail.",
            "describe": "Describe the commands of this program.",
            "files": "Verify and clean up the stored files of a graph.",
            "functions": "Publish the named read functions of a graph, or show them.",
            "graph": "Create and inspect graphs.",
            "ontology": "Validate, compare and release authored ontologies.",
            "policy": "Check, show, compare, apply and explain access policies.",
            "self": "Install the server's build of this program.",
            "skill": "Publish the graph skill that the app installs for agents, or preview it.",
            "skills": "Install and check the admin skills of this program for an agent.",
            "token": "Create, list and revoke bearer tokens of the server API.",
            "workspace": "Set up a maintainer workspace with one folder per graph.",
        },
    )


def main() -> None:
    env = wiring.process_env()
    error_codes.register()
    hints.register()
    services = wiring.Services(env, Path.cwd())
    code = run(
        program(remote=services.remote_mode()),
        sys.argv[1:],
        wiring.invocation(env),
        services=services,
        out=sys.stdout,
        err=sys.stderr,
    )
    sys.exit(code)
