"""Entry point of the ``graph-admin`` console script.

``main`` hands the click program to ``cli_kit.runner.run``, which renders
every outcome as the B9 envelope (text or ``--json``) with its exit code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from graph_cli.cli_kit.runner import run
from graph_cli_admin.maintenance_cli import hints, wiring
from graph_cli_admin.maintenance_cli.commands.audit import audit_group
from graph_cli_admin.maintenance_cli.commands.files import files_group
from graph_cli_admin.maintenance_cli.commands.graph import graph_group
from graph_cli_admin.maintenance_cli.commands.init import init_command
from graph_cli_admin.maintenance_cli.commands.ontology import ontology_group
from graph_cli_admin.maintenance_cli.commands.view import view_command
from graph_cli_admin.services_admin import error_codes


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="graph_cli_admin", prog_name="graph-admin")
def cli() -> None:
    """Create, version and archive knowledge graphs and their ontologies."""


cli.add_command(init_command)
cli.add_command(graph_group)
cli.add_command(ontology_group)
cli.add_command(audit_group)
cli.add_command(files_group)
cli.add_command(view_command)


def main() -> None:
    env = wiring.process_env()
    error_codes.register()
    hints.register()
    services = wiring.Services(env, Path.cwd())
    code = run(
        cli,
        sys.argv[1:],
        wiring.invocation(env),
        services=services,
        out=sys.stdout,
        err=sys.stderr,
    )
    sys.exit(code)
