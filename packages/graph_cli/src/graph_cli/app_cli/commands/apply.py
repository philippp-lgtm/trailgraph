"""``graph apply``: change set documents from a file or stdin (contracts B3.9).

The input is one document or JSONL with one document per line. Every
document is read and checked before anything is applied, so broken input
writes nothing (exit 2). Each document is one tx, in order; locals bound by
earlier documents stay resolvable. Processing stops at the first failure:
``data.results[]`` reports each processed document and the failure is the
envelope error, so the exit code is that of the first failure.
``--idempotency-key`` sets the key of a single document; JSONL input keeps
one ``idempotency_key`` per document. ``--summary`` leaves out the entities
of each result (``entities: null``) and keeps their count and the findings.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.app_cli.commands.common import GRAPH, context
from graph_cli.app_cli.commands.write import EXPECT_ERRORS, usage
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    IDEMPOTENCY_KEY_RE,
    ChangeSet,
    changeset_hash,
    read_documents,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.read import graph_json

if TYPE_CHECKING:
    from graph_cli.services.write import WriteService

STDIN: Final = "-"


def _read(file: str) -> bytes:
    if file == STDIN:
        return sys.stdin.buffer.read()
    try:
        return Path(file).read_bytes()
    except OSError as exc:
        raise GraphCliError(
            "usage.invalid_argument",
            f"file: cannot read {file}: {exc.strerror}",
            details={"argument": "file", "actual": file},
        ) from None
    except ValueError:  # e.g. a NUL in the path
        raise GraphCliError(
            "usage.invalid_argument",
            "file: not a valid path",
            details={"argument": "file", "actual": file},
        ) from None


def _in_document(exc: GraphCliError, index: int) -> GraphCliError:
    return GraphCliError(
        exc.code,
        f"document {index}: {exc.message}",
        details={**exc.details, "document_index": index},
        warnings=exc.warnings,
    )


def _parse(writer: WriteService, data: bytes) -> list[ChangeSet]:
    changesets = []
    for index, document in enumerate(read_documents(data, max_bytes=writer.config.max_bytes)):
        try:
            changesets.append(writer.parse(document))
        except GraphCliError as exc:
            raise _in_document(exc, index) from None
    return changesets


def _with_key(changesets: list[ChangeSet], key: str | None) -> list[ChangeSet]:
    if key is None:
        return changesets
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise usage("--idempotency-key", "expected [A-Za-z0-9._:-]{1,128}", key)
    if len(changesets) != 1:
        raise usage(
            "--idempotency-key",
            f"applies to a single document, got {len(changesets)}; "
            "set idempotency_key in each document instead",
            key,
        )
    (changeset,) = changesets
    if changeset.idempotency_key not in (None, key):
        raise usage(
            "--idempotency-key",
            f"differs from the document's idempotency_key {changeset.idempotency_key!r}",
            key,
        )
    return [replace(changeset, idempotency_key=key)]


def apply(
    state: CommandState,
    file: str,
    graph: str | None,
    dry_run: bool,
    idempotency_key: str | None,
    summary: bool,
) -> Result:
    from graph_cli.services.write import WriteRequest  # loaded by apply only

    ctx = context(state)
    changesets = _with_key(_parse(ctx.writer, _read(file)), idempotency_key)
    entry = ctx.select(graph)
    requests = [
        WriteRequest(
            actor=ctx.actor,
            graph=str(entry.id),
            changeset=changeset,
            dry_run=dry_run,
            request=RequestMeta.of(
                "apply",
                {
                    "graph": entry.name,
                    "dry_run": dry_run,
                    "document_index": index,
                    "changeset_hash": changeset_hash(changeset),
                },
            ),
        )
        for index, changeset in enumerate(changesets)
    ]
    batch = ctx.writer.apply_batch(requests)
    results: list[JsonValue] = []
    for index, result in enumerate(batch.results):
        state.warnings += result.warnings
        results.append({"index": index, "ok": True, "result": result.to_json(summary=summary)})
    error = None
    if batch.error is not None:
        assert batch.failed_index is not None
        error = _in_document(batch.error, batch.failed_index)
        results.append(
            {
                "index": batch.failed_index,
                "ok": False,
                "error": {"code": error.code, "message": batch.error.message},
            }
        )
    seq = batch.results[-1].seq if batch.results else None
    data: dict[str, JsonValue] = {
        "dry_run": dry_run,
        "documents": len(changesets),
        "applied": len(batch.results),
        "results": results,
    }
    return Result(data=data, graph=graph_json(entry, seq), error=error)


APPLY_ERRORS: Final = (
    *EXPECT_ERRORS,
    "usage.invalid_input",
    "changeset.malformed",
    "changeset.local_ref_unresolved",
    "changeset.local_ref_duplicate",
    "format.unsupported_version",
    "conflict.seq_mismatch",
    "access.batch_too_large",
)

APPLY: Final = CommandSpec(
    path=("apply",),
    kind="write",
    surface="act",
    action=None,  # each op is authorized by its own action (B6.3)
    summary="Apply change set documents (JSON or JSONL) from a file or stdin.",
    description=(
        "Each document is one transaction, in order; @local names bound by earlier "
        "documents stay usable. Everything is parsed first, so broken input writes "
        "nothing. Processing stops at the first failing document."
    ),
    params=(
        ParamSpec(
            "file",
            "file",
            f"Path of the input, or {STDIN} for stdin.",
            required=True,
            positional=True,
        ),
        GRAPH,
        ParamSpec("--dry-run", "flag", "Validate every document without writing."),
        ParamSpec(
            "--summary",
            "flag",
            "Per document only counts, seq and findings: entities is null, entity_count stays.",
        ),
        ParamSpec(
            "--idempotency-key",
            "string",
            "Key that makes a retry safe; single document only, else idempotency_key "
            "in each document.",
        ),
    ),
    examples=(
        Example(f"graph apply {STDIN} --json", "Apply documents from stdin."),
        Example("graph apply <file> --dry-run --json", "Check a file without writing."),
        Example(
            "graph apply <file> --summary --json", "Apply a large file, findings without the list."
        ),
    ),
    errors=APPLY_ERRORS,
    handler=apply,
    remote="hooked",
)

SPECS: Final = (APPLY,)
