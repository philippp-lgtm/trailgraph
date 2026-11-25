"""The per-entry half of the tool manifest of ``graph describe tools``: what a function, an action
or a view is as a tool of an agent harness, except what it answers and which errors it may give
(``app_cli.tool_manifest`` adds them: the answer schemas live in the app layer).

A tool is named by the kind of its entry and the entry's name: ``run_<function>``,
``do_<action>``, ``view_<view>``. The name is unique by construction (functions and actions share
one namespace, views have their own, and a view may be named like the function its rows run), it
stays when other entries are published and it is a valid identifier of at most 64 characters, the
limit of the common model APIs; the rare entry name that would not fit keeps its first characters
and ends in ``_`` and 8 hex digits of the hash of the name.

The input schema is the object ``graph call`` takes (JSON Schema 2020-12): one property per
parameter, closed (``additionalProperties: false``), a parameter with a default or an optional one
not required. Properties that are not required and have no default accept ``null`` for "not
given", as typed clients send it. Everything that depends on what the caller sees comes from the
model it is given, which for a limited caller is the filtered one: enum values, the class of a
reference.

Controls are the arguments that are not parameters of the entry; their names start with an
underscore, which a parameter name cannot (``kinetic.params.PARAM_RE``), so they never clash with
authored names: ``_limit`` and ``_offset`` of a function, ``_reason`` and ``_dry_run`` of an
action, ``_entity`` of an entity view, ``_cursor`` and ``_page_bytes`` of a set view.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import HASH_PREFIX, JsonObject, JsonValue, hash_value
from graph_cli.kernel.changeset import MAX_REASON
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry, Named, seen_replacement
from graph_cli.kinetic.params import ParamDef
from graph_cli.kinetic.return_types import ColumnType
from graph_cli.kinetic.tool_types import ParamType, input_value, param_types, ref_type
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.kinetic.view_format import MIN_BYTES, PAGE_BYTES_MAX, RowsSection
from graph_cli.kinetic.view_page import PAGE_BYTES
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT
from graph_cli.services.describe import column_types_of

FUNCTION: Final = "function"
ACTION: Final = "action"
VIEW: Final = "view"
KINDS: Final = (FUNCTION, ACTION, VIEW)
PREFIXES: Final = {FUNCTION: "run_", ACTION: "do_", VIEW: "view_"}
MAX_TOOL_NAME: Final = 64
_DIGEST_CHARS: Final = 8

# The extension key of the schemas: what graph_cli says about a value beyond JSON Schema.
EXTENSION: Final = "x-graph-cli"
NULL: Final[JsonObject] = {"type": "null"}

LIMIT: Final = "_limit"
OFFSET: Final = "_offset"
REASON: Final = "_reason"
DRY_RUN: Final = "_dry_run"
ENTITY: Final = "_entity"
CURSOR: Final = "_cursor"
PAGE_BYTES_ARG: Final = "_page_bytes"


def tool_name(kind: str, entry_name: str) -> str:
    """The name of the tool of an entry (see the module docstring)."""
    prefix = PREFIXES[kind]
    name = prefix + entry_name
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hash_value(entry_name).removeprefix(HASH_PREFIX)[:_DIGEST_CHARS]
    kept = MAX_TOOL_NAME - len(prefix) - 1 - _DIGEST_CHARS
    return f"{prefix}{entry_name[:kept]}_{digest}"


@dataclass(frozen=True, slots=True)
class ToolColumn:
    """A result column of a function as the manifest renders it: its name, what it holds and its
    type (``None``: nothing makes it certain, the column is untyped)."""

    name: str
    summary: str
    type: ColumnType | None


@dataclass(frozen=True, slots=True)
class ToolSection:
    """A section a view declares, as its answer schema needs it: the ``kind`` and ``id`` and, for
    a ``rows`` section, its ``function`` and the columns of it (``None``: the caller has no such
    function, the rows are not typed)."""

    kind: str
    id: str
    function: str | None = None
    columns: tuple[ToolColumn, ...] | None = None


@dataclass(frozen=True, slots=True)
class ToolHead:
    """A tool without its answer: everything of the manifest that follows from the entry, the
    model the caller sees and the caller's obligations. ``columns`` are the result columns of a
    function, ``sections`` the sections of a view and ``entity`` whether it shows one entity (none
    of them for the other kinds)."""

    name: str
    kind: str
    entry: str
    description: str
    input_schema: JsonObject
    annotations: JsonObject
    status: str
    replaced_by: str | None
    featured: bool
    module: str | None
    entry_hash: str
    columns: tuple[ToolColumn, ...] = ()
    sections: tuple[ToolSection, ...] = ()
    entity: bool = False


def enum_values(
    model: CompiledModel, name: str | None
) -> tuple[tuple[str, str | None], ...] | None:
    """The values of an enum of the model with their summaries; ``None`` when the model has no
    such enum (an entry that no longer fits the ontology names one)."""
    found = model.enums.get(name) if name is not None else None
    return None if found is None else tuple((v.name, v.summary) for v in found.values)


def _enum_text(values: Sequence[tuple[str, str | None]]) -> str:
    """``Values: low - rarely used; high``: the values with the summaries the ontology gives,
    left out when it gives none (the schema lists the values)."""
    if not any(summary for _, summary in values):
        return ""
    return "Values: " + "; ".join(f"{n} - {s}" if s else n for n, s in values) + "."


def _ref_text(of_class: str | None) -> str:
    return (
        f"An entity of class {of_class}: its id, its alias or 8 to 32 trailing hex digits of "
        "its id; a name has to be looked up first."
    )


def _scalar(param: ParamType, model: CompiledModel) -> tuple[JsonObject, str]:
    """The schema of one value of a parameter and the text it adds to the description."""
    schema: dict[str, JsonValue] = {"type": param.json_type}
    text = ""
    if param.kind == "integer":
        schema["minimum"], schema["maximum"] = param.minimum, param.maximum
    if param.kind in ("date", "datetime"):
        schema["format"] = "date" if param.kind == "date" else "date-time"
    if param.pattern is not None:
        schema["pattern"] = param.pattern
    if param.kind == "enum":
        values = enum_values(model, param.enum)
        if values is not None:
            schema["enum"] = [name for name, _ in values]
            text = _enum_text(values)
    if param.kind == "ref":
        schema[EXTENSION] = {"class": param.of_class}
        text = _ref_text(param.of_class)
    return schema, text


def param_schema(param: ParamType, model: CompiledModel) -> JsonObject:
    """The property schema of a parameter (TM5): the type of one value, an array of at least one
    for ``many``, the default, and ``null`` as "not given" unless the parameter is required or has
    a default."""
    scalar, text = _scalar(param, model)
    description = " ".join(part for part in (param.summary, text) if part)
    schema: dict[str, JsonValue] = {"type": "array", "items": scalar, "minItems": 1}
    if not param.many:
        schema = dict(scalar)
    if param.default is not None:
        return {**schema, "description": description, "default": param.default}
    if param.required:
        return {**schema, "description": description}
    return {"anyOf": [schema, NULL], "description": description}


def _closed(properties: dict[str, JsonValue], required: list[str]) -> JsonObject:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    schema["required"] = required
    return schema


def _control(
    schema: JsonObject, description: str, *, default: JsonValue = None, nullable: bool = False
) -> JsonObject:
    """The schema of a control: ``nullable`` for one that is not required and has no default."""
    if default is not None:
        return {**schema, "description": description, "default": default}
    if nullable:
        return {"anyOf": [schema, NULL], "description": description}
    return {**schema, "description": description}


def _input(
    head: Sequence[tuple[str, JsonObject, bool]],
    params: Sequence[ParamType],
    tail: Sequence[tuple[str, JsonObject, bool]],
    model: CompiledModel,
) -> JsonObject:
    """The closed object of ``head`` controls, the parameters and ``tail`` controls; a control is
    ``(name, schema, required)``."""
    properties: dict[str, JsonValue] = {}
    required: list[str] = []
    for name, schema, needed in head:
        properties[name] = schema
        if needed:
            required.append(name)
    for param in params:
        properties[param.name] = param_schema(param, model)
        if param.required:
            required.append(param.name)
    for name, schema, needed in tail:
        properties[name] = schema
        if needed:
            required.append(name)
    return _closed(properties, required)


def _limit_schema() -> JsonObject:
    return _control(
        {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        f"Rows per page, at most {MAX_LIMIT}.",
        default=DEFAULT_LIMIT,
    )


def _offset_schema() -> JsonObject:
    return _control(
        {"type": "integer", "minimum": 0},
        "Rows to skip; the next page starts at page.next_offset.",
        default=0,
    )


def function_input(entry: FunctionEntry, model: CompiledModel) -> JsonObject:
    """The input of ``run_<function>``: its parameters, then ``_limit`` and ``_offset``."""
    return _input(
        (),
        param_types(entry.defn.params),
        ((LIMIT, _limit_schema(), False), (OFFSET, _offset_schema(), False)),
        model,
    )


def reason_required(entry: ActionEntry, obligations: frozenset[str]) -> bool:
    """Whether a call of the action must give a reason: the action demands one, or a
    ``require_reason`` obligation of the caller's policy applies to one of the write rights its
    ops need (``obligations`` are the write actions the caller's policy demands a reason for)."""
    return entry.defn.reason == "required" or any(n.action in obligations for n in entry.needs)


def action_input(entry: ActionEntry, model: CompiledModel, *, reason_needed: bool) -> JsonObject:
    """The input of ``do_<action>``: its parameters, then ``_reason`` (required when
    ``reason_needed``) and ``_dry_run``."""
    reason = _control(
        # \S: a reason of blanks only is refused, as the binder refuses it
        {"type": "string", "minLength": 1, "maxLength": MAX_REASON, "pattern": r"\S"},
        "Why this change is made; recorded in the audit trail."
        + (" Required for this action." if reason_needed else ""),
        nullable=not reason_needed,
    )
    dry_run = _control(
        {"type": "boolean"},
        "Check the preconditions and report what would change; nothing is written.",
        default=False,
    )
    return _input(
        (),
        param_types(entry.defn.params),
        ((REASON, reason, reason_needed), (DRY_RUN, dry_run, False)),
        model,
    )


def view_input(entry: ViewEntry, model: CompiledModel) -> JsonObject:
    """The input of ``view_<view>``: ``_entity`` first for an entity view (required: a reference
    to an entity of the class it takes), the parameters, then ``_cursor`` and ``_page_bytes`` for
    a set view."""
    params = param_types(entry.defn.params)
    if entry.takes is not None:
        entity = param_schema(ref_type(ENTITY, "The entity to show.", entry.takes), model)
        return _input(((ENTITY, entity, True),), params, (), model)
    cursor = _control(
        {"type": "string", "minLength": 1},
        "The next of the previous page of this view, with the same parameters; leave it out for "
        "the first page.",
        nullable=True,
    )
    page_bytes = _control(
        {"type": "integer", "minimum": MIN_BYTES, "maximum": PAGE_BYTES_MAX},
        f"Page size in bytes, at least {MIN_BYTES}; the view's own size, else {PAGE_BYTES}.",
        nullable=True,
    )
    return _input((), params, ((CURSOR, cursor, False), (PAGE_BYTES_ARG, page_bytes, False)), model)


REFERENCES_NOTE: Final = "Supply the references it needs."


def _example_line(
    question: str, given: JsonObject, params: Sequence[ParamDef], *, entity: bool = False
) -> str:
    """``- <question> Arguments: {...}``: the arguments the example gives, in the form the input
    takes (a decimal as text). An example never gives a reference, so a line that leaves one out
    (a required ``ref`` parameter, the entity of an entity view) says to supply it."""
    kinds = {p.name: p.type for p in params}
    shown = {k: input_value(kinds.get(k, "string"), v) for k, v in given.items()}
    needs = entity or any(p.type == "ref" and p.required and p.name not in given for p in params)
    parts = [f"- {question}"]
    if shown:
        parts.append(f"Arguments: {json.dumps(shown, ensure_ascii=False, sort_keys=True)}")
    if needs:
        parts.append(REFERENCES_NOTE)
    return " ".join(parts)


def description_text(
    summary: str,
    use_when: str,
    examples: Sequence[str],
    *,
    deprecated: bool,
    replaced_by: str | None,
) -> str:
    """The description of a tool: the summary, a deprecation line, when to use it and the
    examples of the entry (functions and views: the question and the parameters as input of the
    tool; actions: the request)."""
    parts = [summary]
    if deprecated:
        parts.append(f"Deprecated; use {replaced_by}." if replaced_by else "Deprecated.")
    parts.append(f"Use when: {use_when}")
    if examples:
        parts.append("Examples:\n" + "\n".join(examples))
    return "\n\n".join(parts)


def _replacement(kind: str, replaced_by: str | None, visible: Sequence[Named]) -> str | None:
    """The tool of the entry that replaces a deprecated one, when the caller may use that one
    too (a name the caller cannot use is not told)."""
    found = seen_replacement(replaced_by, visible)
    return None if found is None else tool_name(kind, found)


def function_columns(entry: FunctionEntry, model: CompiledModel) -> tuple[ToolColumn, ...]:
    """The columns of a function with their types against ``model`` (the caller's model)."""
    types = column_types_of(entry, model)
    return tuple(
        ToolColumn(r.column, r.summary, t) for r, t in zip(entry.defn.returns, types, strict=True)
    )


def function_head(
    entry: FunctionEntry, model: CompiledModel, visible: Sequence[FunctionEntry]
) -> ToolHead:
    defn = entry.defn
    replaced = _replacement(FUNCTION, defn.replaced_by, visible)
    examples = [_example_line(e.question, dict(e.params), defn.params) for e in defn.examples]
    return ToolHead(
        name=tool_name(FUNCTION, entry.name),
        kind=FUNCTION,
        entry=entry.name,
        description=description_text(
            defn.summary, defn.use_when, examples, deprecated=entry.deprecated, replaced_by=replaced
        ),
        input_schema=function_input(entry, model),
        annotations={"read_only": True, "dry_run": False, "reason": None, "paging": "offset"},
        status=defn.status,
        replaced_by=replaced,
        featured=defn.featured,
        module=entry.module,
        entry_hash=entry.hash,
        columns=function_columns(entry, model),
    )


def action_head(
    entry: ActionEntry,
    model: CompiledModel,
    visible: Sequence[ActionEntry],
    obligations: frozenset[str],
) -> ToolHead:
    defn = entry.defn
    replaced = _replacement(ACTION, defn.replaced_by, visible)
    needed = reason_required(entry, obligations)
    examples = [f"- {e.request}" for e in defn.examples]
    return ToolHead(
        name=tool_name(ACTION, entry.name),
        kind=ACTION,
        entry=entry.name,
        description=description_text(
            defn.summary, defn.use_when, examples, deprecated=entry.deprecated, replaced_by=replaced
        ),
        input_schema=action_input(entry, model, reason_needed=needed),
        annotations={
            "read_only": False,
            "dry_run": True,
            "reason": "required" if needed else "optional",
            "paging": None,
        },
        status=defn.status,
        replaced_by=replaced,
        featured=defn.featured,
        module=entry.module,
        entry_hash=entry.hash,
    )


def view_sections(
    entry: ViewEntry,
    model: CompiledModel,
    functions: Sequence[FunctionEntry],
    known: dict[str, tuple[ToolColumn, ...]],
) -> tuple[ToolSection, ...]:
    """The sections of a view; a ``rows`` section carries the columns of its function as the
    caller sees them, found among the ``functions`` the caller may use. A view is listed only for a
    caller who may run the functions of its ``rows`` sections, so describe and call never get
    ``None`` (no such function: the rows stay untyped) from a real catalog; it is the defensive
    answer for an entry that does not fit it. ``known`` holds the columns worked out already, by
    function name, and gets those this call works out: typing a function resolves its query."""
    found: list[ToolSection] = []
    for section in entry.defn.sections:
        columns = None
        if isinstance(section, RowsSection):
            columns = known.get(section.function)
            if columns is None:
                runs = next((f for f in functions if f.name == section.function), None)
                if runs is not None:
                    columns = known[section.function] = function_columns(runs, model)
        named = section.function if isinstance(section, RowsSection) else None
        found.append(ToolSection(section.kind, section.id, named, columns))
    return tuple(found)


def view_head(
    entry: ViewEntry,
    model: CompiledModel,
    visible: Sequence[ViewEntry],
    functions: Sequence[FunctionEntry],
    known: dict[str, tuple[ToolColumn, ...]] | None = None,
) -> ToolHead:
    defn = entry.defn
    replaced = _replacement(VIEW, defn.replaced_by, visible)
    examples = [
        _example_line(e.question, dict(e.params), defn.params, entity=entry.entity)
        for e in defn.examples
    ]
    return ToolHead(
        name=tool_name(VIEW, entry.name),
        kind=VIEW,
        entry=entry.name,
        description=description_text(
            defn.summary, defn.use_when, examples, deprecated=entry.deprecated, replaced_by=replaced
        ),
        input_schema=view_input(entry, model),
        annotations={
            "read_only": True,
            "dry_run": False,
            "reason": None,
            "paging": None if entry.entity else "cursor",
        },
        status=defn.status,
        replaced_by=replaced,
        featured=False,
        module=entry.module,
        entry_hash=entry.hash,
        sections=view_sections(entry, model, functions, {} if known is None else known),
        entity=entry.entity,
    )


def control_names(kind: str, *, entity: bool = False) -> tuple[str, ...]:
    """The controls a tool of ``kind`` takes (``entity``: a view that shows one entity, else a set
    view), in the order its input schema lists them."""
    if kind == FUNCTION:
        return (LIMIT, OFFSET)
    if kind == ACTION:
        return (REASON, DRY_RUN)
    return (ENTITY,) if entity else (CURSOR, PAGE_BYTES_ARG)


def unique_names(heads: Sequence[ToolHead]) -> None:
    """Refuse a manifest in which two tools share a name (the builder's invariant)."""
    seen: dict[str, ToolHead] = {}
    for head in heads:
        other = seen.get(head.name)
        if other is not None:
            raise GraphCliError(
                "internal.error",
                f"two tools are named {head.name}: the {other.kind} {other.entry} and the "
                f"{head.kind} {head.entry}",
            )
        seen[head.name] = head
