"""``graph-admin files verify|gc|move`` (the stored files of a graph) and ``files store check``
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
from graph_cli_admin.services_admin import file_extract, files, files_move, graphs, store_check


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


def move(
    state: CommandState,
    refs: tuple[str, ...],
    to: str,
    all_graphs: bool,
    dry_run: bool,
    step: str | None,
    reason: str | None,
    data_root: str | None,
) -> Result:
    outcome = files_move.move(
        admin_context(state, data_root),
        refs=refs,
        all_graphs=all_graphs,
        to=to,
        dry_run=dry_run,
        step=step,
        reason=reason,
    )
    return Result(data=outcome.data, error=outcome.error)


def extract(
    state: CommandState,
    graph_ref: str,
    files_: tuple[str, ...],
    extractors: tuple[str, ...],
    retry_failed: bool,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    outcome = file_extract.extract(
        admin_context(state, data_root),
        graph_ref=graph_ref,
        files=files_,
        extractors=extractors,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


def list_extractors(state: CommandState, data_root: str | None) -> Result:
    admin_context(state, data_root)  # the data root is read like every admin command
    return Result(data=file_extract.extractors())


def check_store(state: CommandState, data_root: str | None) -> Result:
    return Result(data=store_check.check(admin_context(state, data_root)))


MODULE_OUTDATED: Final = obj(  # services.file_metadata_plan.ModuleOutdated.to_json
    {"extractor": STRING, "module": STRING, "have": STRING, "need": STRING}
)
VERIFY_OUTPUT: Final = obj(  # services_admin.files.verify
    {
        "graph_id": STRING,
        "rehash": BOOLEAN,
        "stored": obj({"files": INTEGER, "bytes": INTEGER}),
        "counts": ref("finding_counts"),
        "findings": array(ref("finding")),
        "metadata": obj(  # services_admin.file_extract.records_behind
            {
                "behind": array(
                    obj(
                        {
                            "extractor": STRING,
                            "version": INTEGER,
                            "records": INTEGER,
                            "behind": INTEGER,
                            "newer": INTEGER,
                        }
                    )
                ),
                "module_outdated": array(MODULE_OUTDATED),
            },
            {"unavailable": STRING},
        ),
    }
)
_EXTRACTOR_TALLY: Final = obj(  # services.file_metadata.ExtractorTally.to_json
    {
        "name": STRING,
        "version": INTEGER,
        "created": INTEGER,
        "updated": INTEGER,
        "unchanged": INTEGER,
        "current": INTEGER,
        "not_applicable": INTEGER,
        "failed": INTEGER,
        "transient": INTEGER,
        "newer_version": INTEGER,
        "blocked_by_references": INTEGER,
        "problems": INTEGER,
    }
)
EXTRACT_OUTPUT: Final = obj(  # services.file_metadata.ExtractionReport.to_json
    {
        "dry_run": BOOLEAN,
        "enabled": BOOLEAN,
        "run": nullable(STRING),
        "files": INTEGER,
        "extractors": array(_EXTRACTOR_TALLY),
        "module_outdated": array(MODULE_OUTDATED),
        "regressions": array(
            obj(
                {
                    "extractor": STRING,
                    "file": STRING,
                    "from": STRING,
                    "to": STRING,
                    "problem": nullable(STRING),
                }
            )
        ),
        "regression_count": INTEGER,
        "retracted_records": INTEGER,
        "problems": array(
            obj({"extractor": STRING, "file": STRING, "code": STRING, "message": STRING})
        ),
        "problem_count": INTEGER,
        "transient": array(obj({"extractor": STRING, "file": STRING, "detail": STRING})),
        "transient_count": INTEGER,
        "newer_versions": array(
            obj(
                {
                    "extractor": STRING,
                    "file": STRING,
                    "stored_version": INTEGER,
                    "version": INTEGER,
                }
            )
        ),
        "newer_version_count": INTEGER,
    }
)
EXTRACTORS_OUTPUT: Final = obj(  # services_admin.file_extract.extractors
    {
        "extractors": array(
            obj(
                {
                    "name": STRING,
                    "version": INTEGER,
                    "accepts": STRINGS,
                    "record_class": STRING,
                    "part_class": nullable(STRING),
                    "module": obj({"id": STRING, "min_version": STRING}),
                    "summary": STRING,
                }
            )
        )
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
LOCATION: Final = enum("local", "s3")
COUNTS: Final = obj({"blobs": INTEGER, "bytes": INTEGER})
MOVE_OUTPUT: Final = obj(  # services_admin.files_move.move
    {
        "to": LOCATION,
        "dry_run": BOOLEAN,
        "step": nullable(enum("pass", "switch")),
        "graphs": array(
            obj(
                {
                    "graph_id": STRING,
                    "name": STRING,
                    "from": LOCATION,
                    "status": enum("moved", "at_target", "planned", "copying"),
                    "store_id": nullable(STRING),
                    "stored": COUNTS,
                    "copied": COUNTS,
                    "remaining": COUNTS,
                    "passes": INTEGER,
                }
            )
        ),
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
        path=("files", "move"),
        kind="write",
        surface="act",
        action="maint.graph_configure",
        summary="Move the stored files of graphs between the data root and the object store.",
        params=(
            ParamSpec(
                "ref",
                "graph_ref",
                "Graph names or ids.",
                positional=True,
                repeatable=True,
                keyword="refs",
            ),
            ParamSpec(
                "--to",
                "choice",
                "Where the stored files go: the data root or the object store.",
                required=True,
                choices=files_move.LOCATIONS,
            ),
            ParamSpec(
                "--all",
                "flag",
                "Every graph of the installation, archived ones too.",
                keyword="all_graphs",
            ),
            ParamSpec("--dry-run", "flag", "Count what would be copied without copying it."),
            ParamSpec(
                "--step",
                "choice",
                "Run one copy pass, or only the switch, each with 30 s of copying (the"
                " remote mode sends these).",
                choices=files_move.STEPS,
            ),
            ParamSpec("--reason", "string", "Why the files move; recorded in the audit event."),
            DATA_ROOT,
        ),
        examples=(
            Example(
                "graph-admin files move <graph> --to s3 --dry-run",
                "Count the files a move would copy.",
            ),
            Example(
                "graph-admin files move --all --to s3 --reason <reason> --json",
                "Move every graph into the object store.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            *CHANGE_ERRORS,
            "usage.invalid_argument",
            "backend.unavailable",
            "backend.corrupt",
            "files.store_refused",
            "admin.migration_incomplete",
            "conflict.seq_mismatch",
        ),
        handler=move,
        output=MOVE_OUTPUT,
        description="Copies every file the graph's transactions name and every other file"
        " stored there, verified, while writes go on; then, under the graph lock, copies"
        " what arrived meanwhile and switches the graph (maint.graph_configure). The old"
        " copies stay until files gc removes them. A graph already there is skipped;"
        " a graph in the middle of a data migration is refused.",
        remote="hooked",
    ),
    CommandSpec(
        path=("files", "extract"),
        kind="write",
        surface="act",
        action="maint.reindex",
        summary="Write the metadata records of stored files at the current extractor versions.",
        params=(
            GRAPH_OPTION,
            ParamSpec(
                "--file",
                "entity_ref",
                "Only this file (id, alias or trailing hex of the id); repeatable.",
                repeatable=True,
                keyword="files_",
            ),
            ParamSpec(
                "--extractor",
                "string",
                "Only this extractor (see files extractors); repeatable.",
                repeatable=True,
                keyword="extractors",
            ),
            ParamSpec(
                "--retry-failed",
                "flag",
                "Run again the extractors whose last result is failed (after a limit was raised).",
            ),
            ParamSpec(
                "--dry-run",
                "flag",
                "Run the extractors and report what would be written, without writing.",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin files extract --graph <graph> --dry-run", "See the plan."),
            Example(
                "graph-admin files extract --graph <graph> --json",
                "Write every missing or outdated record.",
            ),
            Example(
                "graph-admin files extract --graph <graph> --file <id> --extractor <name>",
                "One file, one extractor.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            *CHANGE_ERRORS,
            "entity.not_found",
            "usage.invalid_argument",
            "backend.unavailable",
        ),
        handler=extract,
        output=EXTRACT_OUTPUT,
        description="The records hold size and type, page count, image size, table shape or"
        " database schema, depending on the kinds the graph imports. Each file is read once;"
        " the `format` extractor runs first and decides which of the others apply. A second"
        " run writes nothing. One tx per file and extractor names the extractor and its"
        " version as its source. Records whose extractor is newer than this program are left"
        " alone (newer_version); a record that said ok and now says failed or not_applicable"
        " is a regression, listed and written. Records of files that are no longer active"
        " are retracted. A dry run still parses every file and writes nothing. Needs the"
        " maintain permission on the graph (maint.reindex).",
    ),
    CommandSpec(
        path=("files", "extractors"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="List the extractors of this program: version, types, record class, module.",
        params=(DATA_ROOT,),
        examples=(
            Example("graph-admin files extractors", "Every extractor, as text."),
            Example("graph-admin files extractors --json", "Every extractor, as JSON."),
        ),
        errors=ROOT_ERRORS,
        handler=list_extractors,
        output=EXTRACTORS_OUTPUT,
        description="A graph enables an extractor by importing its module at the version shown"
        " (files extract reports module_outdated for a lower one). No graph is read.",
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
