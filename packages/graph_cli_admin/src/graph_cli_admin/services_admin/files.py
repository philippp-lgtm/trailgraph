"""``graph-admin files verify`` and ``files gc``: the stored files of one graph (files slice 1).

- ``verify`` reads every stored file under the graph lock: missing and
  damaged files (errors), orphans and staging leftovers (info). With
  ``decrypt`` (sealed graphs only) it also decrypts every file a revision
  names and compares the content with the revision's content hash. It writes
  nothing; error findings fail with ``admin.integrity_errors``.
- ``gc`` removes stored files no tx names and staging files older than one
  hour (an add stages before it takes the lock, so a young staging file may
  belong to a write in flight). Orphans appear only under the lock, so
  removing them under the lock cannot race a writer. The removal is a
  ``maint.repair`` event pair (W.e) whose change names hashes and sizes
  only; nothing to remove writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from graph_cli.audit import events
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, has_errors
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli_admin.services_admin.audit import counts_of
from graph_cli_admin.services_admin.context import AdminContext

STAGING_MAX_AGE: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class FilesOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


def verify(
    ctx: AdminContext, *, graph_ref: str, rehash: bool, decrypt: bool = False
) -> FilesOutcome:
    entry = ctx.resolve(graph_ref, "files verify")
    if decrypt and not entry.sealed:
        raise GraphCliError(
            "usage.invalid_argument",
            f"--decrypt: {entry.name} is not sealed; its files are stored as they are"
            " (--rehash checks them)",
            details={"argument": "--decrypt"},
        )
    args: dict[str, JsonValue] = {"graph": str(entry.id), "rehash": rehash}
    if decrypt:
        args["decrypt"] = True
    meta = RequestMeta.of("files verify", args)
    ctx.authorize([("maint.audit_verify", f"graph:{entry.id}")], request=meta, graph=entry.id)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        findings: list[Finding] = list(session.verify_blobs(rehash=rehash, decrypt=decrypt))
        stored = list(session.blobs())
        seq = ctx.maintenance.store(entry.id).head().seq
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "rehash": rehash,
        "decrypt": decrypt,
        "stored": {"files": len(stored), "bytes": sum(b.size for b in stored)},
        "counts": counts_of(findings),
        "findings": [f.to_json() for f in findings],
    }
    if has_errors(findings):
        raise GraphCliError(
            "admin.integrity_errors",
            f"the stored files of {entry.name} have errors: {data['counts']}",
            details={"counts": data["counts"], "findings": data["findings"]},
        )
    return FilesOutcome(entry, data, seq)


def gc(ctx: AdminContext, *, graph_ref: str, reason: str | None, dry_run: bool) -> FilesOutcome:
    entry = ctx.resolve(graph_ref, "files gc")
    meta = RequestMeta.of("files gc", {"graph": str(entry.id), "dry_run": dry_run})
    decisions = ctx.authorize(
        [("maint.repair", f"graph:{entry.id}")], request=meta, graph=entry.id, reason=reason
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        orphans = list(session.orphan_blobs())
        now = ctx.clock.now()
        stale = [name for name, modified in session.staging() if now - modified > STAGING_MAX_AGE]
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "blobs_removed": events.blob_list(orphans) if orphans else [],
            "staging_removed": list(stale),
            "dry_run": dry_run,
            "removed": False,
        }
        seq = session.head().seq
        if dry_run or not (orphans or stale):
            return FilesOutcome(entry, data, seq)
        change = events.blobs_removed(orphans, len(stale))
        receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
        session.remove_blobs([b.hash for b in orphans], stale, receipt)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        data["removed"] = True
        return FilesOutcome(entry, data, seq)
