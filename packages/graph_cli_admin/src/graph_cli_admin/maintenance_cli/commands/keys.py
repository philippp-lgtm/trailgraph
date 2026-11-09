"""``graph-admin keys verify|gc|backup|rotate-kek``: the key stores of sealed graphs (T16)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, array, nullable, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    GRAPH_OPTION,
    admin_context,
)
from graph_cli_admin.maintenance_cli.output_schemas import STRINGS
from graph_cli_admin.services_admin import graphs, keys

ANY_GRAPH: Final = ParamSpec(
    "--graph",
    "graph_ref",
    "Graph name or id; without it, every sealed graph.",
    keyword="graph_ref",
)


def verify(state: CommandState, graph_ref: str, data_root: str | None) -> Result:
    outcome = keys.verify(admin_context(state, data_root), graph_ref=graph_ref)
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


def gc(
    state: CommandState, graph_ref: str, reason: str | None, dry_run: bool, data_root: str | None
) -> Result:
    outcome = keys.gc(
        admin_context(state, data_root), graph_ref=graph_ref, reason=reason, dry_run=dry_run
    )
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


def backup(state: CommandState, graph_ref: str | None, to: str, data_root: str | None) -> Result:
    return Result(
        data=keys.backup(admin_context(state, data_root), graph_ref=graph_ref, to=Path(to))
    )


def rotate_kek(
    state: CommandState,
    graph_ref: str | None,
    reason: str | None,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    return Result(
        data=keys.rotate_kek(
            admin_context(state, data_root), graph_ref=graph_ref, reason=reason, dry_run=dry_run
        )
    )


VERIFY_OUTPUT: Final = obj(  # services_admin.keys.verify
    {
        "graph_id": STRING,
        "kek_id": STRING,
        "live": obj(extra=INTEGER),
        "counts": ref("finding_counts"),
        "findings": array(ref("finding")),
    }
)
GC_OUTPUT: Final = obj(  # services_admin.keys.gc
    {
        "graph_id": STRING,
        "keys_removed": STRINGS,
        "keys_kept": INTEGER,
        "dry_run": BOOLEAN,
        "removed": BOOLEAN,
    }
)
BACKUP_OUTPUT: Final = obj(  # services_admin.keys.backup
    {
        "dir": STRING,
        "graphs": array(obj({"graph_id": STRING, "name": STRING, "file": STRING})),
    }
)
ROTATE_OUTPUT: Final = obj(  # services_admin.keys.rotate_kek
    {
        "kek_id": nullable(STRING),
        "graphs": array(
            obj(
                {
                    "graph_id": STRING,
                    "name": STRING,
                    "rewrapped": INTEGER,
                    "retired": STRINGS,
                    "rotated": BOOLEAN,
                }
            )
        ),
        "dry_run": BOOLEAN,
    }
)
NOT_SEALED: Final = ("admin.graph_not_sealed",)


SPECS: Final = (
    CommandSpec(
        path=("keys", "verify"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="Check the key store of a sealed graph against its records, files and purges.",
        params=(GRAPH_OPTION, DATA_ROOT),
        examples=(
            Example("graph-admin keys verify --graph <graph>", "Check the key store."),
            Example("graph-admin keys verify --graph <graph> --json", "The findings as data."),
        ),
        errors=(*GRAPH_ERRORS, *NOT_SEALED, "access.denied", "admin.integrity_errors"),
        handler=verify,
        output=VERIFY_OUTPUT,
        description="Exits 1 (admin.integrity_errors) on a key a record or stored file names"
        " but the key store lacks, and on a key shredded without a purge naming it. Purged"
        " files still stored and live keys under an earlier key-encryption key are warnings,"
        " orphan keys info (keys gc removes them). live counts the live keys per"
        " key-encryption key id.",
    ),
    CommandSpec(
        path=("keys", "gc"),
        kind="write",
        surface="act",
        action="maint.repair",
        summary="Delete the orphan keys of a sealed graph that are older than one hour.",
        params=(
            GRAPH_OPTION,
            ParamSpec("--reason", "string", "Why the keys are removed; recorded in the audit."),
            ParamSpec("--dry-run", "flag", "Show what would be removed without removing it."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin keys gc --graph <graph> --dry-run", "Show what would go."),
            Example(
                "graph-admin keys gc --graph <graph> --reason <reason> --json",
                "Remove them, with a reason.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS, *NOT_SEALED, "backend.corrupt"),
        handler=gc,
        output=GC_OUTPUT,
        description="An orphan key is live and named by no record and no stored file: the"
        " key of a write that was refused, or of a file that was staged but not kept. Younger"
        " ones may belong to a write in flight and stay (keys_kept). The keys of stored files"
        " stay until files gc removes the files. The removal is audited (maint.repair);"
        " nothing to remove writes nothing.",
    ),
    CommandSpec(
        path=("keys", "backup"),
        kind="read",
        surface="act",
        action=None,
        summary="Write a consistent copy of the key store of each sealed graph to a directory.",
        params=(
            ANY_GRAPH,
            ParamSpec(
                "--to",
                "directory",
                "Directory for the copies (<graph id>.sqlite each); created if missing.",
                required=True,
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin keys backup --to <dir>", "Back up every key store."),
            Example(
                "graph-admin keys backup --graph <graph> --to <dir> --json",
                "Back up the key store of one graph.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            *NOT_SEALED,
            "backend.lock_timeout",
            "backend.unavailable",
            "usage.invalid_argument",
        ),
        handler=backup,
        output=BACKUP_OUTPUT,
        description="Each copy (mode 0600) is taken under the graph lock and holds the key of"
        " every transaction committed before it; an existing file is never replaced. The"
        " copies are wrapped by the key-encryption key, which is not copied. Keep them apart"
        " from data backups and for a short time: a purge is complete only once no copy from"
        " before it is left. Runs where the data lives, not over the admin API.",
        remote="local",
    ),
    CommandSpec(
        path=("keys", "rotate-kek"),
        kind="write",
        surface="act",
        action="maint.graph_configure",
        summary="Rewrap the keys of each sealed graph under the current key-encryption key.",
        params=(
            ANY_GRAPH,
            ParamSpec(
                "--reason", "string", "Why the key is rotated; recorded in the audit stream."
            ),
            ParamSpec("--dry-run", "flag", "Show what would be rewrapped without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin keys rotate-kek --dry-run", "Show what a rotation would do."),
            Example(
                "graph-admin keys rotate-kek --reason <reason> --json",
                "Rewrap every key store under the new key-encryption key.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS, *NOT_SEALED, "backend.unavailable"),
        handler=rotate_kek,
        output=ROTATE_OUTPUT,
        description="The new key-encryption key is [keys] kek_file, the earlier one [keys]"
        " previous_kek_file; every program that opens the graphs needs both while the"
        " rotation runs. Each graph is rewrapped in one transaction and its earlier"
        " key-encryption keys are marked retired (audited as maint.graph_configure); a graph"
        " with nothing left to rewrap writes nothing, so running it again completes an"
        " interrupted rotation. Afterwards keys verify shows no integrity.keys.kek_not_current,"
        " and the earlier key-encryption key file and its copies can be destroyed. Runs where"
        " the data lives, not over the admin API.",
        remote="local",
    ),
)
