"""Shared pieces of the ``graph`` commands: the context, common options, request digests."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Final

from graph_cli.catalog.wiring import AppContext, AppServices
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.query.plan import DEFAULT_LIMIT

PROGRAM: Final = "graph"
GRAPH: Final = ParamSpec(
    "--graph", "graph_ref", "Graph name or id; else GRAPH_CLI_GRAPH or the pointer default."
)
AS_OF: Final = ParamSpec(
    "--as-of",
    "string",
    "Read the state after tx:<seq>, or at an RFC 3339 time; needs history access.",
)
JSON_OPTION: Final = ParamSpec("--json", "flag", "Write one JSON envelope to stdout.")
Hint = dict[str, JsonValue]
GLOBAL_OPTIONS: Final = (JSON_OPTION, GRAPH)  # also accepted before the subcommand


def services(state: CommandState) -> AppServices:
    found = state.services
    assert isinstance(found, AppServices)
    return found


def context(state: CommandState) -> AppContext:
    return services(state).context(state.warnings, state.command, state.kind)


def command_line(
    command: str,
    positional: tuple[str, ...],
    graph: str,
    options: tuple[tuple[str, tuple[str, ...]], ...],
    switches: tuple[str, ...],
    limit: int,
    offset: int | None = None,
) -> str:
    """A copy-pasteable read command; ``offset`` given for the next page."""
    parts = [PROGRAM, *command.split(), *positional, "--graph", graph]
    for flag, values in options:
        for value in values:
            parts += [flag, value]
    parts += switches
    if limit != DEFAULT_LIMIT or offset is not None:
        parts += ["--limit", str(limit)]
    if offset is not None:
        parts += ["--offset", str(offset)]
    return shlex.join(parts)


def next_page(page: dict[str, JsonValue], line: Callable[[int], str]) -> tuple[Hint, ...]:
    next_offset = page["next_offset"]
    if not isinstance(next_offset, int):
        return ()
    return ({"text": "More results available", "command": line(next_offset)},)
