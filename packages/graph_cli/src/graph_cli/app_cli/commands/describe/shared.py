"""What the ``graph describe`` commands share: options, the read view, the visible entries."""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, GRAPH_ERRORS, context
from graph_cli.cli_kit.output_schemas import STRING, Schema
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.json_access import req_str
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.services.read import GraphRead

TYPE_ARG: Final = ParamSpec(
    "type", "term", "Type CURIE or unique local name.", required=True, positional=True
)
MODULE_OPTION: Final = ParamSpec(
    "--module", "string", "Only the terms of this module (id or prefix); see describe modules."
)
MODULE_ARG: Final = ParamSpec(
    "module",
    "string",
    "Module id or prefix (see describe modules).",
    required=True,
    positional=True,
)
MODULE_FILTER: Final = {"module": STRING}  # a filtered answer names the module it is limited to


def open_view(state: CommandState, graph: str | None, command: str) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    view = ctx.reader.open(
        ctx.actor, entry, "read.describe", RequestMeta.of(command, {"graph": entry.name})
    )
    state.warnings += view.warnings
    return view


def visible_functions(state: CommandState, view: GraphRead) -> tuple[FunctionEntry, ...] | None:
    """The graph's functions the caller may see (``read.function``), else ``None``."""
    return context(state).reader.functions(view.actor, view.entry)


def visible_actions(state: CommandState, view: GraphRead) -> tuple[ActionEntry, ...] | None:
    """The graph's actions the caller may run (all their write rights), else ``None``."""
    return context(state).reader.actions(view.actor, view.entry)


def visible_views(state: CommandState, view: GraphRead) -> tuple[ViewEntry, ...] | None:
    """The graph's views the caller may see (what they name is visible, ``read.function`` for
    the functions they run, ``read.find`` for set views), else ``None``."""
    return context(state).reader.views(view.actor, view.entry)


def function_call(item: JsonObject) -> str:
    """``name(signature)``: how a function or an action is called."""
    return f"{req_str(item, 'name')}({req_str(item, 'signature')})"


def describe_spec(
    name: str,
    summary: str,
    handler: object,
    *,
    with_type: bool,
    with_module: bool = False,
    examples: tuple[Example, ...],
    output: Schema,
    options: tuple[ParamSpec, ...] = (),
) -> CommandSpec:
    params = (TYPE_ARG, *options, GRAPH) if with_type else (*options, GRAPH)
    if with_module:
        params = (MODULE_ARG, GRAPH)
    errors = (
        (*GRAPH_ERRORS, "term.unknown", "query.ambiguous_term")
        if with_type
        else (*GRAPH_ERRORS, "term.unknown")
        if with_module or MODULE_OPTION in options
        else GRAPH_ERRORS
    )
    return CommandSpec(
        path=("describe", name),
        kind="read",
        surface="model",
        action="read.describe",
        summary=summary,
        params=params,
        examples=examples,
        errors=errors,
        handler=handler,  # type: ignore[arg-type]
        output=output,
    )
