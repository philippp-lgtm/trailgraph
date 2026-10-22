"""Entry point of the ``graph`` console script.

The click program is generated from the command declarations
(``app_cli.commands``) and run by ``cli_kit.runner.run``, which renders every
outcome as the B9 envelope (text or ``--json``) with its exit code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from graph_cli.app_cli.commands import all_specs
from graph_cli.app_cli.commands.common import GLOBAL_OPTIONS
from graph_cli.catalog import wiring
from graph_cli.cli_kit.runner import Invocation, run
from graph_cli.cli_kit.spec import build_click


def program() -> click.Group:
    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.version_option(package_name="graph_cli", prog_name="graph")
    def cli() -> None:
        """Read and write instance data in knowledge graphs.

        \b
        Examples:
          graph graphs
          graph describe types --graph <graph>
        """

    return build_click(
        cli,
        all_specs(),
        group_help={
            "describe": "Describe the commands, or the ontology of a graph.",
            "file": "Store files in a graph and find them again.",
            "skills": "Write, install and check the agent skills of this program and its graphs.",
        },
        global_options=GLOBAL_OPTIONS,
    )


def main() -> None:
    env = wiring.process_env()
    json_mode, debug = wiring.output_mode(env)
    services = wiring.AppServices(env, Path.cwd(), wiring.process_executable())
    code = run(
        program(),
        sys.argv[1:],
        Invocation(wiring.PROGRAM, json_mode, debug),
        services=services,
        out=sys.stdout,
        err=sys.stderr,
        after=lambda done: services.record_access(
            done.document, json_mode=done.json_mode, stdout_bytes=done.stdout_bytes
        ),
    )
    sys.exit(code)
