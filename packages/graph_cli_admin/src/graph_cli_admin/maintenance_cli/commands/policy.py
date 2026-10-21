"""``graph-admin policy check|show|diff|apply|explain``: who may use which graph (B6.16)."""

from __future__ import annotations

from pathlib import Path

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli_admin.maintenance_cli.commands.common import admin_context, data_root_option
from graph_cli_admin.services_admin import policy

file_argument = click.argument("file", type=click.Path(path_type=Path, dir_okay=False))
graph_option = click.option(
    "--graph", "graph_ref", help="The graph of a graph policy (name or id)."
)


@click.group("policy")
def policy_group() -> None:
    """Check, show, compare, apply and explain access policies."""


@policy_group.command("check")
@file_argument
@graph_option
@data_root_option
@json_option
@leaf
def check(state: CommandState, file: Path, graph_ref: str | None, data_root: Path | None) -> Result:
    """Compile an authored policy and report every problem; installs nothing.

    The file's id says its layer: installation (one per installation) or
    graph (needs --graph).

    \b
    Examples:
      graph-admin policy check data/policy/installation.yaml
      graph-admin policy check data/policy/example_graph.yaml --graph example_graph --json
    """
    outcome = policy.check(admin_context(state, data_root), file=file, graph_ref=graph_ref)
    return Result(data=outcome.data, warnings=outcome.warnings)


@policy_group.command("show")
@graph_option
@data_root_option
@json_option
@leaf
def show(state: CommandState, graph_ref: str | None, data_root: Path | None) -> Result:
    """Show the installed compiled policy and its hash (the built-in one if none).

    \b
    Examples:
      graph-admin policy show
      graph-admin policy show --graph example_graph --json
    """
    outcome = policy.show(admin_context(state, data_root), graph_ref=graph_ref)
    return Result(data=outcome.data)


@policy_group.command("diff")
@file_argument
@graph_option
@data_root_option
@json_option
@leaf
def diff(state: CommandState, file: Path, graph_ref: str | None, data_root: Path | None) -> Result:
    """Compare an authored policy with the installed one, rule by rule.

    \b
    Examples:
      graph-admin policy diff data/policy/installation.yaml
    """
    outcome = policy.diff(admin_context(state, data_root), file=file, graph_ref=graph_ref)
    return Result(data=outcome.data, warnings=outcome.warnings)


@policy_group.command("apply")
@file_argument
@graph_option
@click.option("--reason", required=True, help="Why the policy changes; recorded in the audit.")
@data_root_option
@json_option
@leaf
def apply(
    state: CommandState, file: Path, graph_ref: str | None, reason: str, data_root: Path | None
) -> Result:
    """Install an authored policy (audited as maint.policy_change).

    Refused when it would stop you from changing policies again. Applying
    the installed policy again changes nothing.

    \b
    Examples:
      graph-admin policy apply data/policy/installation.yaml --reason "project scopes"
      graph-admin policy apply data/policy/example_graph.yaml --graph example_graph \
        --reason "files read-only"
    """
    outcome = policy.apply(
        admin_context(state, data_root), file=file, graph_ref=graph_ref, reason=reason
    )
    return Result(data=outcome.data, warnings=outcome.warnings)


@policy_group.command("explain")
@click.option("--graph", "graph_ref", required=True, help="Graph name or id.")
@click.option("--project", help="The project of the caller; without it: no project.")
@click.option("--action", help="One action, e.g. write.create; without it: every app action.")
@click.option("--type", "type_curie", help="Decide on a type of the graph, e.g. files:File.")
@click.option(
    "--file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Explain an authored policy instead of the installed one.",
)
@data_root_option
@json_option
@leaf
def explain(
    state: CommandState,
    graph_ref: str,
    project: str | None,
    action: str | None,
    type_curie: str | None,
    file: Path | None,
    data_root: Path | None,
) -> Result:
    """What an app caller from a project would get on a graph, and by which rule.

    Records nothing.

    \b
    Examples:
      graph-admin policy explain --graph example_graph --project example_project
      graph-admin policy explain --graph example_graph --action write.create --type files:File
    """
    outcome = policy.explain(
        admin_context(state, data_root),
        graph_ref=graph_ref,
        project=project,
        action=action,
        type_curie=type_curie,
        file=file,
    )
    return Result(data=outcome.data, warnings=outcome.warnings)
