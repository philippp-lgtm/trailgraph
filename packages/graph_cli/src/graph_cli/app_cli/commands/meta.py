"""``graph whoami``, ``graph graphs`` and ``graph describe ...`` (contracts B2.10, B6.12, B11)."""

from __future__ import annotations

from typing import Final

from graph_cli import __version__
from graph_cli.app_cli.commands.common import (
    GRAPH,
    JSON_OPTION,
    PROGRAM,
    context,
)
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import RequestMeta
from graph_cli.services import describe
from graph_cli.services.read import GraphRead

TYPE_ARG: Final = ParamSpec("type", "term", "Type CURIE.", required=True, positional=True)
GRAPH_ERRORS: Final = (
    "graph.not_selected",
    "graph.not_found",
    "graph.not_in_project",
    "config.invalid",
    "config.forbidden_key",
    "access.denied",
    "ontology.artifact_missing",
)


def whoami(state: CommandState) -> Result:
    ctx = context(state)  # builds nothing on disk; works without a data root
    return Result(data=ctx.reader.whoami(ctx.actor))


def graphs(state: CommandState) -> Result:
    ctx = context(state)
    allowed = ctx.pointer.graphs if ctx.pointer is not None else None
    items, hidden, warnings = ctx.reader.graphs(
        ctx.actor, RequestMeta.of("graphs", {}), allowed=allowed
    )
    state.warnings += warnings
    return Result(data={"items": items, "hidden_count": hidden})


def describe_commands(state: CommandState) -> Result:
    from graph_cli.app_cli.commands import all_specs

    return Result(data=inventory(all_specs()))


def inventory(specs: tuple[CommandSpec, ...]) -> dict[str, JsonValue]:
    return build_inventory(
        specs,
        program=PROGRAM,
        version=__version__,
        surface="app",
        global_options=(JSON_OPTION, GRAPH),
    )


def _view(state: CommandState, graph: str | None, command: str) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    view = ctx.reader.open(
        ctx.actor, entry, "read.describe", RequestMeta.of(command, {"graph": entry.name})
    )
    state.warnings += view.warnings
    return view


def describe_types(state: CommandState, graph: str | None) -> Result:
    view = _view(state, graph, "describe types")
    return Result(data=describe.describe_types(view), graph=view.envelope_graph())


def describe_type(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe type")
    return Result(data=describe.describe_type(view, type), graph=view.envelope_graph())


def describe_relations(state: CommandState, graph: str | None) -> Result:
    view = _view(state, graph, "describe relations")
    return Result(data=describe.describe_relations(view), graph=view.envelope_graph())


def describe_fields(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe fields")
    return Result(data=describe.describe_fields(view, type), graph=view.envelope_graph())


def describe_lifecycle(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe lifecycle")
    return Result(data=describe.describe_lifecycle(view, type), graph=view.envelope_graph())


def _describe(
    name: str, summary: str, handler: object, *, with_type: bool, examples: tuple[Example, ...]
) -> CommandSpec:
    params = (TYPE_ARG, GRAPH) if with_type else (GRAPH,)
    errors = (*GRAPH_ERRORS, "term.unknown") if with_type else GRAPH_ERRORS
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
    )


SPECS: Final = (
    CommandSpec(
        path=("whoami",),
        kind="meta",
        surface="meta",
        action=None,
        summary="Show who is acting: the identity facets and what the policy allows.",
        params=(),
        examples=(
            Example("graph whoami", "Show the acting identity."),
            Example("graph whoami --json", "The identity and permissions as JSON."),
        ),
        errors=("config.invalid", "backend.unavailable"),
        handler=whoami,
    ),
    CommandSpec(
        path=("graphs",),
        kind="read",
        surface="retrieve",
        action="read.list",
        summary="List the graphs this project may use.",
        params=(),
        examples=(
            Example("graph graphs", "List the visible graphs."),
            Example("graph graphs --json", "The graphs as JSON, with the hidden count."),
        ),
        errors=("config.invalid", "config.forbidden_key", "access.denied"),
        handler=graphs,
    ),
    CommandSpec(
        path=("describe", "commands"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Describe every command of this program (graph_cli.inventory/1).",
        params=(),
        examples=(
            Example("graph describe commands --json", "The command inventory."),
            Example("graph describe commands", "The inventory as text."),
        ),
        errors=(),
        handler=describe_commands,
    ),
    _describe(
        "types",
        "List the types of the selected graph's ontology.",
        describe_types,
        with_type=False,
        examples=(
            Example("graph describe types", "All types with summaries."),
            Example("graph describe types --graph <graph> --json", "Types of one graph."),
        ),
    ),
    _describe(
        "type",
        "Describe one type: fields, lifecycles, relations and rules.",
        describe_type,
        with_type=True,
        examples=(
            Example("graph describe type <type>", "Everything about one type."),
            Example("graph describe type <type> --json", "The type as graph_cli.model/1."),
        ),
    ),
    _describe(
        "relations",
        "List the relations of the selected graph's ontology.",
        describe_relations,
        with_type=False,
        examples=(
            Example("graph describe relations", "All relations."),
            Example("graph describe relations --json", "Relations as JSON."),
        ),
    ),
    _describe(
        "fields",
        "List the fields of one type with datatypes and limits.",
        describe_fields,
        with_type=True,
        examples=(
            Example("graph describe fields <type>", "Fields of one type."),
            Example("graph describe fields <type> --json", "Fields as JSON."),
        ),
    ),
    _describe(
        "lifecycle",
        "Show the lifecycles of a type, or one lifecycle by name.",
        describe_lifecycle,
        with_type=True,
        examples=(
            Example("graph describe lifecycle <type>", "States and transitions of a type."),
            Example("graph describe lifecycle <type> --json", "Lifecycles as JSON."),
        ),
    ),
)
