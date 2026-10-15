"""Shared pieces of the ``graph`` commands: the context, common options, request digests."""

from __future__ import annotations

from typing import Final

from graph_cli.catalog.wiring import AppContext, AppServices
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import ParamSpec

PROGRAM: Final = "graph"
GRAPH: Final = ParamSpec(
    "--graph", "graph_ref", "Graph name or id; else GRAPH_CLI_GRAPH or the pointer default."
)
AS_OF: Final = ParamSpec("--as-of", "string", "Read an earlier state (tx:<seq>); reserved.")
JSON_OPTION: Final = ParamSpec("--json", "flag", "Write one JSON envelope to stdout.")


def services(state: CommandState) -> AppServices:
    found = state.services
    assert isinstance(found, AppServices)
    return found


def context(state: CommandState) -> AppContext:
    return services(state).context(state.warnings)
