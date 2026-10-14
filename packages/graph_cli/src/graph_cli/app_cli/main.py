"""Entry point of the ``graph`` console script."""

import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="graph_cli", prog_name="graph")
def main() -> None:
    """Read and write instance data in knowledge graphs."""
