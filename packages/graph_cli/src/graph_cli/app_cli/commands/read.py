"""``graph get``, ``graph history``, ``graph find``, ``graph lookup`` and ``graph neighbors``.

Contracts §4.3, B8.8, B8.12, B8.15, B9.7.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Final

from graph_cli.app_cli.commands.common import AS_OF, GRAPH, PROGRAM, context
from graph_cli.app_cli.commands.meta import GRAPH_ERRORS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.lookup import MATCH_KINDS, LookupSpec
from graph_cli.query.plan import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FindSpec,
    check_paging,
    parse_rel_flag,
)
from graph_cli.services.read import GraphRead, refuse_as_of

REF_ERRORS: Final = (
    *GRAPH_ERRORS,
    "entity.not_found",
    "entity.ambiguous_ref",
    "temporal.as_of_unsupported",
)


def _open(
    state: CommandState, graph: str | None, action: str, command: str, args: dict[str, JsonValue]
) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    view = ctx.reader.open(
        ctx.actor, entry, action, RequestMeta.of(command, {"graph": entry.name, **args})
    )
    state.warnings += view.warnings
    return view


def _command_line(
    command: str,
    positional: tuple[str, ...],
    graph: str,
    options: tuple[tuple[str, tuple[str, ...]], ...],
    switches: tuple[str, ...],
    limit: int,
    offset: int | None = None,
) -> str:
    """A copy-pasteable read command; ``offset`` given for the next page."""
    parts = [PROGRAM, command, *positional, "--graph", graph]
    for flag, values in options:
        for value in values:
            parts += [flag, value]
    parts += switches
    if limit != DEFAULT_LIMIT or offset is not None:
        parts += ["--limit", str(limit)]
    if offset is not None:
        parts += ["--offset", str(offset)]
    return shlex.join(parts)


def _next_page(page: dict[str, JsonValue], line: Callable[[int], str]) -> tuple[Hint, ...]:
    next_offset = page["next_offset"]
    if not isinstance(next_offset, int):
        return ()
    return ({"text": "More results available", "command": line(next_offset)},)


def get(
    state: CommandState, ref: str, rev: int | None, graph: str | None, as_of: str | None
) -> Result:
    refuse_as_of(as_of)
    args: dict[str, JsonValue] = {"ref": ref} if rev is None else {"ref": ref, "rev": rev}
    view = _open(state, graph, "read.get", "get", args)
    ctx = context(state)
    if rev is not None:
        # An earlier revision is history: read.get and read.history (B8.15).
        request = RequestMeta.of("get", {"graph": view.entry.name, **args})
        ctx.reader.authorize(
            ctx.actor, "read.history", f"graph:{view.entry.id}", request, view.entry
        )
    data, warnings = ctx.reader.get(view, ref, rev=rev)
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


def history(
    state: CommandState, ref: str, limit: int, offset: int, graph: str | None, as_of: str | None
) -> Result:
    refuse_as_of(as_of)
    check_paging(limit, offset)
    view = _open(
        state, graph, "read.history", "history", {"ref": ref, "limit": limit, "offset": offset}
    )
    ctx = context(state)
    data, page, warnings = ctx.reader.history(view, ctx.actor, ref, limit=limit, offset=offset)
    state.warnings += warnings
    page_json = page.to_json()

    def line(next_offset: int) -> str:
        return _command_line("history", (ref,), view.entry.name, (), (), limit, next_offset)

    hints = _next_page(page_json, line)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def find(
    state: CommandState,
    type: tuple[str, ...],
    where: tuple[str, ...],
    rel: tuple[str, ...],
    in_rel: tuple[str, ...],
    include_inactive: bool,
    sort: tuple[str, ...],
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    refuse_as_of(as_of)
    args: dict[str, JsonValue] = {
        "type": list(type),
        "where": list(where),
        "rel": list(rel),
        "in_rel": list(in_rel),
        "include_inactive": include_inactive,
        "sort": list(sort),
        "limit": limit,
        "offset": offset,
    }
    rels = [parse_rel_flag(text, "--rel") for text in rel]
    in_rels = [parse_rel_flag(text, "--in-rel") for text in in_rel]
    view = _open(state, graph, "read.find", "find", args)
    spec = FindSpec(
        types=type,
        where=where,
        include_inactive=include_inactive,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    data, page, warnings = context(state).reader.find(
        view, spec, rel_refs=rels, in_rel_refs=in_rels
    )
    state.warnings += warnings
    page_json = page.to_json()
    options = (
        ("--type", type),
        ("--where", where),
        ("--rel", rel),
        ("--in-rel", in_rel),
        ("--sort", sort),
    )
    switches = ("--include-inactive",) if include_inactive else ()

    def line(next_offset: int) -> str:
        return _command_line("find", (), view.entry.name, options, switches, limit, next_offset)

    hints = _next_page(page_json, line)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def lookup(
    state: CommandState,
    text: str,
    type: tuple[str, ...],
    match: str,
    ignore_accents: bool,
    include_inactive: bool,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    refuse_as_of(as_of)
    args: dict[str, JsonValue] = {
        "text": text,
        "type": list(type),
        "match": match,
        "ignore_accents": ignore_accents,
        "include_inactive": include_inactive,
        "limit": limit,
        "offset": offset,
    }
    view = _open(state, graph, "read.find", "lookup", args)
    spec = LookupSpec(
        text=text,
        types=type,
        match=match,
        ignore_accents=ignore_accents,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    data, page = context(state).reader.lookup(view, spec)
    page_json = page.to_json()
    switches = tuple(
        flag
        for flag, on in (
            ("--ignore-accents", ignore_accents),
            ("--include-inactive", include_inactive),
        )
        if on
    )

    def line(match_kind: str, next_offset: int | None = None) -> str:
        options = (("--type", type), ("--match", (match_kind,) if match_kind != "exact" else ()))
        return _command_line(
            "lookup", (text,), view.entry.name, options, switches, limit, next_offset
        )

    hints = _next_page(page_json, lambda next_offset: line(match, next_offset))
    if not page.total and match == "exact":
        hints = (
            {
                "text": "No exact match; try names that start with the text",
                "command": line("prefix"),
            },
        )
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def neighbors(
    state: CommandState,
    ref: str,
    rel: tuple[str, ...],
    direction: str,
    depth: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    refuse_as_of(as_of)
    args: dict[str, JsonValue] = {
        "ref": ref,
        "rel": list(rel),
        "direction": direction,
        "depth": depth,
    }
    view = _open(state, graph, "read.traverse", "neighbors", args)
    data, warnings = context(state).reader.neighbors(
        view, ref, rels=rel, direction=direction, depth=depth
    )
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


Hint = dict[str, JsonValue]

REF: Final = ParamSpec(
    "ref", "entity_ref", "Full id, alias or trailing hex of the id.", required=True, positional=True
)

SPECS: Final = (
    CommandSpec(
        path=("get",),
        kind="read",
        surface="retrieve",
        action="read.get",
        summary="Show one entity by id, alias or id suffix.",
        description=(
            "With --rev, the entity as it was at that revision; that also needs the "
            "permission to read history."
        ),
        params=(
            REF,
            ParamSpec("--rev", "integer", "Show this earlier revision instead of the current one."),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph get <id>", "Show one entity."),
            Example("graph get <id> --graph <graph> --json", "The entity as JSON."),
            Example("graph get <id> --rev 2 --json", "The entity as it was at revision 2."),
        ),
        errors=(*REF_ERRORS, "entity.revision_not_found"),
        handler=get,
    ),
    CommandSpec(
        path=("history",),
        kind="read",
        surface="trace",
        action="read.history",
        summary="List how one entity changed, newest first: when, by which command and why.",
        description=(
            "Each revision names the fields and relations that changed, not their values; "
            "read a revision with get --rev. own_session tells whether your session wrote it."
        ),
        params=(
            REF,
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many revisions.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph history <id> --json", "The revisions of one entity."),
            Example("graph history <id> --limit 5 --json", "The five most recent revisions."),
        ),
        errors=REF_ERRORS,
        handler=history,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("find",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Find entities by type, field filters and relations; paged.",
        params=(
            ParamSpec("--type", "term", "Type CURIE, subtypes included.", repeatable=True),
            ParamSpec(
                "--where",
                "filter",
                "attr=value or attr:op=value (ne lt le gt ge contains prefix in exists).",
                repeatable=True,
            ),
            ParamSpec(
                "--rel", "string", "Outgoing relation, optionally =<id>[,<id>...].", repeatable=True
            ),
            ParamSpec(
                "--in-rel",
                "string",
                "Incoming relation, optionally =<id>[,<id>...].",
                repeatable=True,
            ),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec("--sort", "string", "Field to sort by, optionally :desc.", repeatable=True),
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph find --type <type> --where <attr>=<value>", "Filter by a field."),
            Example(
                "graph find --type <type> --rel <relation>=<id> --json", "Filter by a relation."
            ),
            Example(
                "graph find --type <type> --rel <relation>=<id>,<id> --json",
                "An edge to any of the listed entities.",
            ),
        ),
        errors=(*REF_ERRORS, "term.unknown"),
        handler=find,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("lookup",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Find entities by name: main, alternative and hidden labels; ranked candidates.",
        description=(
            "Matches case- and Unicode-insensitively. Labels are not identifiers: use the "
            "returned id for get, writes and change sets."
        ),
        params=(
            ParamSpec("text", "string", "The name to look for.", required=True, positional=True),
            ParamSpec("--type", "term", "Type CURIE, subtypes included.", repeatable=True),
            ParamSpec(
                "--match",
                "choice",
                "Whole name, or names that start with the text.",
                choices=MATCH_KINDS,
                default="exact",
            ),
            ParamSpec(
                "--ignore-accents",
                "flag",
                "Also match with accents and other combining marks ignored.",
            ),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph lookup <value>", "Entities named exactly so, any type."),
            Example(
                "graph lookup <value> --type <type> --match prefix --json",
                "Names of one type that start with the text.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "temporal.as_of_unsupported", "term.unknown", "term.no_labels"),
        handler=lookup,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("neighbors",),
        kind="read",
        surface="retrieve",
        action="read.traverse",
        summary="Show the entities connected to one entity, up to three hops.",
        params=(
            REF,
            ParamSpec("--rel", "term", "Only these relations.", repeatable=True),
            ParamSpec(
                "--direction",
                "choice",
                "Edge direction.",
                choices=("out", "in", "both"),
                default="both",
            ),
            ParamSpec("--depth", "integer", "Hops, 1 to 3.", default=1),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph neighbors <id>", "Direct neighbors."),
            Example(
                "graph neighbors <id> --rel <relation> --direction out --depth 2 --json",
                "Two hops along one relation.",
            ),
        ),
        errors=(*REF_ERRORS, "term.unknown"),
        handler=neighbors,
        limits={"depth": 3},
    ),
)
