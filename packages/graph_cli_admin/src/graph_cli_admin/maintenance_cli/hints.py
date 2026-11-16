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


def _pull_force(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Repeat ``graph pull`` with ``--force``."""
    words = [details.get(key) for key in ("graph", "dir")]
    if not all(isinstance(word, str) for word in words):
        return None
    graph, target = (shlex.quote(str(word)) for word in words)
    return f"{program} graph pull {graph} {target} --force"


def _view_any_port(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Let the system pick a free port."""
    graph = details.get("graph")
    if not isinstance(graph, str):
        return None
    return f"{program} view --graph {shlex.quote(graph)} --port 0"


def _token_list(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} token list"


def _enroll_list(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} enroll list"


def _policy_show(details: Mapping[str, JsonValue], program: str) -> str | None:
    """The installed installation policy lists the declared roles."""
    return f"{program} policy show"


def _check_ontology(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Preview a migration against the data before the release."""
    return f"{program} graph check <graph> --ontology <dir>"


def _policy_apply(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Name the old and the new term in the graph's policy before the release."""
    return f"{program} policy apply <file> --graph <graph> --reason <reason>"


def _release_again(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Finish an interrupted migration with the release that started it."""
    return f"{program} ontology release <graph> <dir>"


ADMIN_HINTS: Final[Mapping[str, Hint | None]] = {
    "admin.model_invalid": None,
    "admin.version_bump_insufficient": None,
    "admin.release_exists": None,
    "admin.graph_name_taken": _graph_list,
    "admin.migration_required": _check_ontology,
    "admin.migration_unexpected": None,  # the message names the versions: bump the major
    "admin.migration_from_mismatch": None,  # details.expected is the version for from:
    "admin.migration_incomplete": _release_again,
    "admin.policy_renamed_term": _policy_apply,
    "migration.invalid": None,  # details.problems names each path to fix
    "ontology.rename_invalid": None,  # details.problems names each renamed_from to fix
    "ontology.marking_name_reused": None,  # details.reuses names each attribute to fix
    "admin.integrity_errors": None,
    "admin.store_check_failed": None,  # details.checks says what is wrong, check by check
    "admin.data_errors": None,  # the entities to fix are in details.findings
    "admin.policy_lockout": None,  # details.rule names the rule that locks out
    "admin.skill_invalid": None,  # the files to fix are in details.problems
    "admin.functions_invalid": None,  # the problems to fix are in details.problems
    "admin.actions_invalid": None,  # the problems to fix are in details.problems
    "admin.functions_broken": None,  # details.problems names the functions to fix or delete
    "admin.actions_broken": None,  # details.problems names the actions to fix or delete
    "admin.skill_render_exists": _render_force,
    "admin.source_exists": _pull_force,
    "admin.source_conflict": None,  # the message says to pull into another directory
    "admin.workspace_conflict": None,  # details.folders names the folders to move away
    "admin.view_port_unavailable": _view_any_port,
    "admin.remote_mode": None,  # the message says where to run it
    "admin.token_exists": _token_list,
    "admin.token_not_found": _token_list,
    "admin.role_unknown": _policy_show,
    "admin.enrollment_not_found": _enroll_list,
}


def register() -> None:
    register_hints(ADMIN_HINTS)
