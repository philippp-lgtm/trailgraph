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
GRAPH_ERRORS: Final = (
    "graph.not_selected",
    "graph.not_found",
    "graph.not_in_project",
    "config.invalid",
    "config.forbidden_key",
    "access.denied",
    "ontology.artifact_missing",
)


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
    *,
    default_limit: int = DEFAULT_LIMIT,
) -> str:
    """A copy-pasteable read command; ``offset`` given for the next page; ``--limit`` is left
    out when it is ``default_limit`` and there is no offset."""
    dashed = any(value.startswith("-") for value in positional)
    # A positional that looks like an option goes after "--", so options stay in front.
    parts = [PROGRAM, *command.split(), *(() if dashed else positional), "--graph", graph]
    for flag, values in options:
        for value in values:
            parts += [flag, value]
    parts += switches
    if limit != default_limit or offset is not None:
        parts += ["--limit", str(limit)]
    if offset is not None:
        parts += ["--offset", str(offset)]
    if dashed:
        parts += ["--", *positional]
    return shlex.join(parts)


def next_page(page: dict[str, JsonValue], line: Callable[[int], str]) -> tuple[Hint, ...]:
    next_offset = page["next_offset"]
    if not isinstance(next_offset, int):
        return ()
    return ({"text": "More results available", "command": line(next_offset)},)
