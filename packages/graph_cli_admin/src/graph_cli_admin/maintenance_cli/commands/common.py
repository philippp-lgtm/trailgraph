"""Parameters, error lists and helpers shared by every graph-admin command."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import ParamSpec
from graph_cli_admin.services_admin.context import AdminContext

DATA_ROOT: Final = ParamSpec(
    "--data-root",
    "directory",
    "Data root to use instead of GRAPH_CLI_ROOT, config or the XDG default.",
)
GRAPH_OPTION: Final = ParamSpec(
    "--graph", "graph_ref", "Graph name or id.", required=True, keyword="graph_ref"
)

# Every command that opens the data root can meet a broken config; one that names a
# graph, a missing graph; one that changes something, a denial, a held lock and an
# unwritable audit log.
ROOT_ERRORS: Final = ("config.invalid", "admin.remote_mode")
GRAPH_ERRORS: Final = (*ROOT_ERRORS, "graph.not_found")
CHANGE_ERRORS: Final = ("access.denied", "backend.lock_timeout", "audit.unavailable")


# A release that carries or implies a data migration (plan M1-M3) can be refused for these.
MIGRATION_ERRORS: Final = (
    "migration.invalid",
    "admin.migration_unexpected",
    "admin.migration_from_mismatch",
    "admin.migration_incomplete",
)


def admin_context(state: CommandState, data_root: str | None) -> AdminContext:
    context: AdminContext = state.services.context(
        data_root=path(data_root), warnings=state.warnings
    )
    return context


def path(value: str | None) -> Path | None:
    return None if value is None else Path(value)
