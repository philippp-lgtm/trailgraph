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
raises ``TOOLS_FORMAT``. The ``lookup`` tool comes last: it is listed when a tool has a reference
input and the caller may find entities (``build_manifest(find=True)``), and the descriptions of
those inputs name it.

Every schema is self-contained (no ``$ref``, no ``$defs``) and has an object at its root.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.app_cli.column_schemas import column_schema
from graph_cli.app_cli.commands.do import DO_ERRORS, DO_OUTPUT
from graph_cli.app_cli.commands.read.entity import VIEW_ERRORS
from graph_cli.app_cli.commands.read.find import LOOKUP_ERRORS, LOOKUP_PAGE_OUTPUT
from graph_cli.app_cli.commands.read.query import RUN_ERRORS, RUN_OUTPUT
from graph_cli.app_cli.output_schemas import standalone
from graph_cli.app_cli.view_schemas import SectionShape, view_schema
from graph_cli.cli_kit.output_schemas import ref
from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services import describe_tools
from graph_cli.services.describe import ModuleIndex, model_envelope
from graph_cli.services.describe_tools import (
    ACTION,
    FUNCTION,
    LOOKUP,
    VIEW,
    RefInput,
    ToolColumn,
    ToolHead,
)
from graph_cli.services.tool_errors import error_info

TOOLS_FORMAT: Final = 1

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
# What the lookup tool never gets: it has no type, relation or entry name to be unknown or
# ambiguous (its input is an enum of the slots it serves), and every slot has a name to look up.
NOT_FOR_LOOKUP: Final = frozenset(
    {
        "term.unknown",
        "query.ambiguous_term",
        "term.no_labels",
        "function.parameter_unknown",
        "action.parameter_unknown",
        "view.parameter_unknown",
    }
)


def row_schema(
    columns: Sequence[ToolColumn], model: CompiledModel, *, cut: bool = False
) -> JsonObject:
    """One row of a function's result: a property per column, typed against the caller's
    ``model`` where the function's types allow (``app_cli.column_schemas``), else any JSON value
    marked ``x-graph-cli: {typed: false}``. ``cut``: an entity view shows the row, which may cut
    its long text cells."""
    return {
        "type": "object",
        "properties": {c.name: column_schema(c.type, c.summary, model, cut=cut) for c in columns},
        "required": [c.name for c in columns],
    }


def function_output(columns: Sequence[ToolColumn], model: CompiledModel) -> JsonObject:
    """What a call of a function answers (``data``): ``RUN_OUTPUT`` plus the ``page`` the run
    command puts in the envelope, with a ``row_schema`` row."""
    base = standalone(RUN_OUTPUT)
    properties = dict(_properties(base))
    properties["items"] = {"type": "array", "items": row_schema(columns, model)}
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


def view_output(head: ToolHead, model: CompiledModel) -> JsonObject:
    """What a call of a view answers: the view model with the sections the view declares, each by
    its id (``app_cli.view_schemas``). The rows of a ``rows`` section are typed by the columns of
    its function as the caller sees them; an entity view cuts long text cells, so there a decimal
    cell loses its pattern (``column_schemas.column_schema``)."""
    sections = []
    for section in head.sections:
        row = None
        if section.columns is not None:
            row = row_schema(section.columns, model, cut=head.entity)
        sections.append(SectionShape(section.kind, section.id, section.function, row))
    assert head.entry is not None  # only the lookup tool has no entry
    return standalone(view_schema(head.entry, sections, entity=head.entity))


def lookup_output() -> JsonObject:
    """What a call of the lookup tool answers: the lookup answer and the ``page`` the command puts
    in the envelope."""
    return standalone(LOOKUP_PAGE_OUTPUT)


def tool_errors(kind: str) -> tuple[str, ...]:
    """The codes a tool of ``kind`` may return, sorted."""
    command = {FUNCTION: RUN_ERRORS, ACTION: DO_ERRORS, VIEW: VIEW_ERRORS, LOOKUP: LOOKUP_ERRORS}[
        kind
    ]
    codes = {*command, *CALL_ERRORS, *TRANSPORT_ERRORS} - NOT_FOR_TOOLS
    if kind == LOOKUP:
        # a call of the tool names its input as an object, which the command's options do not
        codes = (codes - NOT_FOR_LOOKUP) | {"usage.missing_argument"}
    return tuple(sorted(codes))


