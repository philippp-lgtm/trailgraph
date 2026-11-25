"""The tool manifest of a graph (``graph describe tools``): one tool per function, action and view
the caller may use, each with a JSON Schema 2020-12 input and output schema, and an error map.

The per-entry half (names, input schemas, controls, descriptions) is ``services.describe_tools``;
this module adds what lives in the app layer: the schemas of the answers (``RUN_OUTPUT``,
``DO_OUTPUT``, the view model), the error codes of each kind of tool, the hashes and the document.
Everything is rendered for one caller from the entries it may use and the model it sees, so a
hidden or not-allowed entry, a hidden enum value or a hidden class never appears.

The manifest is a public contract (``TOOLS_FORMAT``): input schemas are closed, output schemas
stay open like every published output schema (a consumer ignores what it does not know), so an
answer may grow without breaking a harness that validates; a change that breaks a consumer
raises ``TOOLS_FORMAT``. The ``lookup`` tool of the format is built in a later step.

Every schema is self-contained (no ``$ref``, no ``$defs``) and has an object at its root.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.app_cli.commands.do import DO_ERRORS, DO_OUTPUT
from graph_cli.app_cli.commands.read.entity import VIEW_ERRORS, VIEW_OUTPUT
from graph_cli.app_cli.commands.read.query import RUN_ERRORS, RUN_OUTPUT
from graph_cli.app_cli.output_schemas import DEFS
from graph_cli.cli_kit.output_schemas import Schema, ref
from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services import describe_tools
from graph_cli.services.describe import ModuleIndex, model_envelope
from graph_cli.services.describe_tools import ACTION, FUNCTION, VIEW, ToolHead
from graph_cli.services.tool_errors import error_info

TOOLS_FORMAT: Final = 1
LOOKUP: Final = "lookup"  # a tool of the format that a later step builds
TOOL_KINDS: Final = (FUNCTION, ACTION, VIEW, LOOKUP)

# What a harness may get from the transport and the call itself, besides the codes of the command
# a tool runs. A tool never gets a code that needs an argument it does not have (it selects no
# graph, reads no earlier state, passes a typed entity and names a tool, not an entry).
CALL_ERRORS: Final = ("tool.not_found", "tool.hash_mismatch")
TRANSPORT_ERRORS: Final = (
    "remote.unauthorized",
    "usage.invalid_argument",
    "usage.invalid_input",
    "internal.error",
)
NOT_FOR_TOOLS: Final = frozenset(
    {
        "graph.not_selected",
        "temporal.as_of_unsupported",
        "view.takes_entity",
        "view.takes_no_entity",
        # a call of an entry the caller cannot see is tool.not_found
        "function.not_found",
        "action.not_found",
        "view.not_found",
    }
)
UNTYPED: Final[JsonObject] = {"x-graph-cli": {"typed": False}}


def standalone(schema: Schema, defs: Mapping[str, Schema] = DEFS) -> JsonObject:
    """``schema`` with every ``$ref`` into ``defs`` replaced by the definition it names, so that it
    stands alone. A definition that refers to itself is a bug (``ValueError``)."""

    def inline(node: JsonValue, active: tuple[str, ...]) -> JsonValue:
        if isinstance(node, list):
            return [inline(item, active) for item in node]
        if not isinstance(node, Mapping):
            return node
        target = node.get("$ref")
        if isinstance(target, str):
            name = target.removeprefix("#/$defs/")
            if name in active:
                raise ValueError(f"output schema definition {name!r} refers to itself")
            return inline(defs[name], (*active, name))
        return {key: inline(value, active) for key, value in node.items()}

    found = inline(schema, ())
    assert isinstance(found, dict)
    return found


def function_output(columns: Sequence[str]) -> JsonObject:
    """What a call of a function answers (``data``): ``RUN_OUTPUT`` plus the ``page`` the run
    command puts in the envelope, with a row of the function's columns, each untyped for now
    (any JSON value, ``x-graph-cli: {typed: false}``)."""
    base = standalone(RUN_OUTPUT)
    properties = dict(_properties(base))
    row: dict[str, JsonValue] = {
        "type": "object",
        "properties": {column: UNTYPED for column in columns},
        "required": list(columns),
    }
    properties["items"] = {"type": "array", "items": row}
    properties["page"] = standalone(ref("page"))
    required = [*_required(base), "page"]
    return {"type": "object", "properties": properties, "required": required}


def _properties(schema: JsonObject) -> JsonObject:
    found = schema["properties"]
    assert isinstance(found, dict)
    return found


def _required(schema: JsonObject) -> list[JsonValue]:
    found = schema["required"]
    assert isinstance(found, list)
    return found


def action_output() -> JsonObject:
    """What a call of an action answers: the write result of the ``do`` command, for a dry run
    as for a real run."""
    return standalone(DO_OUTPUT)


def view_output() -> JsonObject:
    """What a call of a view answers: the view model, its sections still loose."""
    return standalone(VIEW_OUTPUT)


def tool_errors(kind: str) -> tuple[str, ...]:
    """The codes a tool of ``kind`` may return, sorted."""
    command = {FUNCTION: RUN_ERRORS, ACTION: DO_ERRORS, VIEW: VIEW_ERRORS}[kind]
    codes = {*command, *CALL_ERRORS, *TRANSPORT_ERRORS} - NOT_FOR_TOOLS
    return tuple(sorted(codes))


def tool_json(head: ToolHead) -> JsonObject:
    """One tool of the manifest. ``hash`` covers what a harness builds its tool from (name,
    description, both schemas, annotations): it changes when an ontology release changes an enum
    even though the entry's hash does not."""
    output = {
        FUNCTION: lambda: function_output(head.columns),
        ACTION: action_output,
        VIEW: view_output,
    }[head.kind]()
    built: JsonObject = {
        "name": head.name,
        "description": head.description,
        "input_schema": head.input_schema,
        "output_schema": output,
        "annotations": head.annotations,
    }
    return {
        "name": head.name,
        "kind": head.kind,
        "entry": head.entry,
        "description": head.description,
        "input_schema": head.input_schema,
        "output_schema": output,
        "annotations": head.annotations,
        "status": head.status,
        "replaced_by": head.replaced_by,
        "featured": head.featured,
        "module": head.module,
        "hash": hash_value(built),
        "entry_hash": head.entry_hash,
        "errors": list(tool_errors(head.kind)),
    }


