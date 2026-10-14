"""Entry point of the ``graph-admin`` console script."""

import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="graph_cli_admin", prog_name="graph-admin")
def main() -> None:
    """Create, version and archive knowledge graphs and their ontologies."""
