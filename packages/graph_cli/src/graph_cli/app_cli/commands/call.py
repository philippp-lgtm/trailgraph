"""``graph call``: run one tool of the tool manifest with typed JSON input (tool manifest plan TM7).

``graph describe tools`` hands an agent harness one tool per function, action and view it may use,
each with the JSON Schema of its input. ``graph call TOOL --input NAME=<json> ...`` runs a tool
with exactly the object that schema describes: a ``run_<function>``, ``do_<action>`` or
``view_<view>`` tool. ``--input`` is repeatable like ``--set-json`` (over the server ``input`` is
the object itself); the names that start with an underscore are the tool's controls (``_limit``,
``_offset``, ``_reason``, ``_dry_run``, ``_entity``, ``_cursor``, ``_page_bytes``), the others its
parameters.

The call resolves the tool among the entries the caller sees: one it does not see is
``tool.not_found``, one it sees but may not use gives the answer the command gives
(``access.denied``). It then runs the service of ``run``, ``do`` or ``view`` and binds the input by
the declared types (``services.refs.bind_params``): ``null`` is "not given", a whole number written
as a float is an integer. The audit records the underlying command and the entry's hash as for the
command itself, plus ``request.via: call``, and the access log the command it ran.

A call is a write only for an action that is not a dry run (``call_kind``): only that one takes an
idempotency key, and the remote client sends it once, with a key. The answer of a function carries
its ``page`` in ``data``; a call never moves its answer to a file, so ``data`` is never ``null``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, context, services
from graph_cli.app_cli.commands.do import DO_ERRORS, DO_OUTPUT
from graph_cli.app_cli.commands.read.entity import VIEW_ERRORS, VIEW_OUTPUT
from graph_cli.app_cli.commands.read.query import RUN_ERRORS, RUN_PAGE_OUTPUT, table_text
from graph_cli.app_cli.commands.read.shared import open_graph
from graph_cli.app_cli.commands.view_text import view_text
from graph_cli.app_cli.commands.write import check_reason_and_key, usage
from graph_cli.app_cli.tool_manifest import (
    CALL_ERRORS,
    NOT_FOR_TOOLS,
    heads,
    missing_tools,
    tool_hash,
)
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import one_of
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, Kind, ParamSpec
from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.changeset import MAX_REASON, load_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import VIA_CALL
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.params import number_value
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT, split_assignment
from graph_cli.services.catalog_access import Classified, allowed_entries
from graph_cli.services.describe_tools import (
    ACTION,
    CURSOR,
    DRY_RUN,
    ENTITY,
    FUNCTION,
    LIMIT,
    OFFSET,
    PAGE_BYTES_ARG,
    PREFIXES,
    REASON,
    VIEW,
    control_names,
    tool_name,
)
from graph_cli.services.read import graph_json

INPUT_NAME_RE: Final = re.compile(r"^_?[a-z][a-z0-9_]*$")  # a parameter or a control
INPUT: Final = "--input"
KEY: Final = "--idempotency-key"


def parse_input(pairs: Sequence[str]) -> dict[str, JsonValue]:
    """The object of ``--input NAME=<json>`` pairs; a name at most once."""
    values: dict[str, JsonValue] = {}
    for text in pairs:
        name, raw = split_assignment(text, INPUT)
        if INPUT_NAME_RE.fullmatch(name) is None:
            raise usage(INPUT, f"{name!r} is not the name of an input", text)
        if name in values:
            raise usage(INPUT, f"{name!r} is given twice", text)
        try:
            values[name] = load_document(raw)
        except GraphCliError as exc:
            raise usage(INPUT, f"{name!r} is not valid JSON: {exc.message}", raw) from None
    return values


def writes(tool: str, dry_run: bool) -> bool:
    """Whether a call of ``tool`` writes: an action that is not a dry run."""
    return tool.startswith(PREFIXES[ACTION]) and not dry_run


def call_kind(args: Mapping[str, JsonValue]) -> Kind:
    """The kind of one call from its arguments (by inventory name): a write for a ``do_*`` tool
    without ``_dry_run``, a read otherwise. It reads the raw ``--input`` and only the one control;
    anything else wrong with the input is the handler's to report."""
    tool = args.get("tool")
    if not isinstance(tool, str):
        return "read"
    return "write" if writes(tool, _dry_run_given(args.get(INPUT))) else "read"