def _output(head: ToolHead, model: CompiledModel) -> JsonObject:
    if head.kind == FUNCTION:
        return function_output(head.columns, model)
    if head.kind == ACTION:
        return action_output()
    if head.kind == LOOKUP:
        return lookup_output()
    return view_output(head, model)


def tool_hash(head: ToolHead, model: CompiledModel) -> str:
    """The ``hash`` of the tool of ``head`` in the manifest, rendered for ``model``."""
    return str(tool_json(head, model)["hash"])


def tool_json(head: ToolHead, model: CompiledModel) -> JsonObject:
    """One tool of the manifest, rendered for the caller's ``model``. ``hash`` covers what a
    harness builds its tool from (name, description, both schemas, annotations): it changes when
    an ontology release changes an enum or the type of a column even though the entry's hash does
    not."""
    output = _output(head, model)
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
    and actions (a view and the lookup tool have no such flag and stay); deprecated tools are
    listed unless ``exclude_deprecated``, so an allow-listed tool does not vanish when its entry is
    deprecated."""

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
    *,
    only: str | None = None,
    refs: Sequence[RefInput] = (),
) -> tuple[ToolHead, ...]:
    """Every tool the caller may use: functions, then actions, then views, each by name, then the
    ``lookup`` tool. With ``only`` just the tool of that name is built (a function's head resolves
    its query to type its columns, which a call that needs one tool should not pay for every
    function); the entries still count as the ones a replacement is looked up in.

    ``refs`` are the reference inputs the ``lookup`` tool serves (``lookup_refs``, over every
    entry the caller may use, whatever ``only`` says): the tool is built, and the descriptions of
    those inputs name it, only when there are some."""
    lookup = bool(refs)

    def wanted(kind: str, entry: FunctionEntry | ActionEntry | ViewEntry) -> bool:
        return only is None or describe_tools.tool_name(kind, entry.name) == only

    function_heads = tuple(
        describe_tools.function_head(e, model, functions, lookup=lookup)
        for e in _by_name(functions)
        if wanted(FUNCTION, e)
    )
    known = {h.entry: h.columns for h in function_heads if h.entry is not None}  # rows of a view
    found = (
        *function_heads,
        *(
            describe_tools.action_head(e, model, actions, obligations, lookup=lookup)
            for e in _by_name(actions)
            if wanted(ACTION, e)
        ),
        *(
            describe_tools.view_head(e, model, views, functions, known, lookup=lookup)
            for e in _by_name(views)
            if wanted(VIEW, e)
        ),
        *((describe_tools.lookup_head(refs),) if lookup and only in (None, LOOKUP) else ()),
    )
    describe_tools.unique_names(found)
    return found


def lookup_refs(
    model: CompiledModel,
    functions: Sequence[FunctionEntry],
    actions: Sequence[ActionEntry],
    views: Sequence[ViewEntry],
    *,
    find: bool,
) -> tuple[RefInput, ...]:
    """The reference inputs of the tools the caller may use that the ``lookup`` tool serves: none
    when the caller may not find entities (``find``: it holds ``read.find``; until a narrower
    lookup exists, a reference input takes the ids the caller already has)."""
    return describe_tools.ref_inputs(model, functions, actions, views) if find else ()


def _by_name[Entry: (FunctionEntry, ActionEntry, ViewEntry)](
    entries: Sequence[Entry],
) -> list[Entry]:
    return sorted(entries, key=lambda e: e.name)


def missing_tools(wanted: Sequence[str], names: Sequence[str]) -> GraphCliError | None:
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
        and (not selection.featured or head.kind in (VIEW, LOOKUP) or head.featured)
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
    find: bool = False,
) -> dict[str, JsonValue]:
    """The manifest (``graph_cli.model/1``) of the entries the caller may use, over the model it
    sees; ``find``: the caller holds ``read.find``, which lists the ``lookup`` tool. A wanted name
    that is no visible tool is ``tool.not_found``; ``term.unknown`` for an unknown module.
    ``version`` is the hash of the tools and errors listed."""
    selection = selection or Selection()
    module = ModuleIndex(model).filter(selection.module)
    refs = lookup_refs(model, functions, actions, views, find=find)
    available = heads(model, functions, actions, views, obligations, refs=refs)
    problem = missing_tools(selection.tools, [h.name for h in available])
    if problem is not None:
        raise problem
    tools = [tool_json(h, model) for h in available if _kept(h, selection, module)]
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
