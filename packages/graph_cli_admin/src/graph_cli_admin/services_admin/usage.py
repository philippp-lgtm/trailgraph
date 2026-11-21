"""``graph-admin usage report``: what the access log says about how agents use the graphs (U6).

The access log (``graph_cli.access/1``, contracts B5.10) of the installation holds, per
``graph`` command, who ran what on which graph; a ``run`` names its function and a ``query``
its shape and the functions that cover it. This report reads it and answers the maintainer's
questions before writing or changing functions: which functions are used and by how many
sessions, which published ones never run (retirement candidates), which query shapes recur
(ranked by distinct sessions, not calls: one session looping over a shape is one need) and
three derived lists: *discovery misses* (a shape that a function covers is still queried),
*batch gaps* (the covering function takes one value where the query took a list) and
*candidates* (a shape no function covers, seen in at least two sessions).

A failed command's envelope names no graph, so a ``run`` or ``query`` that failed before the
graph was open belongs to none: those are counted apart (``without_graph``).

Shapes only: the log holds model terms and placeholders, so there is no value to show, and
sessions are counted, never listed. The aggregation is pure (parsed lines in, plain data
out); the reader and the catalog lookups around it are thin. A session is the actor's session
id (else its name); a line without either counts under one shared bucket, so unlabelled
callers never inflate a distinct count.

One part is installation-wide: the *sessions* and how many of them began with ``graph start``
(the one call the skills tell an agent to make first). A session here is a session id of the
actor together with its agent id when it has one (a subagent shares the session id of its parent
and has an id of its own, and the skills tell every agent to start, so each counts on its own;
without an agent id the session stands alone). A line with only a session name, or none, belongs
to no session and is not counted. The first command of a session is the ``command`` of its
earliest line in the whole log (file order breaks a tie of timestamps), whatever the command or
its outcome, and a line is only there for a command that opened the installation, so ``describe
commands`` and ``--help`` are not first commands. With
``--since`` a session counts when its first line is on or after it, however late the rest.
``--graph`` does not narrow it. Only commands the access log records are seen (in the remote mode
those that reach a command route of the server), and the part is empty unless the log is on.

Each list holds at most ``CAP`` entries; when it was cut, a sibling ``<list>_more`` names how
many were left out (absent otherwise, as for the affordances).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from graph_cli.catalog.access_log import ACCESS_FILE, ACCESS_SCHEMA
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp, parse_date, parse_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli_admin.services_admin import kinetic_slots
from graph_cli_admin.services_admin.context import AdminContext

CAP: Final = 20
CANDIDATE_SESSIONS: Final = 2  # an uncovered shape seen in this many sessions is a candidate
START: Final = "start"  # the command of ``graph start`` in the access log
NO_SESSION: Final = ""  # the one bucket of lines whose actor names no session


def parse_since(text: str) -> datetime:
    """``--since``: an ISO date (midnight UTC) or an RFC 3339 timestamp."""
    try:
        return datetime(*parse_date(text).timetuple()[:3], tzinfo=UTC)
    except GraphCliError:
        pass
    try:
        return parse_timestamp(text)
    except GraphCliError:
        raise GraphCliError(
            "usage.invalid_argument",
            f"--since {text!r} is neither an ISO date nor an RFC 3339 timestamp",
            details={"argument": "--since", "actual": text},
        ) from None


# ---- reading --------------------------------------------------------------------------


def read_lines(path: Path) -> Iterator[object]:
    """The parsed lines of the access log; ``None`` for one that is not JSON. Nothing for a
    missing file."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                yield None


# ---- aggregation (pure) ---------------------------------------------------------------


@dataclass(slots=True)
class _Function:
    calls: int = 0
    sessions: set[str] = field(default_factory=set)
    errors: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _Shape:
    shape_hash: str
    shape: str | None = None
    calls: int = 0
    errors: int = 0
    sessions: set[str] = field(default_factory=set)
    last_ts: str = ""
    covered_by: list[JsonValue] = field(default_factory=list)