def _dry_run_given(given: JsonValue) -> bool:
    """Whether the ``--input`` of a call (its ``NAME=<json>`` texts, or the object of the server
    form) says ``_dry_run`` is true."""
    if isinstance(given, Mapping):
        return given.get(DRY_RUN) is True
    if isinstance(given, list):
        for item in given:
            name, _, raw = str(item).partition("=")
            if name == DRY_RUN:
                try:
                    return json.loads(raw) is True
                except ValueError:
                    return False
    return False


@dataclass(frozen=True, slots=True)
class _Found:
    """The entry a tool name stands for, and whether the caller may use it."""

    kind: str
    name: str
    allowed: bool
    entity: bool = False  # a view that shows one entity


def _find(
    tool: str,
    functions: Sequence[Classified[FunctionEntry]],
    actions: Sequence[Classified[ActionEntry]],
    views: Sequence[Classified[ViewEntry]],
) -> _Found | None:
    """The entry of ``tool`` among everything the caller sees (names are unique by construction)."""
    for fn in functions:
        if tool_name(FUNCTION, fn.entry.name) == tool:
            return _Found(FUNCTION, fn.entry.name, fn.allowed)
    for action in actions:
        if tool_name(ACTION, action.entry.name) == tool:
            return _Found(ACTION, action.entry.name, action.allowed)
    for shown in views:
        if tool_name(VIEW, shown.entry.name) == tool:
            return _Found(VIEW, shown.entry.name, shown.allowed, shown.entry.entity)
    return None


def _integer(
    controls: Mapping[str, JsonValue], name: str, default: int, low: int, high: int
) -> int:
    value = controls.get(name)
    if value is None:
        return default
    value = number_value(value, integer=True)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise usage(name, f"expected a whole number from {low} to {high}", str(value))
    return value


def _text(controls: Mapping[str, JsonValue], name: str) -> str | None:
    value = controls.get(name)
    if value is not None and not isinstance(value, str):
        raise usage(name, "expected a string", str(value))
    return value


def _flag(controls: Mapping[str, JsonValue], name: str) -> bool:
    value = controls.get(name)
    if value is not None and not isinstance(value, bool):
        raise usage(name, "expected true or false", str(value))
    return bool(value)


