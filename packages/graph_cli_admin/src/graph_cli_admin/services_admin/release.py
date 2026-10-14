"""``graph-admin ontology release``: publish a release into a graph and pin it (B7, W.e).

Two maintenance operations under one graph lock, each in the W.e shape:
intent in the graph stream, the effect's rename (the commit point), then an
installation event anchoring the intent:

1. ``maint.ontology_release`` installs ``ontology/<version>/`` (skipped if
   that version is installed with the same artifact hash);
2. ``maint.ontology_pin`` switches the manifest pin from the current to the
   new release.

Refused before any event: archived graph (``graph.archived``), the version
installed with other content (``admin.release_exists``), a bump smaller
than the changes (``admin.version_bump_insufficient``, also downgrades),
a higher major on a graph with data (``admin.migration_required``; until
``check_graph`` exists, item 6). A larger bump than needed is a warning.
Re-running a release that is already pinned changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.model import load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli_admin.services_admin.context import AdminContext, request
from graph_cli_admin.services_admin.ontology import (
    COMPILED_FILE,
    compile_source,
    diff,
    release_files,
)


@dataclass(frozen=True, slots=True)
class Released:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


def release(ctx: AdminContext, *, graph_ref: str, source: Path, module: str | None) -> Released:
    compiled = compile_source(source, module)
    target = compiled.pin
    entry = ctx.catalog.resolve(graph_ref)
    _check_state(entry)
    resource = f"graph:{entry.id}"
    meta = request("ontology release", {"graph": str(entry.id), "ontology": target.to_json()})
    release_decisions = ctx.authorize(
        [("maint.ontology_release", resource)], request=meta, graph=entry.id
    )
    pin_decisions = ctx.authorize([("maint.ontology_pin", resource)], request=meta, graph=entry.id)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        manifest = session.manifest()  # W.6: re-read under the lock
        entry = manifest.entry
        _check_state(entry)
        current = entry.pin
        installed = session.release_hashes()
        if installed.get(target.version, target.artifact_hash) != target.artifact_hash:
            raise GraphCliError(
                "admin.release_exists",
                f"release {target.version} is installed with other content",
                details={
                    "version": target.version,
                    "expected": installed[target.version],
                    "actual": target.artifact_hash,
                },
            )
        head = session.head()
        data: dict[str, JsonValue] = {
            "graph_id": str(entry.id),
            "from": current.to_json(),
            "to": target.to_json(),
            "released": False,
            "pinned": False,
        }
        if current == target:
            return Released(entry, data, head.seq, ())
        old_model = load_compiled(
            session.read_release_file(current.version, COMPILED_FILE),
            expected_hash=current.artifact_hash,
        )
        report, bump = diff(old_model, compiled.model)
        data["diff"] = report.to_json()
        bump_details: dict[str, JsonValue] = {
            "from": current.version,
            "to": target.version,
            "level": report.level,
        }
        if bump == "insufficient":
            raise GraphCliError(
                "admin.version_bump_insufficient",
                f"{current.version} -> {target.version} does not cover {report.level} changes",
                details=bump_details,
            )
        if Version.parse(target.version).major > Version.parse(current.version).major and head.seq:
            raise GraphCliError(
                "admin.migration_required",
                f"the graph has data; {target.version} is a new major version",
                details={**bump_details, "seq": head.seq},
            )
        warnings: tuple[WarningRecord, ...] = ()
        if bump == "excessive":
            warnings = (
                WarningRecord(
                    "admin.version_bump_excessive",
                    f"{current.version} -> {target.version} is more than {report.level} needs",
                    bump_details,
                ),
            )
        if target.version not in installed:
            release_id = ctx.ids.new_id()
            files = release_files(
                compiled, previous=old_model, release_id=release_id, released_at=ctx.clock.now()
            )
            change = events.release_change(target.version, target.artifact_hash, release_id)
            receipt = _intent(ctx, session, entry, release_decisions, meta, change)
            session.install_release(target.version, files, receipt)
            _companion(ctx, entry, release_decisions, meta, receipt)
            data["released"] = True
        change = events.pin_change(current, target)
        receipt = _intent(ctx, session, entry, pin_decisions, meta, change)
        pinned = replace(manifest, entry=replace(entry, pin=target))
        session.replace_manifest(pinned, receipt)
        _companion(ctx, entry, pin_decisions, meta, receipt)
        data["pinned"] = True
        return Released(pinned.entry, data, head.seq, warnings)


def _check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )


def _intent(
    ctx: AdminContext,
    session: MaintenanceSession,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    change: dict[str, JsonValue],
) -> AuditReceipt:
    event = events.maintenance(
        actor=ctx.actor,
        decisions=decisions,
        request=meta,
        ts=ctx.clock.now(),
        event_id=ctx.event_id(),
        env=ctx.env,
        graph=entry,
        change=change,
    )
    return session.append_audit(event)


def _companion(
    ctx: AdminContext,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    receipt: AuditReceipt,
) -> None:
    """The installation event anchoring a committed intent (B5.2, B5.11)."""
    event = events.maintenance(
        actor=ctx.actor,
        decisions=decisions,
        request=meta,
        ts=ctx.clock.now(),
        event_id=ctx.event_id(),
        env=ctx.env,
        graph=entry,
        anchors=[receipt],
    )
    ctx.audit.append_installation(event)