@dataclass(slots=True)
class _Graph:
    name: str = ""
    last_ts: str = ""
    runs: int = 0
    queries: int = 0
    unshaped: int = 0
    functions: dict[str, _Function] = field(default_factory=dict)
    shapes: dict[str, _Shape] = field(default_factory=dict)


@dataclass(slots=True)
class _Session:
    """The earliest line of one session and agent: when, where in the file, which command."""

    first: datetime
    order: int
    command: str


@dataclass(slots=True)
class Aggregate:
    graphs: dict[str, _Graph] = field(default_factory=dict)
    sessions: dict[tuple[str, str], _Session] = field(default_factory=dict)
    skipped: int = 0
    # run and query lines with no graph: they failed before one was open (an error envelope
    # names none), so no graph can be charged with them
    without_graph: int = 0
    without_graph_errors: Counter[str] = field(default_factory=Counter)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _session(actor: object) -> str:
    session = _mapping(_mapping(actor).get("session"))
    for key in ("id", "name"):
        value = session.get(key)
        if isinstance(value, str) and value:
            return value
    return NO_SESSION


def _session_key(actor: object) -> tuple[str, str] | None:
    """The actor's session id and agent id (``""`` without one); ``None`` for a session with a
    name only or none (no session to count)."""
    session = _mapping(_mapping(actor).get("session")).get("id")
    if not isinstance(session, str) or not session:
        return None
    agent = _mapping(_mapping(actor).get("agent")).get("id")
    return session, agent if isinstance(agent, str) else ""


def _stamp(line: Mapping[str, object]) -> datetime | None:
    ts = line.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return parse_timestamp(ts)
    except GraphCliError:
        return None


def aggregate(
    lines: Iterable[object], *, since: datetime | None = None, graphs: frozenset[str] | None = None
) -> Aggregate:
    """Fold the parsed lines of an access log. A line that is not an object of schema
    ``graph_cli.access/1`` with a timestamp is skipped and counted; so is one whose
    ``query`` or ``function`` is malformed. Lines before ``since`` and of other graphs or
    commands are left out silently."""
    found = Aggregate()
    for order, line in enumerate(lines):
        if not isinstance(line, Mapping) or line.get("schema") != ACCESS_SCHEMA:
            found.skipped += 1
            continue
        when = _stamp(line)
        if when is None:
            found.skipped += 1
            continue
        command = line.get("command")
        _first_command(found, line, when, order)
        graph = _mapping(line.get("graph"))
        graph_id = graph.get("id")
        if command in ("run", "query") and graph_id is None and (since is None or when >= since):
            found.without_graph += 1
            code = _mapping(line.get("result")).get("code")
            found.without_graph_errors[code if isinstance(code, str) else "unknown"] += 1
            continue
        if (
            command not in ("run", "query")
            or not isinstance(graph_id, str)
            or (since is not None and when < since)
            or (graphs is not None and graph_id not in graphs)
        ):
            continue
        bucket = found.graphs.setdefault(graph_id, _Graph())
        stamp = format_timestamp(when)
        if stamp >= bucket.last_ts and isinstance(graph.get("name"), str):
            bucket.name, bucket.last_ts = str(graph["name"]), stamp
        session = _session(line.get("actor"))
        result = _mapping(line.get("result"))
        failed = result.get("status") == "error"
        if command == "run":
            _fold_run(bucket, line, session, failed, result)
        elif not _fold_query(bucket, line, session, failed, stamp):
            found.skipped += 1
    return found


def _first_command(
    found: Aggregate, line: Mapping[str, object], when: datetime, order: int
) -> None:
    """Remember the earliest line of the line's session and agent, over the whole log (``since``
    and ``graphs`` do not apply: a session belongs to the window its first line is in)."""
    session, command = _session_key(line.get("actor")), line.get("command")
    if session is None or not isinstance(command, str):
        return
    known = found.sessions.get(session)
    if known is None or (when, order) < (known.first, known.order):
        found.sessions[session] = _Session(when, order, command)


