"""What the ``graph describe`` commands share: options, the read view, the visible entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, GRAPH_ERRORS, context
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, Schema, array, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.json_access import as_list, as_object, req_str
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.services.catalog_access import Classified, Denied, allowed_entries, not_allowed
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
INCLUDE_NOT_ALLOWED: Final = ParamSpec(
    "--include-not-allowed",
    "flag",
    "Also list what you see but may not use, marked allowed: false with what is missing.",
)
MISSING: Final = array(obj({"action": STRING, "resource": STRING}))
# Optional keys of an item that is not allowed (an allowed item has neither).
ACCESS_MARKS: Final = {"allowed": BOOLEAN, "missing": MISSING}
# The count a list adds when the caller sees entries it may not use (absent when there are none).
NOT_ALLOWED_COUNT: Final = {"not_allowed": INTEGER}
MODULE_FILTER: Final = {"module": STRING}  # a filtered answer names the module it is limited to


def open_view(state: CommandState, graph: str | None, command: str) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    view = ctx.reader.open(
        ctx.actor, entry, "read.describe", RequestMeta.of(command, {"graph": entry.name})
    )
    state.warnings += view.warnings
    return view


@dataclass(frozen=True, slots=True)
class Catalogs:
    """The functions, actions and views of a graph for the caller: what it may use (``None``:
    none) and, in ``denied``, what it sees but may not use. Hidden entries are in neither."""

    functions: tuple[FunctionEntry, ...] | None
    actions: tuple[ActionEntry, ...] | None
    views: tuple[ViewEntry, ...] | None
    denied: Denied


def split_functions(
    state: CommandState, view: GraphRead
) -> tuple[tuple[FunctionEntry, ...] | None, tuple[Classified[FunctionEntry], ...]]:
    """The functions the caller may use and the ones it sees but may not."""
    found = context(state).reader.classified_functions(view.actor, view.entry)
    return allowed_entries(found) or None, not_allowed(found)


def split_actions(
    state: CommandState, view: GraphRead
) -> tuple[tuple[ActionEntry, ...] | None, tuple[Classified[ActionEntry], ...]]:
    """The actions the caller may run (all their write rights) and the ones it sees but may
    not run."""
    reader = context(state).reader
    published = reader.published_actions(view.entry)
    if published is None:
        return None, ()
    found = reader.classified_actions(view.actor, view.entry, published)
    return allowed_entries(found) or None, not_allowed(found)


def split_views(
    state: CommandState, view: GraphRead
) -> tuple[tuple[ViewEntry, ...] | None, tuple[Classified[ViewEntry], ...]]:
    """The views the caller may use (what they name is visible, ``read.function`` for the
    functions they run, ``read.find`` for set views) and the ones it sees but may not."""
    found = context(state).reader.classified_views(view.actor, view.entry)
    return allowed_entries(found) or None, not_allowed(found)


def catalogs(state: CommandState, view: GraphRead) -> Catalogs:
    """All three kinds, split into what the caller may use and what it may not."""
    functions, no_functions = split_functions(state, view)
    actions, no_actions = split_actions(state, view)
    views, no_views = split_views(state, view)
    return Catalogs(functions, actions, views, Denied(no_functions, no_actions, no_views))


def _missing_text(item: JsonObject) -> str:
    """``read.function on graph, write.create on type:ex:T``: the rights an item lacks."""
    return ", ".join(
        f"{req_str(m, 'action')} on {req_str(m, 'resource')}"
        for m in (as_object(v, "missing") for v in as_list(item.get("missing", []), "missing"))
    )


def item_mark(item: JsonObject) -> str:
    """The mark a list line of an item that is not allowed ends with (none for an allowed one)."""
    if item.get("allowed") is not False:
        return ""
    return f"  [not allowed: missing {_missing_text(item)}]"


def access_lines(item: JsonObject) -> list[str]:
    """What a description of an item that is not allowed says first (none for an allowed one):
    the item is shown, but the caller may not use it."""
    if item.get("allowed") is not False:
        return []
    return [
        f"  NOT ALLOWED for you; missing {_missing_text(item)}",
        "  Ask the maintainer for a role that allows it; do not work around it.",
    ]


def not_allowed_line(data: JsonObject) -> list[str]:
    """The line a list adds when the caller sees entries it may not use and did not ask for
    them (``--include-not-allowed``)."""
    count = data.get("not_allowed")
    if not isinstance(count, int) or count < 1:
        return []
    return [f"{count} more not allowed for you; --include-not-allowed lists them"]


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
        params = (MODULE_ARG, *options, GRAPH)
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
