"""``graph-admin ontology validate|diff|release``."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.services_admin import ontology

source_argument = click.argument("source", type=click.Path(path_type=Path, file_okay=False))
module_option = click.option(
    "--module", help="Main module id (the file <module>.yaml); needed if SOURCE holds several."
)


@click.group("ontology")
def ontology_group() -> None:
    """Validate, compare and release authored ontologies."""


@ontology_group.command("validate")
@source_argument
@module_option
@json_option
@leaf
def validate(state: CommandState, source: Path, module: str | None) -> Result:
    """Check an authored ontology; error findings fail with admin.model_invalid.

    \b
    Examples:
      graph-admin ontology validate data/example_graph/ontology
      graph-admin ontology validate ./drafts --module inventory --json
    """
    return Result(data=ontology.summary(ontology.compile_source(source, module)))


@ontology_group.command("diff")
@source_argument
@module_option
@click.option(
    "--against",
    type=click.Path(path_type=Path, file_okay=False),
    help="Older authored version to compare with.",
)
@click.option("--graph", "graph_ref", help="Compare with the release this graph is pinned to.")
@data_root_option
@json_option
@leaf
def diff_command(
    state: CommandState,
    source: Path,
    module: str | None,
    against: Path | None,
    graph_ref: str | None,
    data_root: Path | None,
) -> Result:
    """Classify the changes from an older version to SOURCE (patch, minor, major).

    Reports whether the declared version bump suffices; release enforces it.

    \b
    Examples:
      graph-admin ontology diff ./v2 --against ./v1
      graph-admin ontology diff ./drafts --graph example_graph --json
    """
    if (against is None) == (graph_ref is None):
        raise GraphCliError(
            "usage.missing_argument",
            "give exactly one of --against or --graph",
            details={"argument": "against|graph"},
        )
    new = ontology.compile_source(source, module)
    graph = None
    if graph_ref is not None:
        ctx = admin_context(state, data_root)
        entry = ctx.catalog.resolve(graph_ref)
        old = ontology.pinned_model(ctx.catalog, entry)
        graph = {"id": str(entry.id), "name": entry.name, "state": entry.state}
    else:
        assert against is not None
        old = ontology.compile_source(against, module).model
    report, bump = ontology.diff(old, new.model)
    return Result(data={**report.to_json(), "bump": bump}, graph=graph)