def sessions_report(found: Aggregate, since: datetime | None) -> dict[str, JsonValue]:
    """The installation-wide part: the sessions whose first line is in the window, and how many
    of them began with ``graph start``."""
    firsts = [s.command for s in found.sessions.values() if since is None or s.first >= since]
    began = sum(command == START for command in firsts)
    return {
        "count": len(firsts),
        "first_start": began,
        "first_start_share": _percent(began, len(firsts)) if firsts else None,
    }


def _fold_run(
    bucket: _Graph,
    line: Mapping[str, object],
    session: str,
    failed: bool,
    result: Mapping[str, object],
) -> None:
    bucket.runs += 1
    name = _mapping(line.get("function")).get("name")
    if not isinstance(name, str):
        return  # failed before the function was named
    function = bucket.functions.setdefault(name, _Function())
    function.calls += 1
    function.sessions.add(session)
    if failed:
        code = result.get("code")
        function.errors[code if isinstance(code, str) else "unknown"] += 1


def _fold_query(
    bucket: _Graph, line: Mapping[str, object], session: str, failed: bool, stamp: str
) -> bool:
    """False for a malformed ``query`` key (the line is then not counted at all)."""
    key = line.get("query")
    if key is None:  # an older line, or a query that did not parse: no shape
        bucket.queries += 1
        bucket.unshaped += 1
        return True
    shape_hash = _mapping(key).get("shape_hash")
    if not isinstance(shape_hash, str):
        return False
    bucket.queries += 1
    shape = bucket.shapes.setdefault(shape_hash, _Shape(shape_hash))
    shape.calls += 1
    shape.errors += failed
    shape.sessions.add(session)
    text = _mapping(key).get("shape")
    if isinstance(text, str):
        shape.shape = text
    if stamp >= shape.last_ts:  # the most recent line decides the cover
        shape.last_ts = stamp
        shape.covered_by = _cover(_mapping(key).get("covered_by"))
    return True


def _cover(value: object) -> list[JsonValue]:
    covers: list[JsonValue] = []
    for item in value if isinstance(value, list) else []:
        entry = _mapping(item)
        if isinstance(entry.get("name"), str):
            covers.append({"name": str(entry["name"]), "list_fit": entry.get("list_fit") is True})
    return covers


# ---- the report -----------------------------------------------------------------------


def _capped(rows: Sequence[JsonValue], key: str, into: dict[str, JsonValue]) -> None:
    into[key] = list(rows[:CAP])
    if len(rows) > CAP:
        into[f"{key}_more"] = len(rows) - CAP


def _percent(part: int, whole: int) -> int:
    """Shares are whole percent: the JSON of the contracts has no floats."""
    return (200 * part + whole) // (2 * whole)


def _shape_row(shape: _Shape) -> dict[str, JsonValue]:
    return {
        "shape_hash": shape.shape_hash,
        "shape": shape.shape,
        "calls": shape.calls,
        "sessions": len(shape.sessions),
        "errors": shape.errors,
        "error_share": _percent(shape.errors, shape.calls),
        "covered_by": list(shape.covered_by),
    }


