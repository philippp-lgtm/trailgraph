"""``graph get``, ``graph find`` and ``graph neighbors`` (contracts §4.3, B8.8, B8.12, B9.7)."""

from __future__ import annotations

import shlex
from typing import Final

from graph_cli.app_cli.commands.common import AS_OF, GRAPH, PROGRAM, context
from graph_cli.app_cli.commands.meta import GRAPH_ERRORS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT, FindSpec, parse_rel_flag
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


def get(state: CommandState, ref: str, graph: str | None, as_of: str | None) -> Result:
    refuse_as_of(as_of)
    view = _open(state, graph, "read.get", "get", {"ref": ref})
    data, warnings = context(state).reader.get(view, ref)
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


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
    hints: tuple[dict[str, JsonValue], ...] = ()
    next_offset = page_json["next_offset"]
    if isinstance(next_offset, int):
        parts = [PROGRAM, "find", "--graph", view.entry.name]
        for flag, values in (
            ("--type", type),
            ("--where", where),
            ("--rel", rel),
            ("--in-rel", in_rel),
            ("--sort", sort),
        ):
            for value in values:
                parts += [flag, value]
        if include_inactive:
            parts.append("--include-inactive")
        parts += ["--limit", str(limit), "--offset", str(next_offset)]
        hints = ({"text": "More results available", "command": shlex.join(parts)},)
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
        params=(REF, GRAPH, AS_OF),
        examples=(
            Example("graph get <id>", "Show one entity."),
            Example("graph get <id> --graph <graph> --json", "The entity as JSON."),
        ),
        errors=REF_ERRORS,
        handler=get,
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
