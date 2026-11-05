"""``graph-admin keys verify|gc|backup|rotate-kek``: the key stores of sealed graphs (T16).

- ``verify`` compares a graph's key store with its records, its stored files and
  the keys its purge intents name, under the graph lock: missing keys and
  shreds no purge names (errors), purged files still stored and live keys
  under a KEK other than the current one (warnings), orphan keys (info). It
  writes nothing; error findings fail with ``admin.integrity_errors``.
- ``gc`` deletes orphan keys older than an hour (a file is staged, and its key
  created, before the lock is taken: a young orphan may belong to a write in
  flight). Keys of stored files stay until ``files gc`` removes the files. The
  removal is a ``maint.repair`` event pair whose change lists the key ids;
  nothing to remove writes nothing.
- ``backup`` writes a consistent copy of each key store (``<dir>/<graph
  id>.sqlite``, mode 0600) under the graph lock, so it holds the key of every
  tx committed before it. Local only: the server takes no paths.
- ``rotate-kek`` rewraps every live key of each sealed graph under the current
  KEK (``[keys] kek_file``) and retires the others; the earlier KEK must be
  configured as ``[keys] previous_kek_file`` while it runs. One
  ``maint.graph_configure`` event pair per graph names the KEK ids; a graph
  with nothing to rewrap writes nothing, so running it again completes an
  interrupted rotation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from graph_cli.audit import events
from graph_cli.audit.verify import purges, verify_stream
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import has_errors
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import RequestMeta
from graph_cli_admin.services_admin.audit import counts_of
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.files import STAGING_MAX_AGE

BACKUP_DIR_MODE = 0o700


@dataclass(frozen=True, slots=True)
class KeysOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int


def verify(ctx: AdminContext, *, graph_ref: str) -> KeysOutcome:
    entry = _sealed(ctx.resolve(graph_ref, "keys verify"))
    meta = RequestMeta.of("keys verify", {"graph": str(entry.id)})
    ctx.authorize([("maint.audit_verify", f"graph:{entry.id}")], request=meta, graph=entry.id)
    stream = graph_stream(str(entry.id))
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        report = verify_stream(ctx.audit.read_lines(stream), stream=stream)
        findings = list(session.verify_keys(purges(report.events), orphans=True))
        wrapping = session.key_wrapping()
        seq = session.head().seq
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "kek_id": wrapping.current,
        "live": dict(sorted(wrapping.live.items())),
        "counts": counts_of(findings),
        "findings": [f.to_json() for f in findings],
    }
    if has_errors(findings):
        raise GraphCliError(
            "admin.integrity_errors",
            f"the key store of {entry.name} has errors: {data['counts']}",
            details={"counts": data["counts"], "findings": data["findings"]},
        )
    return KeysOutcome(entry, data, seq)


def gc(ctx: AdminContext, *, graph_ref: str, reason: str | None, dry_run: bool) -> KeysOutcome:
    entry = _sealed(ctx.resolve(graph_ref, "keys gc"))
    meta = RequestMeta.of("keys gc", {"graph": str(entry.id), "dry_run": dry_run})
    decisions = ctx.authorize(
        [("maint.repair", f"graph:{entry.id}")], request=meta, graph=entry.id, reason=reason
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        entry = session.manifest().entry  # W.6: re-read under the lock
        cutoff = ctx.clock.now() - STAGING_MAX_AGE
        orphans = session.orphan_keys()
        old = [kid for kid, created in orphans.items() if created < cutoff]
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "keys_removed": [str(kid) for kid in old],
            "keys_kept": len(orphans) - len(old),
            "dry_run": dry_run,
            "removed": False,
        }
        seq = session.head().seq
        if dry_run or not old:
            return KeysOutcome(entry, data, seq)
        receipt = ctx.intent(
            session, entry, decisions, meta, events.keys_removed(old), reason=reason
        )
        session.remove_keys(old, created_before=cutoff, receipt=receipt)
        ctx.anchor(entry, decisions, meta, receipt, reason=reason)
        data["removed"] = True
        return KeysOutcome(entry, data, seq)


def backup(ctx: AdminContext, *, graph_ref: str | None, to: Path) -> dict[str, JsonValue]:
    """Every destination file is checked before the first one is written."""
    entries = _targets(ctx, graph_ref, "keys backup")
    files = {entry.id: to / f"{entry.id}.sqlite" for entry in entries}
    taken = [str(path) for path in files.values() if os.path.lexists(path)]
    if taken:
        raise GraphCliError(
            "usage.invalid_argument",
            f"--to: {taken[0]} already exists; back up into a new directory",
            details={"argument": "--to", "files": list(taken)},
        )
    if entries:
        to.mkdir(mode=BACKUP_DIR_MODE, parents=True, exist_ok=True)
    written: list[JsonValue] = []
    for entry in entries:
        with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
            session.backup_keys(files[entry.id])
        written.append(
            {"graph_id": str(entry.id), "name": entry.name, "file": str(files[entry.id])}
        )
    return {"dir": str(to), "graphs": written}


def rotate_kek(
    ctx: AdminContext, *, graph_ref: str | None, reason: str | None, dry_run: bool
) -> dict[str, JsonValue]:
    """Graph by graph, each under its own lock: an error stops at that graph, and running
    the command again completes the rest."""
    graphs: list[JsonValue] = []
    current: str | None = None
    for entry in _targets(ctx, graph_ref, "keys rotate-kek"):
        meta = RequestMeta.of("keys rotate-kek", {"graph": str(entry.id), "dry_run": dry_run})
        decisions = ctx.authorize(
            [("maint.graph_configure", f"graph:{entry.id}")],
            request=meta,
            graph=entry.id,
            reason=reason,
        )
        with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
            entry = session.manifest().entry  # W.6: re-read under the lock
            wrapping = session.key_wrapping()
            current = wrapping.current
            unknown = sorted(set(wrapping.stale) - wrapping.configured)
            if unknown:
                raise GraphCliError(
                    "config.invalid",
                    f"keys of {entry.name} are wrapped under the key-encryption key"
                    f" {unknown[0]}, which is not configured; set [keys] previous_kek_file to"
                    " the key-encryption key file from before the rotation",
                    details={"graph": entry.name, "kek_ids": list(unknown)},
                )
            item: dict[str, JsonValue] = {
                "graph_id": str(entry.id),
                "name": entry.name,
                "rewrapped": sum(wrapping.stale.values()),
                "retired": list(wrapping.retiring),
                "rotated": False,
            }
            if not dry_run and wrapping.retiring:
                change = events.kek_rotated(wrapping.retiring, wrapping.current)
                receipt = ctx.intent(session, entry, decisions, meta, change, reason=reason)
                done = session.rotate_keys(receipt)
                ctx.anchor(entry, decisions, meta, receipt, reason=reason)
                item.update(rewrapped=done.rewrapped, rotated=True)
        graphs.append(item)
    return {"kek_id": current, "graphs": graphs, "dry_run": dry_run}


def _sealed(entry: GraphEntry) -> GraphEntry:
    if not entry.sealed:
        raise GraphCliError(
            "admin.graph_not_sealed",
            f"{entry.name} is not sealed: its values are stored in the clear and it has no"
            " key store",
            details={"graph": entry.name},
        )
    return entry


def _targets(ctx: AdminContext, graph_ref: str | None, command: str) -> list[GraphEntry]:
    """The sealed graph ``graph_ref`` names, else every sealed graph, archived ones too."""
    if graph_ref is not None:
        return [_sealed(ctx.resolve(graph_ref, command))]
    found = ctx.manifests(include_archived=True)
    return sorted((m.entry for m in found if m.entry.sealed), key=lambda e: e.name)
