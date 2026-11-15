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
  misplaced copies whose bytes the graph's location holds intact (same size and, read
  in full before the lock, the same hash; for a local graph only objects in the store
  its audit trail records it left; the others are kept and listed). Orphans appear only under
  the lock, so removing them under the lock cannot race a writer. The removal is a
  ``maint.repair`` event pair (W.e) whose change names hashes and sizes only; nothing to
  remove writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Final

from graph_cli.audit import events
from graph_cli.audit.schema import is_blob_store_change
from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, has_errors
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import graph_stream
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


def recorded_store(ctx: AdminContext, graph_id: GraphId) -> Callable[[], str | None]:
    """The object store the graph was recorded to leave for the data root: the
    ``store_id`` of its last ``change.blob_store`` switch, when that switch went to
    ``local`` (``None`` otherwise, and when the graph stream does not verify). Only these
    objects of a local graph are its own; asked only when there are any."""

    def read() -> str | None:
        stream = graph_stream(str(graph_id))
        report = verify_stream(ctx.audit.read_lines(stream), stream=stream)
        if has_errors(report.findings):
            return None
        found: str | None = None
        for event in report.events:
            result, change = event.get("result"), event.get("change")
            actions = event.get("actions")
            if not (
                isinstance(result, Mapping)
                and result.get("status") == "ok"
                and isinstance(actions, list)
                and "maint.graph_configure" in actions
                and isinstance(change, Mapping)
                and is_blob_store_change(change.get("blob_store"))
            ):
                continue
            switch = change["blob_store"]
            assert isinstance(switch, Mapping)
            store_id = switch.get("store_id")
            found = str(store_id) if switch.get("to") == "local" else None
        return found

    return read


def verify(ctx: AdminContext, *, graph_ref: str, rehash: bool) -> FilesOutcome:
    entry = ctx.resolve(graph_ref, "files verify")
    meta = RequestMeta.of("files verify", {"graph": str(entry.id), "rehash": rehash})
    ctx.authorize([("maint.audit_verify", f"graph:{entry.id}")], request=meta, graph=entry.id)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        verification = session.verify_blobs(
            rehash=rehash, recorded_store=recorded_store(ctx, entry.id)
        )
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
    # Misplaced copies are judged before the lock: confirming the named location reads
    # every counterpart in full, and stored files are immutable; under the lock only
    # presence and size are checked again, right before a removal.
    judged = ctx.maintenance.misplaced_blobs(entry.id, recorded_store=recorded_store(ctx, entry.id))
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        current = session.manifest()  # W.6: re-read under the lock
        entry = current.entry
        orphans = list(session.orphan_blobs())
        if judged is not None and judged.location == current.store.blob_location:
            # the graph moved since: its copies were judged for the other location
            judged = replace(judged, removable=(), kept=(*judged.removable, *judged.kept))
        misplaced = judged.removable if judged is not None else ()
        kept = judged.kept if judged is not None else ()
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
        session.remove_blobs([b.hash for b in orphans], stale, receipt, misplaced=judged)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        data["removed"] = True
        return FilesOutcome(entry, data, seq)
