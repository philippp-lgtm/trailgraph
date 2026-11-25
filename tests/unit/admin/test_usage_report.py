"""U: the aggregation behind ``graph-admin usage report`` over a synthetic access log (usage
shapes plan U6): sessions, covered shapes, batch gaps, candidates, ``--since``, older lines,
foreign and corrupt lines, and the caps."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.services_admin import usage

G1 = "0192f3c0-0000-7000-8000-000000000001"
G2 = "0192f3c0-0000-7000-8000-000000000002"


def line(
    command: str,
    *,
    session: str | None = "s1",
    agent: str | None = None,
    ts: str = "2026-11-02T10:00:00.000Z",
    graph: str | None = G1,
    name: str = "example_graph",
    code: str | None = None,
    function: str | None = None,
    shape: str | None = None,
    text: str | None = "MATCH (v1:Item) RETURN v1.name AS c1",
    covered: Sequence[tuple[str, bool]] = (),
    shaped: bool = True,
) -> dict[str, Any]:
    actor: dict[str, Any] = {"principal": {"assurance": "asserted", "id": "someone"}}
    if session is not None:
        actor["session"] = {"assurance": "asserted", "id": session}
    if agent is not None:
        actor["agent"] = {"assurance": "asserted", "id": agent}
    out: dict[str, Any] = {
        "schema": "graph_cli.access/1",
        "ts": ts,
        "command": command,
        "actor": actor,
        "graph": {"id": graph, "name": name, "seq": 1} if graph else None,
        "result": {"status": "error", "code": code} if code else {"status": "ok"},
    }
    if function is not None:
        out["function"] = {"name": function, "hash": "sha256:f"}
    if command == "query" and shaped:
        out["query"] = {
            "shape_hash": shape or "sha256:a",
            "shape": text,
            "covered_by": [{"name": n, "list_fit": fit} for n, fit in covered],
        }
    return out


def graph_of(found: usage.Aggregate, published: Sequence[str] | None = None) -> dict[str, Any]:
    (graph_id,) = found.graphs
    report = usage.graph_report(graph_id, found.graphs[graph_id], name=None, published=published)
    return dict(report)


def test_functions_count_calls_distinct_sessions_and_errors_by_code() -> None:
    lines = [
        line("run", function="items_in_location", session="s1"),
        line("run", function="items_in_location", session="s1"),
        line("run", function="items_in_location", session="s2", code="entity.not_found"),
        line("run", function="items_in_location", session="s2", code="entity.not_found"),
        line("run", function="items_in_location", session="s3", code="query.timeout"),
        line("run", function="other", session="s1"),
        line("run", function=None, session="s1", code="function.not_found"),  # never named
    ]
    report = graph_of(usage.aggregate(lines), published=["items_in_location", "other", "idle"])
    assert report["functions"] == [
        {
            "name": "items_in_location",
            "calls": 5,
            "sessions": 3,
            "errors": {"entity.not_found": 2, "query.timeout": 1},
        },
        {"name": "other", "calls": 1, "sessions": 1, "errors": {}},
    ]
    assert report["never_run"] == ["idle"]
    assert report["runs"] == 7  # the unnamed run counts for the share only
    assert "functions_more" not in report and "never_run_more" not in report


def test_shapes_rank_by_sessions_then_calls_then_hash() -> None:
    lines = [
        line("query", shape="sha256:c", session="s1"),
        line("query", shape="sha256:c", session="s1"),
        line("query", shape="sha256:c", session="s1"),
        line("query", shape="sha256:b", session="s1"),
        line("query", shape="sha256:b", session="s2"),
        line("query", shape="sha256:a", session="s1"),
        line("query", shape="sha256:a", session="s2"),
    ]
    shapes = graph_of(usage.aggregate(lines))["shapes"]
    # b and a: two sessions, two calls: the hash decides; c: three calls but one session
    assert [(s["shape_hash"], s["sessions"], s["calls"]) for s in shapes] == [
        ("sha256:a", 2, 2),
        ("sha256:b", 2, 2),
        ("sha256:c", 1, 3),
    ]


def test_error_share_and_function_share() -> None:
    lines = [
        line("query", code="query.parse_error"),
        line("query"),
        line("query"),
        line("query"),
        line("run", function="f"),
    ]
    report = graph_of(usage.aggregate(lines))
    (shape,) = report["shapes"]
    assert (shape["calls"], shape["errors"], shape["error_share"]) == (4, 1, 25)
    assert (report["runs"], report["queries"], report["function_share"]) == (1, 4, 20)


def test_derived_lists() -> None:
    covered = [("items_in_location", True)]
    gap = [("members_of_unit", False), ("other", True)]
    lines = [
        line("query", shape="sha256:covered", covered=covered, session="s1"),
        line("query", shape="sha256:gap", covered=gap, session="s1"),
        line("query", shape="sha256:gap", covered=gap, session="s2"),
        line("query", shape="sha256:cand", session="s1"),
        line("query", shape="sha256:cand", session="s2"),
        line("query", shape="sha256:solo", session="s1"),  # uncovered, one session only
        line("query", shape="sha256:solo", session="s1"),
    ]
    report = graph_of(usage.aggregate(lines))
    hashes = lambda key: [s["shape_hash"] for s in report[key]]  # noqa: E731
    assert hashes("discovery_misses") == ["sha256:gap", "sha256:covered"]
    assert hashes("batch_gaps") == ["sha256:gap"]
    assert hashes("candidates") == ["sha256:cand"]
    assert hashes("shapes") == ["sha256:cand", "sha256:gap", "sha256:solo", "sha256:covered"]


def test_the_most_recent_line_of_a_shape_decides_its_cover() -> None:
    lines = [
        line("query", shape="sha256:x", ts="2026-11-03T00:00:00.000Z", covered=[("new", True)]),
        line("query", shape="sha256:x", ts="2026-11-01T00:00:00.000Z"),  # written earlier
    ]
    (shape,) = graph_of(usage.aggregate(lines))["shapes"]
    assert shape["covered_by"] == [{"name": "new", "list_fit": True}]


def test_since_is_inclusive_and_utc() -> None:
    lines = [
        line("run", function="f", ts="2026-11-01T23:59:59.999Z"),
        line("run", function="f", ts="2026-11-02T00:00:00.000Z"),
        line("query", ts="2026-11-02T08:00:00.000Z"),
    ]
    since = usage.parse_since("2026-11-02")
    assert since == datetime(2026, 11, 2, tzinfo=UTC)
    report = graph_of(usage.aggregate(lines, since=since))
    assert (report["runs"], report["queries"]) == (1, 1)
    stamped = usage.parse_since("2026-11-02T10:00:00+02:00")  # 08:00 UTC, inclusive
    assert graph_of(usage.aggregate(lines, since=stamped))["queries"] == 1
    assert usage.aggregate(lines, since=usage.parse_since("2026-11-03")).graphs == {}


@pytest.mark.parametrize("text", ["yesterday", "2026-13-01", "2026-11-02T10:00:00"])
def test_a_bad_since_is_a_usage_error(text: str) -> None:
    with pytest.raises(GraphCliError) as caught:
        usage.parse_since(text)
    assert caught.value.code == "usage.invalid_argument"


def test_older_lines_without_a_query_key_count_as_unshaped_queries() -> None:
    lines = [line("query", shaped=False), line("query"), line("run", function="f")]
    report = graph_of(usage.aggregate(lines))
    assert (report["queries"], report["unshaped_queries"], report["runs"]) == (2, 1, 1)
    assert report["function_share"] == 33
    assert len(report["shapes"]) == 1


def test_foreign_corrupt_and_malformed_lines_are_skipped_and_counted() -> None:
    foreign = line("run", function="f") | {"schema": "graph_cli.access/2"}
    no_time = line("run", function="f") | {"ts": "soon"}
    bad_query = line("query") | {"query": {"shape": "x"}}
    lines: list[object] = [
        None,  # not JSON
        ["a list"],
        foreign,
        no_time,
        bad_query,
        line("run", function="f"),
        line("get"),  # another command: ignored, not skipped
        line("run", function="f", graph=None, code="x.y"),  # failed before a graph: counted apart
    ]
    found = usage.aggregate(lines)
    assert found.skipped == 5
    assert (found.without_graph, dict(found.without_graph_errors)) == (1, {"x.y": 1})
    assert graph_of(found)["runs"] == 1


def test_graphs_are_kept_apart_and_filterable() -> None:
    lines = [
        line("run", function="f", graph=G1, name="old_name", ts="2026-11-01T00:00:00.000Z"),
        line("run", function="f", graph=G1, name="new_name", ts="2026-11-02T00:00:00.000Z"),
        line("run", function="g", graph=G2, name="second"),
    ]
    found = usage.aggregate(lines)
    assert set(found.graphs) == {G1, G2}
    assert found.graphs[G1].name == "new_name"  # the latest name in the log
    only = usage.aggregate(lines, graphs=frozenset({G2}))
    assert set(only.graphs) == {G2}


def test_a_line_without_a_session_shares_one_bucket() -> None:
    lines = [line("query", session=None), line("query", session=None), line("query", session="s1")]
    (shape,) = graph_of(usage.aggregate(lines))["shapes"]
    assert shape["sessions"] == 2  # the shared bucket and s1, never one per line


def test_no_report_names_a_session() -> None:
    lines = [line("run", function="f", session="session-marker-9"), line("query", session="s9")]
    text = repr(graph_of(usage.aggregate(lines)))
    assert "session-marker-9" not in text and "s9" not in text


def test_lists_are_capped_with_a_sibling_count() -> None:
    n = usage.CAP + 3
    lines = [line("run", function=f"fn_{i:02d}") for i in range(n)]
    lines += [line("query", shape=f"sha256:{i:02d}", session="s1") for i in range(n)]
    lines += [line("query", shape=f"sha256:{i:02d}", session="s2") for i in range(n)]
    report = graph_of(usage.aggregate(lines), published=[f"idle_{i:02d}" for i in range(n)])
    for key in ("functions", "never_run", "shapes", "candidates"):
        assert len(report[key]) == usage.CAP, key
        assert report[f"{key}_more"] == 3, key
    assert "batch_gaps_more" not in report and report["batch_gaps"] == []


def test_the_text_shows_the_summary_and_indents_the_shape() -> None:
    lines = [
        line("run", function="f", session="s1"),
        line("query", shape="sha256:" + "a" * 64, session="s1", covered=[("f", False)]),
        line("query", shape="sha256:" + "b" * 64, session="s1", text=None),
    ]
    found = usage.aggregate(lines)
    (graph_id,) = found.graphs
    graph = usage.graph_report(
        graph_id, found.graphs[graph_id], name="example_graph", published=["g"]
    )
    out = usage.render({"since": None, "access_log": True, "skipped_lines": 0, "graphs": [graph]})
    assert "function share 33%: 1 run(s), 2 query(ies)" in out
    assert "Published, never run: g" in out
    assert "        MATCH (v1:Item) RETURN v1.name AS c1" in out
    assert "covered by f (single value)" in out
    assert "Batch gaps" in out and "shape text not logged" in out


# ---- sessions that began with `graph start` (installation-wide) -----------------------


def ts(minute: int, second: int = 0) -> str:
    return f"2026-11-02T10:{minute:02d}:{second:02d}.000Z"


def sessions(lines: list[dict[str, Any]], since: str | None = None) -> dict[str, Any]:
    start = usage.parse_since(since) if since else None
    return dict(usage.sessions_report(usage.aggregate(lines, since=start), start))


def test_a_session_counts_and_began_with_start_when_its_first_line_is_start() -> None:
    lines = [
        line("start", session="a", ts=ts(0), graph=None),
        line("get", session="a", ts=ts(1), graph=None),
        line("whoami", session="b", ts=ts(0), graph=None),
        line("start", session="b", ts=ts(1), graph=None),  # later: does not count
        line("start", session="c", ts=ts(2), graph=None),
    ]
    assert sessions(lines) == {"count": 3, "first_start": 2, "first_start_share": 67}


def test_the_earliest_line_decides_not_the_order_in_the_file() -> None:
    lines = [line("get", session="a", ts=ts(5)), line("start", session="a", ts=ts(1), graph=None)]
    assert sessions(lines)["first_start"] == 1
    same = [line("get", session="a", ts=ts(1)), line("start", session="a", ts=ts(1), graph=None)]
    assert sessions(same) == {"count": 1, "first_start": 0, "first_start_share": 0}  # file order


def test_a_failed_start_is_still_the_first_command() -> None:
    lines = [line("start", session="a", graph=None, code="graph.not_found")]
    assert sessions(lines)["first_start"] == 1


def test_lines_without_a_session_id_are_not_sessions() -> None:
    named = line("start", session=None, graph=None)
    named["actor"]["session"] = {"assurance": "asserted", "name": "only-a-name"}
    lines = [line("start", session=None, graph=None), named, line("start", session="", graph=None)]
    assert sessions(lines) == {"count": 0, "first_start": 0, "first_start_share": None}
    assert sessions([]) == {"count": 0, "first_start": 0, "first_start_share": None}


def test_a_session_belongs_to_the_window_its_first_line_is_in() -> None:
    lines = [
        line("get", session="old", ts=ts(0)),  # began before: not counted, even though...
        line("start", session="old", ts=ts(30), graph=None),  # ...this line is in the window
        line("start", session="edge", ts=ts(10), graph=None),  # inclusive
        line("get", session="new", ts=ts(20)),
    ]
    found = sessions(lines, since="2026-11-02T10:10:00Z")
    assert found == {"count": 2, "first_start": 1, "first_start_share": 50}


def test_the_graph_filter_and_other_commands_do_not_narrow_the_sessions() -> None:
    lines = [
        line("start", session="a", graph=None),
        line("run", session="b", function="f", graph=G2),
        line("query", session="c", graph=G1),
    ]
    found = usage.aggregate(lines, graphs=frozenset({G1}))
    assert set(found.graphs) == {G1}
    assert usage.sessions_report(found, None)["count"] == 3
    assert found.skipped == 0 and found.without_graph == 0  # a start line is no skipped line


def test_the_sessions_part_names_no_session_and_the_text_says_it() -> None:
    lines = [line("start", session="session-marker-7", graph=None), line("get", session="s8")]
    part = sessions(lines)
    assert "session-marker-7" not in repr(part) and "s8" not in repr(part)
    base = {"since": None, "access_log": True, "skipped_lines": 0, "graphs": []}
    out = usage.render({**base, "sessions": part})
    assert "Sessions: 2, first command was start in 1 (50%)" in out
    assert "Sessions" not in usage.render(base)  # an older answer has no such part
    empty = usage.render({**base, "sessions": sessions([])})
    assert "Sessions: 0, first command was start in 0" in empty and "%" not in empty


def test_each_agent_of_a_session_counts_on_its_own() -> None:
    """A subagent shares the session id of its parent and has an agent id of its own; the skills
    tell every agent to start, so the report asks it of each. Without an agent id the session
    stands alone (the main agent, a harness that names none)."""
    lines = [
        line("get", session="a", ts=ts(0), graph=None),  # the main agent: no agent id
        line("start", session="a", agent="sub-1", ts=ts(1), graph=None),
        line("get", session="a", agent="sub-2", ts=ts(2), graph=None),
        line("start", session="a", agent="sub-2", ts=ts(3), graph=None),  # not its first
        line("start", session="b", agent="main", ts=ts(0), graph=None),
    ]
    assert sessions(lines) == {"count": 4, "first_start": 2, "first_start_share": 50}
    # the same agent id under two sessions is two agents; an empty agent id is none
    twice = [
        line("start", session="a", agent="x", ts=ts(0), graph=None),
        line("get", session="b", agent="x", ts=ts(0), graph=None),
        line("get", session="c", agent="", ts=ts(0), graph=None),
    ]
    assert sessions(twice)["count"] == 3


def test_a_run_made_by_a_call_counts_like_any_other_run() -> None:
    """``graph call`` is logged as the command it ran, with ``via: call``."""
    called = {**line("run", function="items_in_location", session="s2"), "via": "call"}
    lines = [line("run", function="items_in_location", session="s1"), called]
    report = graph_of(usage.aggregate(lines), published=["items_in_location", "idle"])
    assert report["functions"][0]["calls"] == 2 and report["functions"][0]["sessions"] == 2
    assert report["never_run"] == ["idle"]
