"""``graph-admin files verify`` and ``files gc``: the stored files of one graph (files slice 1,
object storage O7, O8).

- ``verify`` checks the stored files under the graph lock, in an object store by one
  listing of the graph's prefix (``--rehash`` reads every file): missing and damaged
  files (errors), orphans, staging leftovers and misplaced copies (info: copies outside
  the location the manifest names). An object store that cannot be reached or carries
  another store marker is the error finding ``blob_store_unavailable``. It writes
  nothing; error findings fail with ``admin.integrity_errors``.
- ``gc`` removes stored files no tx names (in an object store only those older than 24
  hours by the store's clock), staging files older than one hour (an add stages before
  it takes the lock, so a young staging file may belong to a write in flight) and
  misplaced copies whose hash the graph's location holds with the same size (the
  others are kept and listed). Orphans appear only under the lock, so removing them
  under the lock cannot race a writer. The removal is a ``maint.repair`` event pair
  (W.e) whose change names hashes and sizes only; nothing to remove writes nothing.
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


def verify(ctx: AdminContext, *, graph_ref: str, rehash: bool) -> FilesOutcome:
    entry = ctx.resolve(graph_ref, "files verify")
    meta = RequestMeta.of("files verify", {"graph": str(entry.id), "rehash": rehash})
    ctx.authorize([("maint.audit_verify", f"graph:{entry.id}")], request=meta, graph=entry.id)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        verification = session.verify_blobs(rehash=rehash)
        findings: list[Finding] = list(verification.findings)
        stored = verification.stored or ()  # None only beside a blob_store_unavailable error
        seq = ctx.maintenance.store(entry.id).head().seq
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "rehash": rehash,
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
        misplaced, kept = session.misplaced_blobs()
        now = ctx.clock.now()
        stale = [name for name, modified in session.staging() if now - modified > STAGING_MAX_AGE]
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "blobs_removed": events.blob_list(orphans) if orphans else [],
            "staging_removed": list(stale),
            "misplaced_removed": events.blob_list(misplaced) if misplaced else [],
            "misplaced_kept": events.blob_list(kept) if kept else [],
            "dry_run": dry_run,
            "removed": False,
        }
        seq = session.head().seq
        if dry_run or not (orphans or stale or misplaced):
            return FilesOutcome(entry, data, seq)
        change = events.blobs_removed(orphans, len(stale), misplaced=misplaced)
        receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
        session.remove_blobs([b.hash for b in orphans], stale, receipt, misplaced=misplaced)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        data["removed"] = True
        return FilesOutcome(entry, data, seq)
