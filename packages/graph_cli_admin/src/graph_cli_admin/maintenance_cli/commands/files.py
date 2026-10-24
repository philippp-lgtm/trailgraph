"""``graph-admin files verify|gc``: the stored files of a graph."""

from __future__ import annotations

from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    GRAPH_OPTION,
    admin_context,
)
from graph_cli_admin.services_admin import files, graphs


def verify(state: CommandState, graph_ref: str, rehash: bool, data_root: str | None) -> Result:
    outcome = files.verify(admin_context(state, data_root), graph_ref=graph_ref, rehash=rehash)
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


def gc(
    state: CommandState, graph_ref: str, reason: str | None, dry_run: bool, data_root: str | None
) -> Result:
    outcome = files.gc(
        admin_context(state, data_root), graph_ref=graph_ref, reason=reason, dry_run=dry_run
    )
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


SPECS: Final = (
    CommandSpec(
        path=("files", "verify"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="Check that every file a tx names is stored; list orphans and staging leftovers.",
        params=(
            GRAPH_OPTION,
            ParamSpec("--rehash", "flag", "Read every stored file and compare its hash."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin files verify --graph <graph>", "Check the stored files."),
            Example(
                "graph-admin files verify --graph <graph> --rehash --json",
                "Also compare every file's hash.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "access.denied", "admin.integrity_errors"),
        handler=verify,
        description="Exits 1 (admin.integrity_errors) on a missing or, with --rehash, damaged"
        " file; orphans and staging leftovers are info findings (files gc removes them).",
    ),
    CommandSpec(
        path=("files", "gc"),
        kind="write",
        surface="act",
        action="maint.repair",
        summary="Remove stored files no tx names and staging files older than one hour.",
        params=(
            GRAPH_OPTION,
            ParamSpec(
                "--reason", "string", "Why the files are removed; recorded in the audit event."
            ),
            ParamSpec("--dry-run", "flag", "Show what would be removed without removing it."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin files gc --graph <graph> --dry-run", "Show what would go."),
            Example(
                "graph-admin files gc --graph <graph> --reason <reason> --json",
                "Remove them, with a reason.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS),
        handler=gc,
        description="Files that any revision ever named stay, history included. The removal"
        " is audited (maint.repair); nothing to remove writes nothing.",
    ),
)
