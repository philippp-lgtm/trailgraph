"""Hints for the admin-only error codes (contracts B9.9); registered at startup."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Final

from graph_cli.cli_kit.hints import Hint, register_hints
from graph_cli.kernel.canonical import JsonValue


def _graph_list(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} graph list"


def _render_force(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Repeat ``graph skill render`` with ``--force`` (Q5)."""
    words = [details.get(key) for key in ("graph", "dir", "out")]
    if not all(isinstance(word, str) for word in words):
        return None
    graph, source, out = (shlex.quote(str(word)) for word in words)
    return f"{program} graph skill render {graph} {source} --out {out} --force"


def _view_any_port(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Let the system pick a free port."""
    graph = details.get("graph")
    if not isinstance(graph, str):
        return None
    return f"{program} view --graph {shlex.quote(graph)} --port 0"


ADMIN_HINTS: Final[Mapping[str, Hint | None]] = {
    "admin.model_invalid": None,
    "admin.version_bump_insufficient": None,
    "admin.release_exists": None,
    "admin.graph_name_taken": _graph_list,
    "admin.migration_required": None,
    "admin.integrity_errors": None,
    "admin.skill_invalid": None,  # the files to fix are in details.problems
    "admin.skill_render_exists": _render_force,
    "admin.view_port_unavailable": _view_any_port,
}


def register() -> None:
    register_hints(ADMIN_HINTS)
