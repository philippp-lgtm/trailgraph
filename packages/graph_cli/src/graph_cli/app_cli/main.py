"""Entry point of the ``graph`` console script.

The click program is generated from the command declarations
(``app_cli.commands``) and run by ``cli_kit.runner.run``, which renders every
outcome as the B9 envelope (text or ``--json``) with its exit code. Every read
and write checks the installed skill 1 first (``app_cli.skill_check``). The
command's timings start at the first import of ``graph_cli``; everything up
to running the command (imports, the click tree) is the ``startup`` phase.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from graph_cli import __version__
from graph_cli.app_cli.commands import all_specs
from graph_cli.app_cli.commands.common import GLOBAL_OPTIONS
from graph_cli.app_cli.skill_check import guarded
from graph_cli.catalog import wiring
from graph_cli.cli_kit.runner import Invocation, run
from graph_cli.cli_kit.spec import build_click
from graph_cli.kernel.timing import Timings


def program(*, remote: bool = False) -> click.Group:
    """The click program; ``remote`` sends every command the server runs there (B14.10)."""
    specs = all_specs()
    if remote:
        from graph_cli.app_cli.remote import remote_specs

        specs = remote_specs(specs)
    specs = guarded(specs)

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.version_option(__version__, prog_name="graph")
    def cli() -> None:
        """Read and write instance data in knowledge graphs.

        \b
        Examples:
          graph graphs
          graph describe types --graph <graph>
        """

    return build_click(
        cli,
        specs,
        group_help={
            "describe": "Describe the commands, or the ontology of a graph.",
            "file": "Store files in a graph and find them again.",
            "self": "Keep this program and its agent skills in step with the server.",
            "skills": "Write, install and check the agent skills of this program and its graphs.",
        },
        global_options=GLOBAL_OPTIONS,
    )


def main() -> None:
    timings = Timings.since_import()
    env = wiring.process_env()
    json_mode, debug = wiring.output_mode(env)
    services = wiring.AppServices(env, Path.cwd(), wiring.process_executable(), timings)
    cli = program(remote=services.remote_mode())
    timings.until_now("startup")
    code = run(
        cli,
        sys.argv[1:],
        Invocation(wiring.PROGRAM, json_mode, debug, wiring.OutputSpill(services)),
        services=services,
        out=sys.stdout,
        err=sys.stderr,
        after=lambda done: services.record_access(
            done.document,
            json_mode=done.json_mode,
            stdout_bytes=done.stdout_bytes,
            spilled_bytes=done.spilled_bytes,
        ),
        timings=timings,
    )
    sys.exit(code)
