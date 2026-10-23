"""``graph get``, ``graph history``, ``graph find``, ``graph lookup``, ``graph neighbors``
and ``graph query``.

Contracts §4.3, B8.8, B8.12, B8.15, B9.7, B13 (Cypher).
"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from graph_cli.app_cli.commands.common import AS_OF, GRAPH, PROGRAM, context
from graph_cli.app_cli.commands.meta import GRAPH_ERRORS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.cypher.values import Value, parameter_value
from graph_cli.query.lookup import MATCH_KINDS, LookupSpec
from graph_cli.query.plan import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FindSpec,
    check_paging,
    parse_rel_flag,
    split_assignment,
)
from graph_cli.query.search import SearchSpec
from graph_cli.services.read import GraphRead, QuerySpec
from graph_cli.temporal_knowledge.as_of import as_of_text, parse_as_of

REF_ERRORS: Final = (
    *GRAPH_ERRORS,
    "entity.not_found",
    "entity.ambiguous_ref",
    "temporal.as_of_unsupported",
)
QUERY_ERRORS: Final = (
    *GRAPH_ERRORS,
    "temporal.as_of_unsupported",
    "term.unknown",
    "query.syntax",
    "query.unsupported",
    "query.ambiguous_term",
    "query.unbound_variable",
    "query.parameter_missing",
    "query.too_large",
    "query.invalid_value",
)
STDIN: Final = "-"


def _open(
    state: CommandState,
    graph: str | None,
    action: str,
    command: str,
    args: dict[str, JsonValue],
    as_of: AsOf | None,
) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    if as_of is not None:
        args = {**args, "as_of": as_of_text(as_of)}
    request = RequestMeta.of(command, {"graph": entry.name, **args})
    view = ctx.reader.open(ctx.actor, entry, action, request, as_of=as_of)
    state.warnings += view.warnings
    return view


def _as_of(text: str | None) -> AsOf | None:
    """``--as-of`` parsed before the graph is opened (B8.8)."""
    return None if text is None else parse_as_of(text)


def _pinned(view: GraphRead, as_of: AsOf | None) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``--as-of tx:<seq>`` for a next-page command: the same state, also after a timestamp."""
    return () if as_of is None else (("--as-of", (f"tx:{view.snapshot.seq}",)),)


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


FIELDS: Final = ParamSpec(
    "--fields",
    "string",
    "Only these fields (comma-separated): attributes, edges or system fields;"
    " id, alias, type, rev and record_status always.",
    repeatable=True,
)


