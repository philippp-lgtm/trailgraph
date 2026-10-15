"""``graph-admin graph create|list|show`` and ``graph-admin graph skill publish``."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.services_admin import graphs, skill


@click.group("graph")
def graph_group() -> None:
    """Create and inspect graphs."""


@graph_group.command("create")
@click.argument("name")
@click.option(
    "--ontology",
    "source",
    required=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory with the authored ontology; becomes the first release.",
)
@click.option("--module", help="Main module id, if the directory holds several.")
@click.option("--title", help="Human-readable title.")
@click.option(
    "--language",
    help="BCP 47 default language of the values (e.g. en, de-CH); fixed after create.",
)
@data_root_option
@json_option
@leaf
def create(
    state: CommandState,
    name: str,
    source: Path,
    module: str | None,
    title: str | None,
    language: str | None,
    data_root: Path | None,
) -> Result:
    """Create graph NAME pinned to the first release of an authored ontology.

    \b
    Examples:
      graph-admin graph create example_graph --ontology data/example_graph/ontology
      graph-admin graph create example_graph --ontology ./onto --title "Example" --json
      graph-admin graph create example_graph --ontology ./onto --language en
    """
    ctx = admin_context(state, data_root)
    manifest = graphs.create(
        ctx, name=name, source=source, module=module, title=title, language=language
    )
    return Result(
        data=graphs.describe(manifest), graph=graphs.envelope_graph(manifest.entry, seq=0)
    )


@graph_group.command("list")
@click.option("--all", "include_archived", is_flag=True, help="Include archived graphs.")
@data_root_option
@json_option
@leaf
def list_command(state: CommandState, include_archived: bool, data_root: Path | None) -> Result:
    """List the graphs of the data root.

    \b
    Examples:
      graph-admin graph list
      graph-admin graph list --all --json
    """
    ctx = admin_context(state, data_root)
    items = graphs.list_graphs(ctx, include_archived=include_archived)
    state.warnings += ctx.catalog.warnings
    return Result(data={"items": items})


@graph_group.command("show")
@click.argument("ref")
@data_root_option
@json_option
@leaf
def show(state: CommandState, ref: str, data_root: Path | None) -> Result:
    """Show the manifest, head seq and installed releases of graph REF (name or id).

    \b
    Examples:
      graph-admin graph show example_graph
      graph-admin graph show 0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a --json
    """
    ctx = admin_context(state, data_root)
    entry, data = graphs.show(ctx, ref)
    state.warnings += ctx.catalog.warnings
    seq = data["seq"]
    return Result(
        data=data, graph=graphs.envelope_graph(entry, seq if isinstance(seq, int) else None)
    )


@graph_group.group("skill")
def skill_group() -> None:
    """Publish the graph skill that the app installs for agents."""


@skill_group.command("publish")
@click.argument("ref")
@click.argument(
    "source",
    metavar="DIR",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
)
@click.option("--reason", help="Why the skill changes; recorded in the audit event.")
@click.option("--dry-run", is_flag=True, help="Validate and show the change without writing.")
@data_root_option
@json_option
@leaf
def publish(
    state: CommandState,
    ref: str,
    source: Path,
    reason: str | None,
    dry_run: bool,
    data_root: Path | None,
) -> Result:
    """Publish DIR as the skill of graph REF (name or id).

    DIR is a complete skill directory named graph-<graph> (underscores become
    dashes). Publishing the same files again changes nothing.

    \b
    Examples:
      graph-admin graph skill publish example_graph data/example_graph/skill/graph-example-graph
      graph-admin graph skill publish example_graph ./graph-example-graph --dry-run --json
    """
    ctx = admin_context(state, data_root)
    published = skill.publish(
        ctx, graph_ref=ref, source=source.resolve(), reason=reason, dry_run=dry_run
    )
    return Result(data=published.data, graph=graphs.envelope_graph(published.entry, published.seq))
