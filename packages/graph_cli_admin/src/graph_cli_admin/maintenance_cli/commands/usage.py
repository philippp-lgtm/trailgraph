"""``graph-admin usage report``: how agents use the graphs, read from the access log (U6)."""

from __future__ import annotations

from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, array, nullable, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import DATA_ROOT, GRAPH_ERRORS, admin_context
from graph_cli_admin.services_admin import usage

GRAPH: Final = ParamSpec(
    "graph",
    "graph_ref",
    "Report on this graph only (name or id); without it, on every graph.",
    positional=True,
    keyword="graph_ref",
)
SINCE: Final = ParamSpec(
    "--since",
    "string",
    "Count only lines from this ISO date (midnight UTC) or RFC 3339 timestamp on, inclusive.",
)


def report(
    state: CommandState, graph_ref: str | None, since: str | None, data_root: str | None
) -> Result:
    outcome = usage.report(admin_context(state, data_root), graph_ref=graph_ref, since=since)
    hints: list[dict[str, str]] = []
    if outcome.data["access_log"] and since is None:
        graph = graph_ref or "<graph>"
        hints.append(
            {
                "text": "Count only recent use, e.g. since the last functions change.",
                "command": f"graph-admin usage report {graph} --since <date>",
            }
        )
    return Result(
        data=outcome.data,
        warnings=outcome.warnings,
        hints=tuple(hints),
        text=usage.render(outcome.data),
    )


_COVER = obj({"name": STRING, "list_fit": BOOLEAN})
_SHAPE = obj(  # services_admin.usage._shape_row
    {
        "shape_hash": STRING,
        "shape": nullable(STRING),  # null above the log's size cap
        "calls": INTEGER,
        "sessions": INTEGER,
        "errors": INTEGER,
        "error_share": INTEGER,  # whole percent of the calls
        "covered_by": array(_COVER),
    }
)
_FUNCTION = obj(
    {
        "name": STRING,
        "calls": INTEGER,
        "sessions": INTEGER,
        "errors": obj(extra=INTEGER),  # by error code
    }
)


REPORT_OUTPUT: Final = obj(  # services_admin.usage.report
    {
        "since": nullable(STRING),
        "access_log": BOOLEAN,
        "skipped_lines": INTEGER,
        # run and query calls that failed before a graph was open, by error code
        "without_graph": obj({"calls": INTEGER, "errors": obj(extra=INTEGER)}),
        # installation-wide: sessions (by session id) whose first logged command is in the window,
        # and how many of them began with `graph start`; whole percent, null without sessions
        "sessions": obj(
            {"count": INTEGER, "first_start": INTEGER, "first_start_share": nullable(INTEGER)}
        ),
        "graphs": array(
            obj(
                {
                    "id": STRING,
                    "name": STRING,
                    "runs": INTEGER,
                    "queries": INTEGER,
                    "unshaped_queries": INTEGER,
                    "function_share": nullable(INTEGER),  # whole percent: runs / (runs + queries)
                    "functions": array(_FUNCTION),
                    "never_run": array(STRING),
                    "shapes": array(_SHAPE),
                    "discovery_misses": array(_SHAPE),
                    "batch_gaps": array(_SHAPE),
                    "candidates": array(_SHAPE),
                },
                {
                    f"{name}_more": INTEGER  # absent when nothing was cut
                    for name in (
                        "functions",
                        "never_run",
                        "shapes",
                        "discovery_misses",
                        "batch_gaps",
                        "candidates",
                    )
                },
            )
        ),
    }
)

SPECS: Final = (
    CommandSpec(
        path=("usage", "report"),
        kind="read",
        surface="trace",
        action=None,
        summary="Report function runs and query shapes from the access log, per graph.",
        params=(GRAPH, SINCE, DATA_ROOT),
        examples=(
            Example("graph-admin usage report", "Every graph, all lines of the access log."),
            Example(
                "graph-admin usage report <graph> --since 2026-01-01",
                "One graph, from a date on.",
            ),
            Example("graph-admin usage report --json", "The report as JSON."),
        ),
        errors=(*GRAPH_ERRORS, "usage.invalid_argument"),
        handler=report,
        output=REPORT_OUTPUT,
        description="Reads the access log of the installation it runs on (in remote mode, the"
        " server's). Per graph: function runs (calls, distinct sessions, errors by code) and the"
        " published functions never run; query shapes ranked by distinct sessions; and three"
        " lists derived from them: discovery misses (a covered shape still queried), batch gaps"
        " (the covering function takes one value where the query took a list) and candidates"
        " (uncovered shapes in at least two sessions). Shapes only, no values; sessions are"
        " counted, never listed. Each list shows 20 entries, with `<list>_more` when cut. Apart"
        " from the graphs, `sessions` counts the session ids whose first logged command is in the"
        " window and how many began with `graph start` (not narrowed by GRAPH).",
    ),
)
