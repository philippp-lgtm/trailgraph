"""``graph apply``: change set documents from a file or stdin (contracts B3.9).

The input is one document or JSONL with one document per line. Every
document is read and checked before anything is applied, so broken input
writes nothing (exit 2). Each document is one tx, in order; locals bound by
earlier documents stay resolvable. Processing stops at the first failure:
``data.results[]`` reports each processed document and the failure is the
envelope error, so the exit code is that of the first failure.
A document may carry ``source`` and ``reconcile`` (the desired state of a source, planned
by the write service under the graph lock); the answer of such a document has ``reconcile``.
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
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, array, obj, one_of, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    IDEMPOTENCY_KEY_RE,
    ChangeSet,
    changeset_hash,
    document_hash,
    read_documents,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.read import graph_json

if TYPE_CHECKING:
    from graph_cli.services.write import WriteService

STDIN: Final = "-"


def read_input(file: str) -> bytes:
    """The input of ``apply``: a file or, for ``-``, stdin (also the remote mode's)."""
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


def _content(changeset: ChangeSet) -> dict[str, JsonValue]:
    """What the request digest (C.10) covers of a document: the hash of its change set, which
    for a document that names a source or reconciles scopes is the hash of the whole document
    (the tx records the ops a reconcile expanded to, so its hash does not say what was asked).
    A document without either keeps the digest it always had."""
    if changeset.source is None and not changeset.reconcile:
        return {"changeset_hash": changeset_hash(changeset)}
    return {"document_hash": document_hash(changeset)}


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
    changesets = _with_key(_parse(ctx.writer, read_input(file)), idempotency_key)
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
                    **_content(changeset),
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
    # a reconcile document: a type or relation it names that the caller does not see, and the
    # guards of the plan (audited refusals)
    "term.unknown",
    "reconcile.source_empty",
    "reconcile.retire_limit",
    "reconcile.shape",
    "reconcile.key_attribute",
    "reconcile.scope_overlap",
    "reconcile.scope_conflict",
    "reconcile.move_invalid",
    "reconcile.target_not_found",
    "reconcile.type_mismatch",
)

APPLY_OUTPUT: Final = obj(  # B3.9: one result per committed document, then the failed one
    {
        "dry_run": BOOLEAN,
        "documents": INTEGER,
        "applied": INTEGER,
        "results": array(
            one_of(
                obj({"index": INTEGER, "ok": {"const": True}, "result": ref("write_result")}),
                obj(
                    {
                        "index": INTEGER,
                        "ok": {"const": False},
                        "error": obj({"code": STRING, "message": STRING}),
                    }
                ),
            )
        ),
    }
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
        "nothing. Processing stops at the first failing document. A document: "
        '{"reason": "<why>", "ops": [{"op": "create", "local": "a", "type": "<type>", '
        '"attrs": {"<attr>": "<value>"}, "edges": [{"rel": "<relation>", "target": "<id>"}]}, '
        '{"op": "link", "id": "@a", "rel": "<relation>", "target": "<id>"}]}; '
        "more ops: update (id, set), unlink, transition (id, to), supersede, retract. "
        "A pipeline can also hand over the desired state of a source as a reconcile "
        "document (source, reconcile), applied by key in one transaction; agents do not "
        "write those. Every key: references/changesets.md of the graph-cli skill."
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
    output=APPLY_OUTPUT,
    remote="hooked",
)

SPECS: Final = (APPLY,)
