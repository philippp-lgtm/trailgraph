"""``graph call``: run one tool of the tool manifest with typed JSON input (tool manifest plan TM7).

``graph describe tools`` hands an agent harness one tool per function, action and view it may use,
each with the JSON Schema of its input. ``graph call TOOL --input NAME=<json> ...`` runs a tool
with exactly the object that schema describes: a ``run_<function>``, ``do_<action>`` or
``view_<view>`` tool, or ``lookup``, which finds the entity a name stands for. ``--input`` is
repeatable like ``--set-json`` (over the server ``input`` is the object itself); the names that
start with an underscore are the tool's controls (``_limit``,
``_offset``, ``_reason``, ``_dry_run``, ``_entity``, ``_cursor``, ``_page_bytes``), the others its
parameters. The input of ``lookup`` is ``text`` (a name or a list of names), ``for`` (the
reference input the entity is for, ``<tool>.<input>``) and ``limit``; the tool runs the service of
``lookup`` and is there for a caller who holds ``read.find`` when some tool takes a reference.

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

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph_cli.app_cli.commands.common import GRAPH, context, services
from graph_cli.app_cli.commands.do import DO_ERRORS, DO_OUTPUT
from graph_cli.app_cli.commands.read.entity import VIEW_ERRORS, VIEW_OUTPUT
from graph_cli.app_cli.commands.read.find import LOOKUP_ERRORS, LOOKUP_PAGE_OUTPUT
from graph_cli.app_cli.commands.read.query import RUN_ERRORS, RUN_PAGE_OUTPUT, table_text
from graph_cli.app_cli.commands.read.shared import open_graph
from graph_cli.app_cli.commands.view_text import view_text
from graph_cli.app_cli.commands.write import check_reason_and_key, usage
from graph_cli.app_cli.tool_manifest import (
    CALL_ERRORS,
    NOT_FOR_LOOKUP,
    NOT_FOR_TOOLS,
    heads,
    lookup_refs,
    missing_tools,
    tool_hash,
)
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import one_of
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, Kind, ParamSpec
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.changeset import MAX_REASON, FloatLiteral, load_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import VIA_CALL
from graph_cli.kinetic.params import number_value
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.kinetic.view_format import PAGE_BYTES_MAX
from graph_cli.query.lookup import MAX_LOOKUP_TEXTS, LookupSpec
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT, split_assignment
from graph_cli.services.catalog_access import Classified, allowed_entries
from graph_cli.services.describe_tools import (
    ACTION,
    CURSOR,
    DRY_RUN,
    ENTITY,
    FUNCTION,
    KINDS,
    LIMIT,
    LOOKUP,
    LOOKUP_FOR,
    LOOKUP_LIMIT,
    LOOKUP_TEXT,
    OFFSET,
    PAGE_BYTES_ARG,
    PREFIXES,
    REASON,
    VIEW,
    RefInput,
    control_names,
    ref_inputs,
    tool_name,
)
from graph_cli.services.read import ReadService, graph_json

INPUT_NAME_RE: Final = re.compile(r"^_?[a-z][a-z0-9_]*$")  # a parameter or a control
INPUT: Final = "--input"
KEY: Final = "--idempotency-key"
RAN: Final = {FUNCTION: "run", ACTION: "do", VIEW: "view", LOOKUP: "lookup"}  # what a tool runs


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


def _kind_of(tool: str) -> str | None:
    """The kind of entry the prefix of a tool name says, ``None`` when it has none of them."""
    return next((kind for kind, prefix in PREFIXES.items() if tool.startswith(prefix)), None)


def _classified(
    reader: ReadService, actor: ActorContext, entry: GraphEntry, kind: str
) -> Sequence[Classified[Any]]:
    """What the caller sees of the catalog of ``kind``, with the rights it lacks."""
    if kind == FUNCTION:
        return reader.classified_functions(actor, entry)
    if kind == VIEW:
        return reader.classified_views(actor, entry)
    published = reader.published_actions(entry)
    return () if published is None else reader.classified_actions(actor, entry, published)


def _find(tool: str, kind: str, seen: Sequence[Classified[Any]]) -> _Found | None:
    """The entry of ``tool`` among what the caller sees of its catalog (names are unique by
    construction)."""
    for item in seen:
        if tool_name(kind, item.entry.name) == tool:
            entity = isinstance(item.entry, ViewEntry) and item.entry.entity
            return _Found(kind, item.entry.name, item.allowed, entity)
    return None


def _usable(
    reader: ReadService, actor: ActorContext, entry: GraphEntry
) -> dict[str, tuple[Any, ...]]:
    """The entries of each kind the caller may use."""
    return {kind: tuple(allowed_entries(_classified(reader, actor, entry, kind))) for kind in KINDS}


def _lookup_refs(
    reader: ReadService, actor: ActorContext, entry: GraphEntry, usable: Mapping[str, Sequence[Any]]
) -> tuple[RefInput, ...]:
    """The reference inputs the ``lookup`` tool serves for the caller (it is listed when there are
    some and the caller may find entities)."""
    if not reader.may(actor, entry, "read.find"):
        return ()  # the model is read only when it matters
    model = reader.visible_model(actor, entry)
    return lookup_refs(model, usable[FUNCTION], usable[ACTION], usable[VIEW], find=True)


def _usable_names(reader: ReadService, actor: ActorContext, entry: GraphEntry) -> list[str]:
    """The names of every tool the caller may use, for the suggestions of a name that is none."""
    usable = _usable(reader, actor, entry)
    names = [tool_name(kind, e.name) for kind in KINDS for e in usable[kind]]
    return [*names, LOOKUP] if _lookup_refs(reader, actor, entry, usable) else names


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
    # a number with a fraction is a FloatLiteral, which is a str: no string either
    if value is not None and (not isinstance(value, str) or isinstance(value, FloatLiteral)):
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
    if tool == LOOKUP:
        return _lookup(state, graph, entry, values, expect_hash)
    kind = _kind_of(tool)
    seen = () if kind is None else _classified(reader, actor, entry, kind)
    found = None if kind is None else _find(tool, kind, seen)
    if kind is None or found is None:
        problem = missing_tools([tool], _usable_names(reader, actor, entry))
        assert problem is not None
        raise problem
    services(state).usage.ran = RAN[kind]  # from here on the log says what ran
    if expect_hash is not None and found.allowed:  # one the caller may not use is refused below
        obligations = reader.reason_obligations(actor, entry) if kind == ACTION else frozenset()
        model = reader.visible_model(actor, entry)
        # the other kinds only when they decide whether the lookup tool is listed (and so what
        # the tool says of its references), or type the rows of a view
        every = reader.may(actor, entry, "read.find")
        none: tuple[Any, ...] = ()
        parts = {k: none for k in KINDS}
        for part in KINDS:
            if every or part == kind or (kind == VIEW and part == FUNCTION):
                parts[part] = tuple(
                    allowed_entries(
                        seen if part == kind else _classified(reader, actor, entry, part)
                    )
                )
        refs = lookup_refs(model, parts[FUNCTION], parts[ACTION], parts[VIEW], find=every)
        (head,) = heads(
            model, parts[FUNCTION], parts[ACTION], parts[VIEW], obligations, only=tool, refs=refs
        )
        _check_hash(tool, expect_hash, tool_hash(head, model))
    params, controls = _split(values, found)
    if found.kind == FUNCTION:
        return _run(state, graph, found.name, params, controls)
    if found.kind == ACTION:
        return _do(state, graph, found.name, params, controls, idempotency_key)
    return _view(state, graph, found, params, controls)


def _check_hash(tool: str, expected: str, current: str) -> None:
    """``tool.hash_mismatch`` unless the tool is the one the caller built its call for."""
    if current != expected:
        raise GraphCliError(
            "tool.hash_mismatch",
            f"tool {tool} is not the one this call was built for: its hash is now {current}",
            details={"tool": tool, "expected": expected, "actual": current},
        )


def _lookup(
    state: CommandState,
    graph: str | None,
    entry: GraphEntry,
    values: Mapping[str, JsonValue],
    expect_hash: str | None,
) -> Result:
    """A call of the ``lookup`` tool: the lookup service for the entity a name stands for, for the
    reference input ``for`` names. The tool is there only when some tool takes a reference; a
    caller that may not find entities gets the ``access.denied`` of ``lookup``."""
    ctx = context(state)
    reader, actor = ctx.reader, ctx.actor
    usable = _usable(reader, actor, entry)
    model = reader.visible_model(actor, entry)
    refs = ref_inputs(model, usable[FUNCTION], usable[ACTION], usable[VIEW])
    if not refs:
        problem = missing_tools([LOOKUP], _usable_names(reader, actor, entry))
        assert problem is not None
        raise problem
    services(state).usage.ran = RAN[LOOKUP]
    if expect_hash is not None and reader.may(actor, entry, "read.find"):
        (head,) = heads(model, (), (), (), frozenset(), only=LOOKUP, refs=refs)
        _check_hash(LOOKUP, expect_hash, tool_hash(head, model))
    texts, slot, limit = _lookup_input(values, refs)
    args: dict[str, JsonValue] = {
        "text": list(texts),
        "type": [],
        "for": slot.lookup_for,
        "for_source": None,
        "match": "exact",
        "ignore_accents": False,
        "include_inactive": False,
        "lang": None,
        "limit": limit,
        "offset": 0,
    }
    view = open_graph(state, graph, "read.find", "lookup", args, None, via=VIA_CALL)
    spec = LookupSpec(text=texts[0], limit=limit)
    data, page = reader.lookup(view, spec, texts, for_use=slot.lookup_for)
    return Result(data={**data, "page": page.to_json()}, graph=view.envelope_graph())


def _lookup_input(
    values: Mapping[str, JsonValue], refs: Sequence[RefInput]
) -> tuple[tuple[str, ...], RefInput, int]:
    """The names, the reference input and the limit of a call of ``lookup``; ``null`` is not given,
    anything else the input schema refuses is ``usage.invalid_argument``."""
    known = (LOOKUP_TEXT, LOOKUP_FOR, LOOKUP_LIMIT)
    for name in values:
        if name not in known:
            raise GraphCliError(
                "usage.invalid_argument",
                f"{name}: {LOOKUP} has no such input; it takes {', '.join(known)}",
                details={"argument": name, "expected": list(known)},
            )
    names = values.get(LOOKUP_TEXT)
    slot = values.get(LOOKUP_FOR)
    for given, argument in ((names, LOOKUP_TEXT), (slot, LOOKUP_FOR)):
        if given is None:
            raise GraphCliError(
                "usage.missing_argument",
                f"{argument} is required by the {LOOKUP} tool",
                details={"argument": argument},
            )
    texts = names if isinstance(names, list) else [names]
    if not 1 <= len(texts) <= MAX_LOOKUP_TEXTS:
        raise usage(LOOKUP_TEXT, f"expected 1 to {MAX_LOOKUP_TEXTS} names", str(len(texts)))
    for text in texts:
        if not isinstance(text, str) or isinstance(text, FloatLiteral):
            raise usage(LOOKUP_TEXT, "expected a name or a list of names", str(text))
        if not text.strip():
            raise usage(LOOKUP_TEXT, "a name is blank", text)
    found = next((r for r in refs if r.value == slot), None)
    if found is None:
        close = difflib.get_close_matches(str(slot), [r.value for r in refs], n=5)
        raise GraphCliError(
            "usage.invalid_argument",
            f"{LOOKUP_FOR}: {slot!r} is none of the reference inputs of your tools",
            details={"argument": LOOKUP_FOR, "actual": str(slot), "did_you_mean": close},
        )
    limit = _integer(values, LOOKUP_LIMIT, DEFAULT_LIMIT, 1, MAX_LIMIT)
    return tuple(str(t) for t in texts), found, limit


def _digestible(value: JsonValue) -> JsonValue:
    """``value`` for the digest of a request: a number written with a fraction (a ``FloatLiteral``,
    a ``str``) as ``{"number": text}``, so that ``5.0`` and the text ``"5.0"`` differ."""
    if isinstance(value, FloatLiteral):
        return {"number": str(value)}
    if isinstance(value, Mapping):
        return {k: _digestible(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_digestible(v) for v in value]
    return value


def _run(
    state: CommandState,
    graph: str | None,
    name: str,
    params: dict[str, JsonValue],
    controls: dict[str, JsonValue],
) -> Result:
    limit = _integer(controls, LIMIT, DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _integer(controls, OFFSET, 0, 0, MAX_SAFE_INT)
    asked = _digestible(params)
    args: dict[str, JsonValue] = {"name": name, "input": asked, "limit": limit, "offset": offset}
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


def _as_control(exc: GraphCliError) -> GraphCliError:
    """``exc`` naming the control ``_entity`` where the view service names its option ``ref``."""
    if exc.details.get("argument") != "ref":
        return exc
    return GraphCliError(
        exc.code,
        exc.message.replace("ref:", f"{ENTITY}:", 1),
        details={**exc.details, "argument": ENTITY},
        warnings=exc.warnings,
    )


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
    page_bytes = None if given is None else _integer(controls, PAGE_BYTES_ARG, 0, 1, PAGE_BYTES_MAX)
    args: dict[str, JsonValue] = {"name": name, "param": sorted(params)}
    if ref is not None:
        args["ref"] = ref
    reading = open_graph(state, graph, "read.get", "view", args, None, via=VIA_CALL)
    try:
        data, warnings = context(state).reader.view(
            reading,
            ref,
            name=name,
            pairs=params,
            cursor=cursor,
            page_bytes=page_bytes,
        )
    except GraphCliError as exc:
        raise _as_control(exc) from None
    state.warnings += warnings
    return Result(data=data, graph=reading.envelope_graph(), text=view_text(data))


CALL_OUTPUT: Final = one_of(RUN_PAGE_OUTPUT, DO_OUTPUT, VIEW_OUTPUT, LOOKUP_PAGE_OUTPUT)
# every code a tool may return (the manifest's per-tool lists are subsets), and the graph choice
_ERRORS: Final = tuple(
    sorted(
        (
            {*RUN_ERRORS, *DO_ERRORS, *VIEW_ERRORS, *LOOKUP_ERRORS, *CALL_ERRORS}
            - NOT_FOR_TOOLS
            - NOT_FOR_LOOKUP
        )
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
            "do_NAME, view_NAME or lookup and each --input NAME=<json> is one member of the input "
            "object of its input_schema (over the server, args.input is that object). Names "
            "that start with an underscore are the tool's controls: _limit and _offset of a "
            "function, _reason and _dry_run of an action, _entity of an entity view, _cursor and "
            "_page_bytes of a set view. null means not given; a whole number written as 5.0 is "
            "an integer. It runs what run, do, view or lookup runs, with the same answers and "
            "errors; the page of a function or a lookup is in data. The lookup tool takes text "
            "(a name or a list of names), for (the reference input the entity is for, as "
            "TOOL.INPUT) and limit. A tool you may not see is tool.not_found, one "
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
            Example(
                "graph call --input text='\"<value>\"' --input for='\"<name>.<param>\"' lookup "
                "--json",
                "Find the entity a name stands for, to give a tool as a reference.",
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