@dataclass(frozen=True, slots=True)
class Selection:
    """Which of the tools a caller may use are listed. ``featured`` keeps the featured functions
    and actions (a view has no such flag and stays); deprecated tools are listed unless
    ``exclude_deprecated``, so an allow-listed tool does not vanish when its entry is deprecated."""

    tools: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    module: str | None = None
    featured: bool = False
    exclude_deprecated: bool = False


def heads(
    model: CompiledModel,
    functions: Sequence[FunctionEntry],
    actions: Sequence[ActionEntry],
    views: Sequence[ViewEntry],
    obligations: frozenset[str],
) -> tuple[ToolHead, ...]:
    """Every tool the caller may use: functions, then actions, then views, each by name."""
    found = (
        *(describe_tools.function_head(e, model, functions) for e in _by_name(functions)),
        *(describe_tools.action_head(e, model, actions, obligations) for e in _by_name(actions)),
        *(describe_tools.view_head(e, model, views) for e in _by_name(views)),
    )
    describe_tools.unique_names(found)
    return found


def _by_name[Entry: (FunctionEntry, ActionEntry, ViewEntry)](
    entries: Sequence[Entry],
) -> list[Entry]:
    return sorted(entries, key=lambda e: e.name)


def _missing(wanted: Sequence[str], names: Sequence[str]) -> GraphCliError | None:
    """``tool.not_found`` for the first wanted name no tool has, with the close names."""
    absent = [name for name in wanted if name not in names]
    if not absent:
        return None
    first = absent[0]
    return GraphCliError(
        "tool.not_found",
        f"no tool {first!r}" + (f" (and {len(absent) - 1} more)" if len(absent) > 1 else ""),
        details={
            "name": first,
            "missing": absent,
            "did_you_mean": difflib.get_close_matches(first, sorted(names), n=5),
        },
    )


def _kept(head: ToolHead, selection: Selection, module: str | None) -> bool:
    return (
        (not selection.tools or head.name in selection.tools)
        and (not selection.kinds or head.kind in selection.kinds)
        and (module is None or head.module == module)
        and (not selection.featured or head.kind == VIEW or head.featured)
        and not (selection.exclude_deprecated and head.status == "deprecated")
    )


def build_manifest(
    entry: GraphEntry,
    model: CompiledModel,
    *,
    functions: Sequence[FunctionEntry],
    actions: Sequence[ActionEntry],
    views: Sequence[ViewEntry],
    obligations: frozenset[str],
    selection: Selection | None = None,
) -> dict[str, JsonValue]:
    """The manifest (``graph_cli.model/1``) of the entries the caller may use, over the model it
    sees. A wanted name that is no visible tool is ``tool.not_found``; ``term.unknown`` for an
    unknown module. ``version`` is the hash of the tools and errors listed."""
    selection = selection or Selection()
    module = ModuleIndex(model).filter(selection.module)
    available = heads(model, functions, actions, views, obligations)
    problem = _missing(selection.tools, [h.name for h in available])
    if problem is not None:
        raise problem
    tools = [tool_json(h) for h in available if _kept(h, selection, module)]
    codes = sorted({code for tool in tools for code in _codes(tool)})
    errors: dict[str, JsonValue] = {code: error_info(code) for code in codes}
    version = hash_value({"tools": tools, "errors": errors})
    return model_envelope(
        entry, model, tools_format=TOOLS_FORMAT, version=version, tools=tools, errors=errors
    )


def _codes(tool: JsonObject) -> list[str]:
    listed = tool["errors"]
    assert isinstance(listed, list)
    return [code for code in listed if isinstance(code, str)]
