"""Hints for the admin-only error codes (contracts B9.9); registered at startup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.cli_kit.hints import Hint, register_hints
from graph_cli.kernel.canonical import JsonValue


def _graph_list(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} graph list"


ADMIN_HINTS: Final[Mapping[str, Hint | None]] = {
    "admin.model_invalid": None,
    "admin.version_bump_insufficient": None,
    "admin.release_exists": None,
    "admin.graph_name_taken": _graph_list,
    "admin.migration_required": None,
    "admin.integrity_errors": None,
}


def register() -> None:
    register_hints(ADMIN_HINTS)