def graph_report(
    graph_id: str, bucket: _Graph, *, name: str | None, published: Sequence[str] | None
) -> dict[str, JsonValue]:
    """One graph's part of the report. ``published`` are the names in its function catalog
    (``None`` when it could not be read: ``never_run`` is then empty)."""
    ranked = sorted(
        bucket.shapes.values(), key=lambda s: (-len(s.sessions), -s.calls, s.shape_hash)
    )
    functions = sorted(bucket.functions.items(), key=lambda kv: (-kv[1].calls, kv[0]))
    function_rows: list[JsonValue] = [
        {
            "name": fn,
            "calls": f.calls,
            "sessions": len(f.sessions),
            "errors": dict(sorted(f.errors.items())),
        }
        for fn, f in functions
    ]
    never = sorted(set(published or ()) - bucket.functions.keys())
    covered = [s for s in ranked if s.covered_by]
    batch_gaps = [
        s for s in covered if any(_mapping(c).get("list_fit") is False for c in s.covered_by)
    ]
    candidates = [s for s in ranked if not s.covered_by and len(s.sessions) >= CANDIDATE_SESSIONS]
    total = bucket.runs + bucket.queries
    report: dict[str, JsonValue] = {
        "id": graph_id,
        "name": name or bucket.name,
        "runs": bucket.runs,
        "queries": bucket.queries,
        "unshaped_queries": bucket.unshaped,
        "function_share": _percent(bucket.runs, total) if total else None,
    }
    _capped(function_rows, "functions", report)
    _capped([*never], "never_run", report)
    _capped([_shape_row(s) for s in ranked], "shapes", report)
    _capped([_shape_row(s) for s in covered], "discovery_misses", report)
    _capped([_shape_row(s) for s in batch_gaps], "batch_gaps", report)
    _capped([_shape_row(s) for s in candidates], "candidates", report)
    return report


@dataclass(frozen=True, slots=True)
class Report:
    data: dict[str, JsonValue]
    warnings: tuple[WarningRecord, ...] = ()


def report(ctx: AdminContext, *, graph_ref: str | None, since: str | None) -> Report:
    """The report over the access log in the state root of this installation. A missing log is
    an empty report that says so; ``graph_ref`` limits it to one graph (unknown: the usual
    ``graph.not_found``)."""
    start = parse_since(since) if since is not None else None
    entry = ctx.resolve(graph_ref, "usage report") if graph_ref is not None else None
    path = ctx.roots.state_root / "logs" / ACCESS_FILE
    names = {
        str(m.entry.id): m.entry for m in ctx.manifests(include_archived=True, skip_unreadable=True)
    }
    folded = aggregate(
        read_lines(path), since=start, graphs=frozenset({str(entry.id)}) if entry else None
    )
    warnings: list[WarningRecord] = []
    wanted = [str(entry.id)] if entry else sorted(set(folded.graphs) | set(names))
    rows: list[JsonValue] = []
    for graph_id in wanted:
        known = names.get(graph_id)
        published = _published(ctx, known, warnings) if known is not None else None
        bucket = folded.graphs.get(graph_id)
        if bucket is None:
            if not published:  # nothing logged and nothing published: no row
                continue
            bucket = _Graph()
        rows.append(
            graph_report(
                graph_id,
                bucket,
                name=known.name if known is not None else None,
                published=published,
            )
        )
    rows.sort(key=lambda r: (str(_mapping(r).get("name")), str(_mapping(r).get("id"))))
    data: dict[str, JsonValue] = {
        "since": format_timestamp(start) if start is not None else None,
        "access_log": path.exists(),
        "skipped_lines": folded.skipped,
        "without_graph": {
            "calls": folded.without_graph,
            "errors": dict(sorted(folded.without_graph_errors.items())),
        },
        "sessions": sessions_report(folded, start),
        "graphs": rows,
    }
    return Report(data, tuple(warnings))


def _published(
    ctx: AdminContext, entry: GraphEntry, warnings: list[WarningRecord]
) -> list[str] | None:
    """Names in the graph's function catalog; ``None`` (with a warning) when unreadable."""
    graph = entry.id
    try:
        slot = ctx.catalog.read_functions(graph)
    except GraphCliError as exc:
        warnings.append(
            WarningRecord(exc.code, f"the function catalog of graph {graph} cannot be read")
        )
        return None
    return [str(_mapping(item).get("name")) for item in kinetic_slots.listed(slot)] if slot else []


# ---- text -----------------------------------------------------------------------------


