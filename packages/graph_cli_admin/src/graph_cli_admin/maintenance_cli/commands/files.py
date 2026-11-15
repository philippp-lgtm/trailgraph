"""``graph-admin files verify|gc`` (the stored files of a graph) and ``files store check``
(the configured object store)."""

from __future__ import annotations

from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    GRAPH_OPTION,
    ROOT_ERRORS,
    admin_context,
)
from graph_cli_admin.maintenance_cli.output_schemas import STRINGS
from graph_cli_admin.services_admin import files, graphs, store_check


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


def check_store(state: CommandState, data_root: str | None) -> Result:
    return Result(data=store_check.check(admin_context(state, data_root)))


VERIFY_OUTPUT: Final = obj(  # services_admin.files.verify
    {
        "graph_id": STRING,
        "rehash": BOOLEAN,
        "stored": obj({"files": INTEGER, "bytes": INTEGER}),
        "counts": ref("finding_counts"),
        "findings": array(ref("finding")),
    }
)
BLOBS: Final = array(obj({"hash": STRING, "size": INTEGER}))  # audit.events.blob_list
GC_OUTPUT: Final = obj(  # services_admin.files.gc
    {
        "graph_id": STRING,
        "blobs_removed": BLOBS,
        "staging_removed": STRINGS,
        "misplaced_removed": BLOBS,
        "misplaced_kept": BLOBS,
        "dry_run": BOOLEAN,
        "removed": BOOLEAN,
    }
)
STORE_CHECK_OUTPUT: Final = obj(  # services_admin.store_check.check
    {
        "configured": BOOLEAN,
        "files_store": enum("local", "s3"),
        "store": nullable(
            obj(
                {
                    "bucket": STRING,
                    "prefix": STRING,
                    "region": STRING,
                    "endpoint_host": STRING,
                    "addressing": enum("path", "virtual"),
                }
            )
        ),
        "store_id": nullable(STRING),
        "graphs": array(obj({"id": STRING, "name": STRING})),
        "skew_s": nullable(INTEGER),
        "checks": array(
            obj(
                {
                    "name": STRING,
                    "status": enum("ok", "warning", "error", "skipped"),
                    "message": STRING,
                }
            )
        ),
    }
)


SPECS: Final = (
    CommandSpec(
        path=("files", "verify"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="Check that every file a tx names is stored; list orphans, staging leftovers"
        " and misplaced copies.",
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
        output=VERIFY_OUTPUT,
        description="Exits 1 (admin.integrity_errors) on a missing or, with --rehash, damaged"
        " file, or when the object store holding the graph's files cannot be reached"
        " (blob_store_unavailable); orphans, staging leftovers and copies outside the"
        " graph's location (blob_misplaced) are info findings (files gc removes them).",
    ),
    CommandSpec(
        path=("files", "gc"),
        kind="write",
        surface="act",
        action="maint.repair",
        summary="Remove stored files no tx names, staging files older than one hour and"
        " misplaced copies.",
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
        output=GC_OUTPUT,
        description="Files that any revision ever named stay, history included; in an object"
        " store only files older than 24 hours by the store's clock go. A copy outside the"
        " graph's location goes only when that location's copy, read in full, has the same"
        " hash (misplaced_kept lists the others). The removal is audited (maint.repair); nothing"
        " to remove writes nothing.",
    ),
    CommandSpec(
        path=("files", "store", "check"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="Check the configured object store: settings, marker, permissions, clock.",
        params=(DATA_ROOT,),
        examples=(
            Example("graph-admin files store check", "Check the object store."),
            Example("graph-admin files store check --json", "Every check as JSON."),
        ),
        errors=(*ROOT_ERRORS, "access.denied", "admin.store_check_failed"),
        handler=check_store,
        output=STORE_CHECK_OUTPUT,
        description="Writes and removes one probe object below graph_cli_probe/ and records"
        " no audit event. Exits 1 (admin.store_check_failed) when a check fails; warnings"
        " (no store marker yet, a clock that drifts) do not fail. Without a configured"
        " object store it says so and succeeds.",
    ),
)
