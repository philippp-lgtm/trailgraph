"""``graph query`` (a Cypher read) and ``graph run`` (a named function): the handlers, the
table text, the output schemas and the specs."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.app_cli.commands.common import (
    AS_OF,
    GRAPH,
    GRAPH_ERRORS,
    Hint,
    command_line,
    context,
    next_page,
)
from graph_cli.app_cli.commands.read.shared import STDIN, as_of_flag, open_graph, pinned
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, STRINGS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import ANY, BOOLEAN, STRING, array, nullable, obj, one_of
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.langtag import tagged_parts
from graph_cli.query.cypher.values import Value, is_number_text, parameter_value
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT, check_paging, split_assignment
from graph_cli.services.read import QuerySpec

if TYPE_CHECKING:  # the cover rule pulls in the Cypher parser, which reads must not load
    from graph_cli.kinetic.cover import Cover

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


def _parameters(texts: tuple[str, ...]) -> tuple[dict[str, Value], dict[str, str]]:
    """The values as given (JSON when it parses) and, for a number or a list of numbers,
    the text as written: the query may bind it as text (``text_params``)."""
    found: dict[str, Value] = {}
    written: dict[str, str] = {}
    for text in texts:
        name, value = split_assignment(text, "--param")
        if name in found:
            raise _usage("--param", f"{name!r} is given twice", text)
        found[name] = parameter_value(value)
        if is_number_text(value):
            written[name] = value
    return found, written


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
    point = as_of_flag(as_of)
    check_paging(limit, offset)
    source = query_text(text, file)
    parameters, written = _parameters(param)
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
    view = open_graph(state, graph, "read.query", "query", args, point)
    spec = QuerySpec(parameters, include_inactive, explain, limit, offset, texts=written)
    data, page, covers, warnings = reader.query(view, parsed, spec)
    state.warnings += warnings
    cover_hints = _cover_hints(covers, view.entry.name)
    if page is None:
        return Result(data=data, graph=view.envelope_graph(), hints=cover_hints)
    page_json = page.to_json()
    positional = () if text is None else (text,)
    options = (
        ("--file", () if file is None else (file,)),
        ("--param", param),
        *pinned(view, point),
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
    view = open_graph(state, graph, "read.function", "run", args, None)
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
                "name=value binds $name; the value is JSON when it parses, else text"
                " (a number compared only with text fields is bound as written).",
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