def _split(
    values: Mapping[str, JsonValue], found: _Found
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """The parameters and the controls of the input. A control the tool does not have is refused;
    a control that is ``null`` is not given."""
    known = control_names(found.kind, entity=found.entity)
    params: dict[str, JsonValue] = {}
    controls: dict[str, JsonValue] = {}
    for name, value in values.items():
        if not name.startswith("_"):
            params[name] = value
        elif name not in known:
            raise GraphCliError(
                "usage.invalid_argument",
                f"{name}: {tool_name(found.kind, found.name)} has no such control"
                + (f"; it takes {', '.join(known)}" if known else ""),
                details={"argument": name, "expected": list(known)},
            )
        elif value is not None:
            controls[name] = value
    return params, controls


def call(
    state: CommandState,
    tool: str,
    inputs: tuple[str, ...],
    expect_hash: str | None,
    idempotency_key: str | None,
    graph: str | None,
) -> Result:
    values = parse_input(inputs)
    check_reason_and_key(None, idempotency_key)
    if idempotency_key is not None and not writes(tool, values.get(DRY_RUN) is True):
        raise usage(
            KEY,
            "only an action that is not a dry run takes an idempotency key; "
            "a read repeats safely without one",
            idempotency_key,
        )
    ctx = context(state)
    entry = ctx.select(graph)
    reader, actor = ctx.reader, ctx.actor
    functions = reader.classified_functions(actor, entry)
    published = reader.published_actions(entry)
    actions = () if published is None else reader.classified_actions(actor, entry, published)
    views = reader.classified_views(actor, entry)
    found = _find(tool, functions, actions, views)
    if found is None:
        names = [
            *(tool_name(FUNCTION, e.name) for e in allowed_entries(functions)),
            *(tool_name(ACTION, e.name) for e in allowed_entries(actions)),
            *(tool_name(VIEW, e.name) for e in allowed_entries(views)),
        ]
        problem = missing_tools([tool], names)
        assert problem is not None
        raise problem
    if expect_hash is not None and found.allowed:  # one the caller may not use is refused below
        usable = (allowed_entries(functions), allowed_entries(actions), allowed_entries(views))
        obligations = reader.reason_obligations(actor, entry) if usable[1] else frozenset[str]()
        manifest = heads(reader.visible_model(actor, entry), *usable, obligations)
        current = tool_hash(next(h for h in manifest if h.name == tool))
        if current != expect_hash:
            raise GraphCliError(
                "tool.hash_mismatch",
                f"tool {tool} is not the one this call was built for: its hash is now {current}",
                details={"tool": tool, "expected": expect_hash, "actual": current},
            )
    params, controls = _split(values, found)
    note = services(state).usage
    if found.kind == FUNCTION:
        note.ran = "run"
        return _run(state, graph, found.name, params, controls)
    if found.kind == ACTION:
        note.ran = "do"
        return _do(state, graph, found.name, params, controls, idempotency_key)
    note.ran = "view"
    return _view(state, graph, found, params, controls)


def _run(
    state: CommandState,
    graph: str | None,
    name: str,
    params: dict[str, JsonValue],
    controls: dict[str, JsonValue],
) -> Result:
    limit = _integer(controls, LIMIT, DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _integer(controls, OFFSET, 0, 0, MAX_SAFE_INT)
    args: dict[str, JsonValue] = {"name": name, "input": params, "limit": limit, "offset": offset}
    view = open_graph(state, graph, "read.function", "run", args, None, via=VIA_CALL)
    data, page, warnings = context(state).reader.run(view, name, params, limit=limit, offset=offset)
    state.warnings += warnings
    columns, items = data["columns"], data["items"]
    assert isinstance(columns, list) and isinstance(items, list)
    return Result(
        data={**data, "page": page.to_json()},
        graph=view.envelope_graph(),
        text=table_text([str(c) for c in columns], items),
    )


def _do(
    state: CommandState,
    graph: str | None,
    name: str,
    params: dict[str, JsonValue],
    controls: dict[str, JsonValue],
    idempotency_key: str | None,
) -> Result:
    reason = _text(controls, REASON)
    dry_run = _flag(controls, DRY_RUN)
    if reason is not None and (not reason.strip() or len(reason) > MAX_REASON):
        raise usage(REASON, f"expected 1 to {MAX_REASON} characters")
    ctx = context(state)
    entry = ctx.select(graph)
    result = ctx.actions.do(
        ctx.actor,
        entry,
        name,
        params,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        via=VIA_CALL,
    )
    state.warnings += (*result.warnings, *result.write.warnings)
    return Result(data=result.to_json(), graph=graph_json(entry, result.write.seq))


def _view(
    state: CommandState,
    graph: str | None,
    found: _Found,
    params: dict[str, JsonValue],
    controls: dict[str, JsonValue],
) -> Result:
    name = found.name
    ref = _text(controls, ENTITY)
    if found.entity and ref is None:  # the entity is the one required control
        raise GraphCliError(
            "usage.missing_argument",
            f"{ENTITY} is required: the entity {tool_name(VIEW, name)} shows",
            details={"argument": ENTITY},
        )
    cursor = _text(controls, CURSOR)
    given = controls.get(PAGE_BYTES_ARG)
    page_bytes = None if given is None else _integer(controls, PAGE_BYTES_ARG, 0, 1, MAX_SAFE_INT)
    args: dict[str, JsonValue] = {"name": name, "param": sorted(params)}
    if ref is not None:
        args["ref"] = ref
    reading = open_graph(state, graph, "read.get", "view", args, None, via=VIA_CALL)
    data, warnings = context(state).reader.view(
        reading,
        ref,
        name=name,
        pairs=params,
        cursor=cursor,
        page_bytes=page_bytes,
    )
    state.warnings += warnings
    return Result(data=data, graph=reading.envelope_graph(), text=view_text(data))


CALL_OUTPUT: Final = one_of(RUN_PAGE_OUTPUT, DO_OUTPUT, VIEW_OUTPUT)
# every code a tool may return (the manifest's per-tool lists are subsets), and the graph choice
_ERRORS: Final = tuple(
    sorted(
        ({*RUN_ERRORS, *DO_ERRORS, *VIEW_ERRORS, *CALL_ERRORS} - NOT_FOR_TOOLS)
        | {"graph.not_selected"}
    )
)

SPECS: Final = (
    CommandSpec(
        path=("call",),
        kind="write",  # the strongest it can be; kind_by_args makes it a read where it is one
        surface="act",
        action=None,  # the command it runs authorizes
        summary="Run one tool of the tool manifest with typed JSON input.",
        description=(
            "For agent harnesses that built their tools from describe tools: TOOL is run_NAME, "
            "do_NAME or view_NAME and each --input NAME=<json> is one member of the input "
            "object of its input_schema (over the server, args.input is that object). Names "
            "that start with an underscore are the tool's controls: _limit and _offset of a "
            "function, _reason and _dry_run of an action, _entity of an entity view, _cursor and "
            "_page_bytes of a set view. null means not given; a whole number written as 5.0 is "
            "an integer. It runs what run, do or view runs, with the same answers and errors; "
            "the page of a function is in data. A tool you may not see is tool.not_found, one "
            "you may not use access.denied. --expect-hash makes the call refuse "
            "(tool.hash_mismatch) when the tool is no longer the one with that hash in the "
            "manifest. An action that is not a dry run takes --idempotency-key; any other call "
            "is a read and refuses one. A call never writes its answer to a file."
        ),
        params=(
            ParamSpec(
                "tool", "string", "The tool name from the manifest.", required=True, positional=True
            ),
            ParamSpec(
                INPUT,
                "json_assignment",
                "NAME=<json>: one member of the tool's input object, a JSON value; repeat for "
                "more.",
                repeatable=True,
                pairs="map",
                keyword="inputs",
            ),
            ParamSpec(
                "--expect-hash", "string", "Refuse unless the tool's hash in the manifest is this."
            ),
            ParamSpec(KEY, "string", "Key that makes a retry of an action safe."),
            GRAPH,
        ),
        examples=(
            Example(
                "graph call <name> --input <name>=<json> --json",
                "Run a tool of the manifest: run_NAME, do_NAME or view_NAME.",
            ),
            Example(
                "graph call <name> --input <name>=<json> --input _dry_run=true --json",
                "See what an action would change.",
            ),
            Example(
                "graph call <name> --input <name>=<json> --input _reason='\"<value>\"' "
                "--idempotency-key <value> --json",
                "Run an action that needs a reason, safe to repeat.",
            ),
            Example(
                "graph call <name> --input _entity='\"<id>\"' --expect-hash <value> --json",
                "Show an entity, only if the tool is still the one the manifest described.",
            ),
        ),
        errors=_ERRORS,
        handler=call,
        output=CALL_OUTPUT,
        skill_table=False,  # for agent harnesses; shell agents have run, do and view
        spill=False,
        kind_by_args=call_kind,
    ),
)