def render(data: Mapping[str, JsonValue]) -> str:
    """The readable form: per graph the function share, top functions, top shapes (the shape
    text indented below its line) and the three derived lists."""
    lines: list[str] = []
    since = data.get("since")
    lines.append("Usage report" + (f" since {since}" if since else ""))
    if not data.get("access_log"):
        lines.append("No access log found: it is written only with [audit] access_log = true.")
    skipped = data.get("skipped_lines")
    if skipped:
        lines.append(f"{skipped} line(s) skipped (unparseable or of another schema).")
    failed = _mapping(data.get("without_graph"))
    if failed.get("calls"):
        codes = ", ".join(f"{c} x{n}" for c, n in _mapping(failed.get("errors")).items())
        lines.append(f"{failed['calls']} run/query call(s) failed before a graph was open: {codes}")
    sessions = _mapping(data.get("sessions"))
    if sessions:
        share = sessions.get("first_start_share")
        lines.append(
            f"Sessions: {sessions.get('count')}, first command was start in"
            f" {sessions.get('first_start')}" + (f" ({share}%)" if isinstance(share, int) else "")
        )
    graphs = data.get("graphs")
    for graph in graphs if isinstance(graphs, list) else []:
        lines += _render_graph(_mapping(graph))
    return "\n".join(lines)


def _list(graph: Mapping[str, object], key: str) -> list[object]:
    value = graph.get(key)
    return value if isinstance(value, list) else []


def _more(lines: list[str], graph: Mapping[str, object], key: str) -> None:
    if key + "_more" in graph:
        lines.append(f"  ... and {graph[key + '_more']} more")


def _render_graph(graph: Mapping[str, object]) -> list[str]:
    share = graph.get("function_share")
    shown = f"{share}%" if isinstance(share, int) else "n/a"
    lines = [
        "",
        f"Graph {graph.get('name')} ({graph.get('id')})",
        f"  function share {shown}: {graph.get('runs')} run(s), {graph.get('queries')} query(ies)"
        + (f", {graph['unshaped_queries']} without shape" if graph.get("unshaped_queries") else ""),
    ]
    functions = _list(graph, "functions")
    if functions:
        lines.append("  Functions (calls, sessions, errors):")
        for item in functions:
            f = _mapping(item)
            errors = _mapping(f.get("errors"))
            tail = ", ".join(f"{code} x{n}" for code, n in errors.items())
            lines.append(
                f"    {f.get('name')}: {f.get('calls')} call(s), {f.get('sessions')} session(s)"
                + (f", errors: {tail}" if tail else "")
            )
        _more(lines, graph, "functions")
    never = _list(graph, "never_run")
    if never:
        lines.append("  Published, never run: " + ", ".join(str(n) for n in never))
        _more(lines, graph, "never_run")
    shown_text: set[object] = set()  # a shape's text is printed once per graph
    for key, title in (
        ("shapes", "Query shapes by sessions"),
        ("discovery_misses", "Discovery misses (covered, still queried)"),
        ("batch_gaps", "Batch gaps (the function takes one value, the query a list)"),
        ("candidates", "Candidates (uncovered, in 2+ sessions)"),
    ):
        rows = _list(graph, key)
        if not rows:
            continue
        lines.append(f"  {title}:")
        for item in rows:
            lines += _render_shape(_mapping(item), shown_text)
        _more(lines, graph, key)
    return lines


def _render_shape(shape: Mapping[str, object], shown_text: set[object]) -> list[str]:
    covers = [_mapping(c) for c in _list(shape, "covered_by")]
    names = ", ".join(
        f"{c.get('name')}{'' if c.get('list_fit') else ' (single value)'}" for c in covers
    )
    error_share = shape.get("error_share")
    errors = f", {error_share}% errors" if isinstance(error_share, int) and error_share else ""
    head = (
        f"    {str(shape.get('shape_hash'))[:19]}: {shape.get('sessions')} session(s), "
        f"{shape.get('calls')} call(s){errors}" + (f"; covered by {names}" if names else "")
    )
    if shape.get("shape_hash") in shown_text:
        return [head]
    shown_text.add(shape.get("shape_hash"))
    text = shape.get("shape")
    body = [f"        {row}" for row in str(text).splitlines()] if isinstance(text, str) else []
    return [head, *(body or ["        (shape text not logged: above the size cap)"])]