def get(
    state: CommandState,
    ref: str,
    rev: int | None,
    fields: tuple[str, ...],
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {"ref": ref} if rev is None else {"ref": ref, "rev": rev}
    view = _open(state, graph, "read.get", "get", args, point)
    ctx = context(state)
    if rev is not None and point is None:
        # An earlier revision is history: read.get and read.history (B8.15); with
        # --as-of, opening the view authorized both.
        request = RequestMeta.of("get", {"graph": view.entry.name, **args})
        ctx.reader.authorize(
            ctx.actor, "read.history", f"graph:{view.entry.id}", request, view.entry
        )
    data, warnings = ctx.reader.get(view, ref, rev=rev, fields=fields)
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


def history(
    state: CommandState, ref: str, limit: int, offset: int, graph: str | None, as_of: str | None
) -> Result:
    point = _as_of(as_of)
    check_paging(limit, offset)
    args: dict[str, JsonValue] = {"ref": ref, "limit": limit, "offset": offset}
    view = _open(state, graph, "read.history", "history", args, point)
    ctx = context(state)
    data, page, warnings = ctx.reader.history(view, ctx.actor, ref, limit=limit, offset=offset)
    state.warnings += warnings
    page_json = page.to_json()

    def line(next_offset: int) -> str:
        options = _pinned(view, point)
        return _command_line("history", (ref,), view.entry.name, options, (), limit, next_offset)

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
    fields: tuple[str, ...],
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {
        "type": list(type),
        "where": list(where),
        "rel": list(rel),
        "in_rel": list(in_rel),
        "include_inactive": include_inactive,
        "sort": list(sort),
        "fields": list(fields),
        "limit": limit,
        "offset": offset,
    }
    rels = [parse_rel_flag(text, "--rel") for text in rel]
    in_rels = [parse_rel_flag(text, "--in-rel") for text in in_rel]
    view = _open(state, graph, "read.find", "find", args, point)
    spec = FindSpec(
        types=type,
        where=where,
        include_inactive=include_inactive,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    data, page, warnings = context(state).reader.find(
        view, spec, rel_refs=rels, in_rel_refs=in_rels, fields=fields
    )
    state.warnings += warnings
    page_json = page.to_json()
    options = (
        ("--type", type),
        ("--where", where),
        ("--rel", rel),
        ("--in-rel", in_rel),
        ("--sort", sort),
        ("--fields", fields),
        *_pinned(view, point),
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
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {
        "text": text,
        "type": list(type),
        "match": match,
        "ignore_accents": ignore_accents,
        "include_inactive": include_inactive,
        "limit": limit,
        "offset": offset,
    }
    view = _open(state, graph, "read.find", "lookup", args, point)
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
        options = (
            ("--type", type),
            ("--match", (match_kind,) if match_kind != "exact" else ()),
            *_pinned(view, point),
        )
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
    elif not page.total:
        options = (("--type", type), *_pinned(view, point))
        hints = (
            {
                "text": "No name starts with the text; search names and text fields",
                "command": _command_line(
                    "search",
                    (text,),
                    view.entry.name,
                    options,
                    ("--include-inactive",) if include_inactive else (),  # no accent switch
                    DEFAULT_LIMIT,
                ),
            },
        )
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def search(
    state: CommandState,
    text: str,
    type: tuple[str, ...],
    field: tuple[str, ...],
    include_inactive: bool,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {
        "text": text,
        "type": list(type),
        "field": list(field),
        "include_inactive": include_inactive,
        "limit": limit,
        "offset": offset,
    }
    view = _open(state, graph, "read.find", "search", args, point)
    spec = SearchSpec(
        text=text,
        types=type,
        fields=field,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    data, page = context(state).reader.search(view, spec)
    page_json = page.to_json()
    options = (("--type", type), ("--field", field), *_pinned(view, point))
    switches = ("--include-inactive",) if include_inactive else ()

    def line(next_offset: int) -> str:
        return _command_line(
            "search", (text,), view.entry.name, options, switches, limit, next_offset
        )

    hints = _next_page(page_json, line)
    if not page.total and (type or field):
        hints = (
            {
                "text": "Nothing found; search every type and field",
                "command": _command_line(
                    "search",
                    (text,),
                    view.entry.name,
                    _pinned(view, point),
                    switches,
                    DEFAULT_LIMIT,
                ),
            },
        )
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def neighbors(
    state: CommandState,
    ref: str,
    rel: tuple[str, ...],
    direction: str,
    depth: int,
    fields: tuple[str, ...],
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {
        "ref": ref,
        "rel": list(rel),
        "direction": direction,
        "depth": depth,
        "fields": list(fields),
    }
    view = _open(state, graph, "read.traverse", "neighbors", args, point)
    data, warnings = context(state).reader.neighbors(
        view, ref, rels=rel, direction=direction, depth=depth, fields=fields
    )
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


def _query_text(text: str | None, file: str | None) -> str:
    """The query from the argument, ``-`` (stdin) or ``--file`` (F10: no heredoc needed)."""
    if (text is None) == (file is None):
        raise _usage("text", "give the query as an argument, - for stdin, or --file <path>")
    if text is not None and text != STDIN:
        return text
    argument = "--file" if file is not None else "text"
    try:
        data = sys.stdin.buffer.read() if STDIN in (text, file) else Path(str(file)).read_bytes()
    except OSError as exc:
        raise _usage(argument, f"cannot read {file}: {exc.strerror}", file) from None
    except ValueError:  # e.g. a NUL in the path
        raise _usage(argument, "not a valid path", file) from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _usage(argument, "the query is not UTF-8 text") from None


def _parameters(texts: tuple[str, ...]) -> dict[str, Value]:
    found: dict[str, Value] = {}
    for text in texts:
        name, value = split_assignment(text, "--param")
        if name in found:
            raise _usage("--param", f"{name!r} is given twice", text)
        found[name] = parameter_value(value)
    return found


def _usage(argument: str, message: str, actual: JsonValue = None) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, "actual": actual},
    )


def _cell(value: JsonValue) -> str:
    """A table cell: an entity as ``alias type`` (or id), an edge as its relation."""
    if isinstance(value, Mapping) and "content_hash" in value and "type" in value:
        return f"{value.get('alias') or value.get('id')} {value.get('type')}"
    if isinstance(value, Mapping) and "edge_id" in value and "rel" in value:
        return str(value["rel"])
    if value is None:
        return "-"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _table(columns: list[str], items: list[JsonValue]) -> str:
    rows = [[_cell(item[c]) for c in columns] for item in items if isinstance(item, Mapping)]
    widths = [max([len(c), *(len(row[i]) for row in rows)]) for i, c in enumerate(columns)]
    lines = [columns, *rows]
    return "".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        + "\n"
        for line in lines
    )


def query(
    state: CommandState,
    text: str | None,
    file: str | None,
    param: tuple[str, ...],
    include_inactive: bool,
    explain: bool,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = _as_of(as_of)
    check_paging(limit, offset)
    source = _query_text(text, file)
    parameters = _parameters(param)
    reader = context(state).reader
    parsed = reader.parse_query(source)
    args: dict[str, JsonValue] = {
        "text": source,
        "param": list(param),
        "include_inactive": include_inactive,
        "explain": explain,
        "limit": limit,
        "offset": offset,
    }
    view = _open(state, graph, "read.query", "query", args, point)
    spec = QuerySpec(parameters, include_inactive, explain, limit, offset)
    data, page = reader.query(view, parsed, spec)
    if page is None:
        return Result(data=data, graph=view.envelope_graph())
    page_json = page.to_json()
    positional = () if text is None else (text,)
    options = (
        ("--file", () if file is None else (file,)),
        ("--param", param),
        *_pinned(view, point),
    )
    switches = ("--include-inactive",) if include_inactive else ()

    def line(next_offset: int) -> str:
        return _command_line(
            "query", positional, view.entry.name, options, switches, limit, next_offset
        )

    columns, items = data["columns"], data["items"]
    assert isinstance(columns, list) and isinstance(items, list)
    return Result(
        data=data,
        graph=view.envelope_graph(),
        page=page_json,
        hints=_next_page(page_json, line),
        text=_table([str(c) for c in columns], items),
    )


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
            FIELDS,
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph get <id>", "Show one entity."),
            Example("graph get <id> --graph <graph> --json", "The entity as JSON."),
            Example("graph get <id> --rev 2 --json", "The entity as it was at revision 2."),
            Example("graph get <id> --fields <attr>,edges --json", "Only one field and the edges."),
            Example(
                "graph get <id> --as-of tx:<seq> --json", "The entity as it was after that tx."
            ),
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
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
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
            FIELDS,
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
                "graph find --type <type> --fields <attr> --json",
                "Only one field of each entity.",
            ),
            Example(
                "graph find --type <type> --rel <relation>=<id> --json", "Filter by a relation."
            ),
            Example(
                "graph find --type <type> --rel <relation>=<id>,<id> --json",
                "An edge to any of the listed entities.",
            ),
            Example(
                "graph find --type <type> --as-of tx:<seq> --json",
                "The entities of a type as they were after that tx.",
            ),
        ),
        errors=(*REF_ERRORS, "term.unknown", "query.ambiguous_term"),
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
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
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
        errors=(
            *GRAPH_ERRORS,
            "temporal.as_of_unsupported",
            "term.unknown",
            "query.ambiguous_term",
            "term.no_labels",
        ),
        handler=lookup,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("search",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Search names and text fields of every type; ranked suggestions, typos allowed.",
        description=(
            "Ignores case, accents and punctuation; finds the text inside longer words and "
            "similar names. Results are suggestions: check one with get before you use its id."
        ),
        params=(
            ParamSpec("text", "string", "The words to look for.", required=True, positional=True),
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
            ParamSpec("--field", "string", "Only this text field.", repeatable=True),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph search <value>", "Names and text fields of any type."),
            Example(
                "graph search <value> --type <type> --field <attr> --json",
                "One field of one type.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "temporal.as_of_unsupported",
            "term.unknown",
            "query.ambiguous_term",
            "term.no_text_fields",
        ),
        handler=search,
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
            FIELDS,
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
        errors=(*REF_ERRORS, "term.unknown", "query.ambiguous_term"),
        handler=neighbors,
        limits={"depth": 3},
    ),
    CommandSpec(
        path=("query",),
        kind="read",
        surface="retrieve",
        action="read.query",
        summary="Answer a read-only Cypher query: several hops, OR/NOT across relations, counts.",
        description=(
            "Labels are types (subtypes included), written as the local name when it is "
            "unique or as the CURIE in backticks; relationship types are relations. Only "
            "active entities are nodes unless --include-inactive. Writes are refused: "
            "use apply. --explain shows the resolved query without running it."
        ),
        params=(
            ParamSpec(
                "text",
                "string",
                f"The query, or {STDIN} to read it from stdin.",
                positional=True,
            ),
            ParamSpec("--file", "file", "Read the query from this file."),
            ParamSpec(
                "--param",
                "assignment",
                "name=value binds $name; the value is JSON when it parses, else text.",
                repeatable=True,
            ),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec("--explain", "flag", "Show the resolved query; run nothing."),
            ParamSpec(
                "--limit", "integer", f"Rows per page, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many rows.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example(
                "graph query 'MATCH (n:<type>)-[:<relation>]->(m) RETURN n, m.<attr>' --json",
                "Entities with the entities they point to.",
            ),
            Example(
                "graph query 'MATCH (n:<type>) WHERE NOT (n)-[:<relation>]->() RETURN n' --json",
                "Entities without an outgoing edge of one relation.",
            ),
            Example(
                "graph query 'MATCH (n:<type>)-[:<relation>]->(m) "
                "RETURN m.<attr>, count(n) AS sources ORDER BY sources DESC' --json",
                "Counts per target.",
            ),
            Example(
                "graph query --file <file> --param id=<id> --json",
                "A query from a file with a parameter.",
            ),
        ),
        errors=QUERY_ERRORS,
        handler=query,
        limits={"limit": MAX_LIMIT},
    ),
)
