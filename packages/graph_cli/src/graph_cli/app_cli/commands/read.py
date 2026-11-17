"""``graph get``, ``graph history``, ``graph changes``, ``graph find``, ``graph lookup``,
``graph neighbors``, ``graph query`` and ``graph run``.

Contracts §4.3, B8.8, B8.12, B8.15, B9.7, B13 (Cypher); ``run``: named functions.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.app_cli.commands.common import (
    AS_OF,
    GRAPH,
    Hint,
    command_line,
    context,
    next_page,
)
from graph_cli.app_cli.commands.meta import GRAPH_ERRORS
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, SOURCE, STRINGS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    ANY,
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    nullable,
    obj,
    one_of,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.langtag import tagged_parts
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
from graph_cli.services.read import GraphRead, QuerySpec, refuse_affordances_with_rev
from graph_cli.temporal_knowledge.as_of import as_of_text, parse_as_of
from graph_cli.temporal_knowledge.changes import FEED_LIMIT, parse_since

if TYPE_CHECKING:  # the cover rule pulls in the Cypher parser, which reads must not load
    from graph_cli.kinetic.cover import Cover

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


FIELDS: Final = ParamSpec(
    "--fields",
    "string",
    "Only these fields (comma-separated): attributes, edges or system fields;"
    " id, alias, type, rev and record_status always.",
    repeatable=True,
)
FULL: Final = ParamSpec(
    "--full",
    "flag",
    "Also the system fields of how each entity was stored (created_at, recorded_at, tx_seq,"
    " ontology_version, content_hash) and the edge ids, for unlink --edge-id.",
)


def get(
    state: CommandState,
    ref: str,
    rev: int | None,
    fields: tuple[str, ...],
    full: bool,
    affordances: bool,
    graph: str | None,
    as_of: str | None,
) -> Result:
    if affordances and rev is not None:
        raise refuse_affordances_with_rev()
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {"ref": ref} if rev is None else {"ref": ref, "rev": rev}
    if affordances:
        args["affordances"] = True  # only when set: the log and audit args stay as they were
    if full:
        args["full"] = True
    view = _open(state, graph, "read.get", "get", args, point)
    ctx = context(state)
    if rev is not None and point is None:
        # An earlier revision is history: read.get and read.history (B8.15); with
        # --as-of, opening the view authorized both.
        request = RequestMeta.of("get", {"graph": view.entry.name, **args})
        ctx.reader.authorize(
            ctx.actor, "read.history", f"graph:{view.entry.id}", request, view.entry
        )
    data, warnings = ctx.reader.get(
        view, ref, rev=rev, fields=fields, affordances=affordances, full=full
    )
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
        return command_line("history", (ref,), view.entry.name, options, (), limit, next_offset)

    hints = next_page(page_json, line)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def changes(state: CommandState, since: str, limit: int, graph: str | None) -> Result:
    """The change feed (change feed plan CF1): what changed in the whole graph after a
    position, one page, oldest first. Authorized by ``read.history`` like ``history``; no
    ``--as-of`` (the position is the "from", the head the "to")."""
    position = parse_since(since)
    check_paging(limit, 0)
    args: dict[str, JsonValue] = {"since": since, "limit": limit}
    view = _open(state, graph, "read.history", "changes", args, None)
    data = context(state).reader.changes(view, position, limit=limit)
    options = (("--since", (str(data["next"]),)),)
    hints: tuple[Hint, ...] = ()
    if data["more"]:
        line = command_line(
            "changes", (), view.entry.name, options, (), limit, default_limit=FEED_LIMIT
        )
        hints = ({"text": "More changes available", "command": line},)
    return Result(data=data, graph=view.envelope_graph(), hints=hints, text=_changes_text(data))


def _changes_text(data: dict[str, JsonValue]) -> str:
    """The text of a feed page (CF14): a header line per tx, a line per entity, a line for
    the migration of a migration tx."""
    items = data["items"]
    assert isinstance(items, list)
    if not items:
        return f"No changes after {data['since']}.\n"
    lines: list[str] = []
    for entry in items:
        assert isinstance(entry, Mapping)
        lines += _entry_lines(entry)
    return "".join(line + "\n" for line in lines)


def _entry_lines(entry: Mapping[str, JsonValue]) -> list[str]:
    head = [f"tx:{entry['tx_seq']}", str(entry["recorded_at"]), str(entry["command"])]
    action = entry.get("action")
    if isinstance(action, Mapping):
        head.append(str(action["name"]))
    if entry["own_session"]:
        head.append("(own session)")
    if entry.get("partial"):
        head.append("(continues)")
    lines = [" ".join(head)]
    if entry["reason"]:
        lines.append(f"  reason: {_one_line(str(entry['reason']))}")
    migration = entry.get("migration")
    if isinstance(migration, Mapping):
        text = f"  migration {migration['from']} -> {migration['to']}"
        if "summary" in migration:
            text += f": {_one_line(str(migration['summary']))}"
        lines.append(text)
    entities = entry["entities"]
    assert isinstance(entities, list)
    for item in entities:
        assert isinstance(item, Mapping)
        lines.append("  " + _entity_line(item))
    return lines


def _entity_line(item: Mapping[str, JsonValue]) -> str:
    text = f"{item['id']} {item['type']} rev {item['rev']}"
    if item["record_status"] != "active":
        text += f" ({item['record_status']})"
    changed = item["changed"]
    if not isinstance(changed, Mapping):
        return f"{text} created"
    edges = changed["edges"]
    assert isinstance(edges, Mapping)
    named = [
        ("record", changed["record"]),
        ("attrs", changed["attrs"]),
        *((f"edges {kind}", names) for kind, names in edges.items()),
    ]
    parts = [
        f"{label}: {', '.join(map(str, names))}"
        for label, names in named
        if isinstance(names, list) and names
    ]
    return f"{text} {'; '.join(parts)}" if parts else f"{text} no visible change"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def find(
    state: CommandState,
    type: tuple[str, ...],
    where: tuple[str, ...],
    rel: tuple[str, ...],
    in_rel: tuple[str, ...],
    include_inactive: bool,
    sort: tuple[str, ...],
    fields: tuple[str, ...],
    full: bool,
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
    if full:
        args["full"] = True
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
        view, spec, rel_refs=rels, in_rel_refs=in_rels, fields=fields, full=full
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
    switches = ("--include-inactive",) * include_inactive + ("--full",) * full

    def line(next_offset: int) -> str:
        return command_line("find", (), view.entry.name, options, switches, limit, next_offset)

    hints = next_page(page_json, line)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


MAX_LOOKUP_TEXTS: Final = 50
MAX_SEARCH_HINTS: Final = 5


def lookup(
    state: CommandState,
    text: tuple[str, ...],
    type: tuple[str, ...],
    for_use: str | None,
    for_source: str | None,
    match: str,
    ignore_accents: bool,
    include_inactive: bool,
    lang: str | None,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    if len(text) > MAX_LOOKUP_TEXTS:
        raise GraphCliError(
            "usage.invalid_argument",
            f"text: at most {MAX_LOOKUP_TEXTS} values, got {len(text)}",
            details={"argument": "text", "actual": len(text)},
        )
    point = _as_of(as_of)
    args: dict[str, JsonValue] = {
        "text": list(text),
        "type": list(type),
        "for": for_use,
        "for_source": for_source,
        "match": match,
        "ignore_accents": ignore_accents,
        "include_inactive": include_inactive,
        "lang": lang,
        "limit": limit,
        "offset": offset,
    }
    view = _open(state, graph, "read.find", "lookup", args, point)
    spec = LookupSpec(
        text=text[0],
        types=type,
        match=match,
        ignore_accents=ignore_accents,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
        lang=lang,
    )
    data, page = context(state).reader.lookup(
        view, spec, text, for_use=for_use, for_source=for_source
    )
    page_json = page.to_json()
    switches = tuple(
        flag
        for flag, on in (
            ("--ignore-accents", ignore_accents),
            ("--include-inactive", include_inactive),
        )
        if on
    )

    uses = (
        ("--for", (for_use,) if for_use else ()),
        ("--for-source", (for_source,) if for_source else ()),
    )

    def line(
        texts: tuple[str, ...],
        match_kind: str,
        next_offset: int | None = None,
        *,
        fit: bool = True,
    ) -> str:
        options = (
            ("--type", type),
            *(uses if fit else ()),
            ("--match", (match_kind,) if match_kind != "exact" else ()),
            ("--lang", (lang,) if lang else ()),
            *_pinned(view, point),
        )
        return command_line("lookup", texts, view.entry.name, options, switches, limit, next_offset)

    counts = data["texts"]
    assert isinstance(counts, list)
    missing = tuple(str(c["text"]) for c in counts if isinstance(c, Mapping) and not c["total"])
    hints = next_page(page_json, lambda next_offset: line(text, match, next_offset))
    if missing and match == "exact":
        hints += (
            {
                "text": "No exact match; try names that start with the text"
                if len(counts) == 1
                else f"No exact match for {len(missing)} of {len(counts)}; try names that "
                "start with them",
                "command": line(missing, "prefix"),
            },
        )
    elif missing:
        options = (
            ("--type", type),
            *uses,
            ("--lang", (lang,) if lang else ()),
            *_pinned(view, point),
        )
        hints += tuple(
            {
                "text": "No name starts with the text; search names and text fields",
                "command": command_line(
                    "search",
                    (value,),
                    view.entry.name,
                    options,
                    ("--include-inactive",) if include_inactive else (),  # no accent switch
                    DEFAULT_LIMIT,
                ),
            }
            for value in missing[:MAX_SEARCH_HINTS]
        )
    return Result(
        data=data,
        graph=view.envelope_graph(),
        page=page_json,
        hints=hints + _fit_hints(counts, for_use, for_source, lambda t: line(t, match, fit=False)),
    )


def _fit_hints(
    counts: list[JsonValue],
    for_use: str | None,
    for_source: str | None,
    unfitted: Callable[[tuple[str, ...]], str],
) -> tuple[dict[str, JsonValue], ...]:
    """Lookup hints about the intended use: how to narrow names with several exact
    candidates (no ``--for`` given), or that ``--for`` dropped every exact match."""
    rows = [c for c in counts if isinstance(c, Mapping)]
    if for_use is None and for_source is None:
        several = [str(c["text"]) for c in rows if int(str(c["exact"])) > 1]
        if not several:
            return ()
        which = "the name" if len(rows) == 1 else f"{len(several)} of {len(rows)} names"
        return (
            {
                "text": f"Several exact candidates for {which}: narrow them to what the entity is"
                " for with --for <relation> (the candidate is its target), --for"
                " <function>.<param> or --for-source <relation>, or tell them apart by type"
                " and facts",
                "command": None,
            },
        )
    dropped = tuple(
        str(c["text"]) for c in rows if not int(str(c["exact"])) and int(str(c["excluded"]))
    )
    if not dropped:
        return ()
    use = " and ".join(
        f"{flag} {value}"
        for flag, value in (("--for", for_use), ("--for-source", for_source))
        if value
    )
    which = "" if len(rows) == 1 else f" for {len(dropped)} of {len(rows)} names"
    return (
        {
            "text": f"No exact match fits {use}{which}; matches of other types are left out:"
            " see them without it",
            "command": unfitted(dropped),
        },
    )


def search(
    state: CommandState,
    text: str,
    type: tuple[str, ...],
    field: tuple[str, ...],
    include_inactive: bool,
    lang: str | None,
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
        "lang": lang,
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
        lang=lang,
    )
    data, page = context(state).reader.search(view, spec)
    page_json = page.to_json()
    language = ("--lang", (lang,) if lang else ())
    options = (("--type", type), ("--field", field), language, *_pinned(view, point))
    switches = ("--include-inactive",) if include_inactive else ()

    def line(next_offset: int) -> str:
        return command_line(
            "search", (text,), view.entry.name, options, switches, limit, next_offset
        )

    hints = next_page(page_json, line)
    if not page.total and (type or field):
        hints = (
            {
                "text": "Nothing found; search every type and field",
                "command": command_line(
                    "search",
                    (text,),
                    view.entry.name,
                    (language, *_pinned(view, point)),
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
    full: bool,
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
    if full:
        args["full"] = True
    view = _open(state, graph, "read.traverse", "neighbors", args, point)
    data, warnings = context(state).reader.neighbors(
        view, ref, rels=rel, direction=direction, depth=depth, fields=fields, full=full
    )
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


def query_text(text: str | None, file: str | None) -> str:
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
    """A table cell: an entity as ``alias type`` (or id), an edge as its relation, a
    language-tagged value as ``"text"@tag``."""
    if isinstance(value, Mapping) and "content_hash" in value and "type" in value:
        return f"{value.get('alias') or value.get('id')} {value.get('type')}"
    if isinstance(value, Mapping) and "edge_id" in value and "rel" in value:
        return str(value["rel"])
    if value is None:
        return "-"
    parts = tagged_parts(value)
    if parts is not None:
        return f"{json.dumps(parts[0], ensure_ascii=False)}@{parts[1]}"
    if isinstance(value, list) and value and all(tagged_parts(v) for v in value):
        return ", ".join(_cell(v) for v in value)
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


MAX_COVER_HINTS: Final = 2


def _cover_hints(covers: tuple[Cover, ...], graph: str) -> tuple[Hint, ...]:
    """Hints for the best covering functions (usage shapes plan U4): a hint, never a
    rewrite; the agent decides whether to run the function."""
    hints: list[Hint] = []
    for cover in covers[:MAX_COVER_HINTS]:
        kind = "a list: repeat --param " + cover.parameter
        if not cover.many:
            kind = "one " + (cover.of_class or "value") + " per run"
        summary = cover.summary.rstrip()
        stop = "" if summary.endswith(".") else "."
        hints.append(
            {
                "text": f"Function {cover.function} covers this query: {summary}{stop} "
                f"It takes {kind}.",
                "command": command_line(
                    "run",
                    (cover.function,),
                    graph,
                    (("--param", (f"{cover.parameter}=<ref>",)),),
                    (),
                    DEFAULT_LIMIT,
                ),
            }
        )
    return tuple(hints)


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
    source = query_text(text, file)
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
    data, page, covers = reader.query(view, parsed, spec)
    cover_hints = _cover_hints(covers, view.entry.name)
    if page is None:
        return Result(data=data, graph=view.envelope_graph(), hints=cover_hints)
    page_json = page.to_json()
    positional = () if text is None else (text,)
    options = (
        ("--file", () if file is None else (file,)),
        ("--param", param),
        *_pinned(view, point),
    )
    switches = ("--include-inactive",) if include_inactive else ()

    def line(next_offset: int) -> str:
        return command_line(
            "query", positional, view.entry.name, options, switches, limit, next_offset
        )

    columns, items = data["columns"], data["items"]
    assert isinstance(columns, list) and isinstance(items, list)
    return Result(
        data=data,
        graph=view.envelope_graph(),
        page=page_json,
        hints=cover_hints + next_page(page_json, line),
        text=_table([str(c) for c in columns], items),
    )


def run(
    state: CommandState,
    name: str,
    param: tuple[str, ...],
    limit: int,
    offset: int,
    graph: str | None,
) -> Result:
    check_paging(limit, offset)
    pairs = [split_assignment(text, "--param") for text in param]
    args: dict[str, JsonValue] = {
        "name": name,
        "param": list(param),
        "limit": limit,
        "offset": offset,
    }
    view = _open(state, graph, "read.function", "run", args, None)
    data, page, warnings = context(state).reader.run(view, name, pairs, limit=limit, offset=offset)
    state.warnings += warnings
    page_json = page.to_json()

    def line(next_offset: int) -> str:
        return command_line(
            "run", (name,), view.entry.name, (("--param", param),), (), limit, next_offset
        )

    columns, items = data["columns"], data["items"]
    assert isinstance(columns, list) and isinstance(items, list)
    return Result(
        data=data,
        graph=view.envelope_graph(),
        page=page_json,
        hints=next_page(page_json, line),
        text=_table([str(c) for c in columns], items),
    )


REF: Final = ParamSpec(
    "ref", "entity_ref", "Full id, alias or trailing hex of the id.", required=True, positional=True
)

_FOUND: Final = {  # what lookup and search show of an entity
    "id": STRING,
    "alias": NULLABLE_STRING,
    "type": STRING,
    "record_status": STRING,
    "label": NULLABLE_STRING,
}
_LABEL_ROLES: Final = ("label", "alt_label", "hidden_label")
_MATCH: Final = {  # query.lookup.Match.to_json; search adds score and excerpt for value
    "attr": STRING,
    "role": enum(*_LABEL_ROLES),
    "kind": STRING,
    "lang": NULLABLE_STRING,
}
# search also reads attributes without a label role: the role is null there
_SEARCH_MATCH: Final = {**_MATCH, "role": nullable(enum(*_LABEL_ROLES))}
GET_OUTPUT: Final = obj(
    {
        "entity": ref("entity"),
        "current_rev": INTEGER,
        "superseded_by": STRINGS,
        "retracted_by": STRINGS,
    },
    {"affordances": obj(extra=ANY)},  # only with --affordances
)
CHANGED: Final = nullable(  # temporal_knowledge.history.changed_names; null for a creation
    obj(
        {
            "record": STRINGS,
            "attrs": STRINGS,
            "edges": obj({"added": STRINGS, "removed": STRINGS, "changed": STRINGS}),
        }
    )
)
HISTORY_OUTPUT: Final = obj(
    {
        "id": STRING,
        "current_rev": INTEGER,
        "items": array(
            obj(
                {
                    "rev": INTEGER,
                    "tx_seq": INTEGER,
                    "recorded_at": STRING,
                    "record_status": STRING,
                    "command": NULLABLE_STRING,
                    "reason": NULLABLE_STRING,
                    "own_session": BOOLEAN,
                    "changed": CHANGED,
                },
                {
                    "action": obj({"name": STRING, "hash": STRING}),  # set by a ``do``
                    "source": SOURCE,  # set by a document that names its source run
                    # the revision a migration wrote (services.read._migration_item)
                    "migration": obj(
                        {
                            "from": NULLABLE_STRING,
                            "to": NULLABLE_STRING,
                            "renames": array(obj({"kind": STRING}, extra=ANY)),
                            "steps": array(obj({"kind": STRING, "index": INTEGER}, extra=ANY)),
                        },
                        {"summary": STRING},
                    ),
                },
            )
        ),
    }
)
# services.read.change_page: the answer of ``graph changes`` (change feed plan section 4)
CHANGES_OUTPUT: Final = obj(
    {
        "since": STRING,
        "since_tx_id": NULLABLE_STRING,
        "head": INTEGER,
        "next": STRING,
        "next_tx_id": NULLABLE_STRING,
        "more": BOOLEAN,
        "view": NULLABLE_STRING,
        "items": array(
            obj(
                {
                    "tx_seq": INTEGER,
                    "tx_id": STRING,
                    "recorded_at": STRING,
                    "command": NULLABLE_STRING,
                    "reason": NULLABLE_STRING,
                    "own_session": BOOLEAN,
                    "entities": array(
                        obj(
                            {
                                "id": STRING,
                                "type": STRING,
                                "rev": INTEGER,
                                "record_status": STRING,
                                "changed": CHANGED,
                                "targets": STRINGS,
                            },
                            {"targets_more": INTEGER},  # targets beyond the cap
                        )
                    ),
                },
                {
                    "action": obj({"name": STRING, "hash": STRING}),  # set by a ``do``
                    "source": SOURCE,  # set by a document that names its source run
                    "migration": obj(  # a migration tx (services.read._feed_migration)
                        {"from": NULLABLE_STRING, "to": NULLABLE_STRING}, {"summary": STRING}
                    ),
                    "partial": BOOLEAN,  # true: the tx goes on on the next page
                },
            )
        ),
    }
)
FIND_OUTPUT: Final = obj({"items": array(ref("entity"))})
LOOKUP_OUTPUT: Final = obj(
    {
        "items": array(
            obj(
                {
                    "text": STRING,
                    **_FOUND,
                    "match": obj({**_MATCH, "value": NULLABLE_STRING}),
                    "facts": nullable(  # query.lookup.fact_edges
                        array(
                            obj(
                                {
                                    "rel": STRING,
                                    "id": STRING,
                                    "alias": NULLABLE_STRING,
                                    "label": NULLABLE_STRING,
                                }
                            )
                        )
                    ),
                }
            )
        ),
        "texts": array(
            obj({"text": STRING, "total": INTEGER, "exact": INTEGER, "excluded": INTEGER})
        ),
        "fit": nullable(  # --for, --for-source: services.read.ReadService.lookup
            obj({"for": NULLABLE_STRING, "for_source": NULLABLE_STRING, "types": nullable(STRINGS)})
        ),
    }
)
SEARCH_OUTPUT: Final = obj(
    {
        "items": array(
            obj(
                {
                    **_FOUND,
                    "match": obj({**_SEARCH_MATCH, "score": INTEGER, "excerpt": NULLABLE_STRING}),
                    "fields": STRINGS,
                }
            )
        )
    }
)
NEIGHBORS_OUTPUT: Final = obj(
    {
        "start": STRING,
        "items": array(
            obj({"direction": enum("out", "in"), "edge": ref("edge"), "entity": ref("entity")})
        ),
    }
)
QUERY_OUTPUT: Final = one_of(
    # B9.7: a cell is an entity as get shows it, an edge with its source, a list, a map or
    # a plain value
    obj({"columns": STRINGS, "items": array(obj(extra=ANY))}),
    obj(
        {
            "plan": obj(  # --explain: query.cypher.resolve.ResolvedQuery.explain
                {
                    "clauses": array(
                        obj(
                            {
                                "clause": STRING,
                                "text": STRING,
                                "variables": obj(
                                    extra=obj({"kind": STRING, "types": nullable(STRINGS)})
                                ),
                            }
                        )
                    ),
                    "columns": STRINGS,
                    "parameters": obj(extra=ANY),
                    "fields": array(
                        obj(
                            {
                                "variable": NULLABLE_STRING,
                                "field": STRING,
                                "datatypes": STRINGS,
                                "many": BOOLEAN,
                            }
                        )
                    ),
                }
            )
        }
    ),
)
RUN_OUTPUT: Final = obj(
    {
        "function": obj({"name": STRING, "hash": STRING}),
        "columns": STRINGS,
        "items": array(obj(extra=ANY)),
    }
)
RUN_ERRORS: Final = (
    *GRAPH_ERRORS,
    "function.not_found",
    "function.parameter_missing",
    "function.parameter_unknown",
    "function.parameter_invalid",
    "function.parameter_type",
    "function.stale",
    "entity.not_found",
    "entity.ambiguous_ref",
    "query.too_large",
    "query.invalid_value",
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
            "permission to read history. --affordances adds what the current state allows; "
            "transitions and relations are model-level (not filtered by write rights), actions "
            "and functions are listed as describe actions and describe functions list them."
        ),
        params=(
            REF,
            ParamSpec("--rev", "integer", "Show this earlier revision instead of the current one."),
            FIELDS,
            FULL,
            ParamSpec(
                "--affordances",
                "flag",
                "Also show what the entity can do next: lifecycle transitions with their guards,"
                " relations with room left, and the actions and functions that take it as a"
                " parameter. Not with --rev.",
            ),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph get <id>", "Show one entity."),
            Example(
                "graph get <id> --affordances --json", "The entity and what applies to it next."
            ),
            Example("graph get <id> --graph <graph> --json", "The entity as JSON."),
            Example("graph get <id> --rev 2 --json", "The entity as it was at revision 2."),
            Example("graph get <id> --fields <attr>,edges --json", "Only one field and the edges."),
            Example("graph get <id> --full --json", "With the system fields and the edge ids."),
            Example(
                "graph get <id> --as-of tx:<seq> --json", "The entity as it was after that tx."
            ),
        ),
        errors=(*REF_ERRORS, "entity.revision_not_found"),
        handler=get,
        output=GET_OUTPUT,
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
        output=HISTORY_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("changes",),
        kind="read",
        surface="trace",
        action="read.history",
        summary="List what changed in the whole graph after a position, oldest first; paged.",
        description=(
            "The change feed: per tx the entities it changed, as names and ids, never values; "
            "read values with get or find. --since is tx:<seq> (tx:0 for the start) or an "
            "RFC 3339 time with offset. Continue with the next of the answer while more is "
            "true. A different view than the one stored means start again from tx:0, and so "
            "does a since_tx_id that differs from the stored next_tx_id. Needs permission to "
            "read history."
        ),
        params=(
            ParamSpec(
                "--since",
                "string",
                "Where to start: tx:<seq>, tx:<seq>/<entity id> (a next of the answer) or an "
                "RFC 3339 time with offset.",
                required=True,
            ),
            ParamSpec(
                "--limit",
                "integer",
                f"Entity items per page, at most {MAX_LIMIT}.",
                default=FEED_LIMIT,
            ),
            GRAPH,
        ),
        examples=(
            Example("graph changes --since tx:0 --json", "Every change from the start."),
            Example(
                "graph changes --since tx:<seq> --graph <graph> --json",
                "Continue after a position, the next of the last answer.",
            ),
            Example(
                "graph changes --since 2030-01-01T12:00:00Z --json", "What changed since a time."
            ),
        ),
        errors=(*GRAPH_ERRORS, "usage.invalid_argument"),
        handler=changes,
        output=CHANGES_OUTPUT,
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
                "attr=value or attr:op=value (ne lt le gt ge contains prefix in exists lang).",
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
            FULL,
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
        output=FIND_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("lookup",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Find entities by name: main, alternative and hidden labels; ranked candidates.",
        description=(
            "Matches case- and Unicode-insensitively. Several names are looked up at once, "
            f"at most {MAX_LOOKUP_TEXTS}; each argument is one name, so quote a name with "
            "spaces. Limit and offset apply per name; data.texts counts the candidates and "
            "exact matches of each. --for and --for-source keep the candidates whose type "
            "can fill a slot and count the others as excluded. A name with several candidates "
            "gives each up to three facts that tell them apart. Labels are not identifiers: "
            "use the returned id for get, writes and change sets."
        ),
        params=(
            ParamSpec(
                "text",
                "string",
                "The names to look for, one per argument.",
                required=True,
                positional=True,
                repeatable=True,
            ),
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
            ParamSpec(
                "--for",
                "string",
                "Only candidates that can fill this slot: a relation (the candidate is its "
                "target) or <function or action>.<param> (a ref parameter).",
                keyword="for_use",
            ),
            ParamSpec(
                "--for-source",
                "term",
                "Only candidates that can be the source of this relation.",
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
                "--lang",
                "string",
                "Only names in this language (a BCP 47 tag or range such as de, or *); "
                "also picks the shown label.",
            ),
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
                "graph lookup <value> '<value> <value>' --json",
                "Several names in one call, one per argument.",
            ),
            Example(
                "graph lookup <value> --type <type> --match prefix --json",
                "Names of one type that start with the text.",
            ),
            Example(
                "graph lookup <value> --for <relation> --json",
                "Only candidates that can be the target of the relation.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "temporal.as_of_unsupported",
            "term.unknown",
            "query.ambiguous_term",
            "term.no_labels",
            "function.not_found",
            "function.parameter_unknown",
            "action.parameter_unknown",
        ),
        handler=lookup,
        output=LOOKUP_OUTPUT,
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
                "--lang",
                "string",
                "Only text in this language (a BCP 47 tag or range such as de, or *); "
                "also picks the shown label.",
            ),
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
        output=SEARCH_OUTPUT,
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
            FULL,
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
        output=NEIGHBORS_OUTPUT,
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
                pairs="map",
                pair_values="json",
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
        output=QUERY_OUTPUT,
        remote="hooked",
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("run",),
        kind="read",
        surface="retrieve",
        action="read.function",
        summary="Run a named function of the graph with typed parameters.",
        description=(
            "A function is a curated, typed read query the maintainer published; find one "
            "with describe search --kind function. Each --param is read by the type the "
            "function declares (repeat a name for a list). A reference parameter takes an "
            "id, alias or trailing hex digits of an entity of the declared class, never a "
            "name: look the name up first. The answer is like query's, plus the function's "
            "name and hash."
        ),
        params=(
            ParamSpec("name", "string", "The function name.", required=True, positional=True),
            ParamSpec(
                "--param",
                "assignment",
                "name=value for a parameter of the function, read by its declared type.",
                repeatable=True,
                pairs="multimap",
            ),
            ParamSpec(
                "--limit", "integer", f"Rows per page, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many rows.", default=0),
            GRAPH,
        ),
        examples=(
            Example("graph run <function> --param key=<value> --json", "Run a function."),
            Example(
                "graph run <function> --param key=<value> --param key=<value>",
                "A list parameter takes its name once per value.",
            ),
        ),
        errors=RUN_ERRORS,
        handler=run,
        output=RUN_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
)
