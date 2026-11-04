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
