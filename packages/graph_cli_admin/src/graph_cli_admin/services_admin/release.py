"""``graph-admin ontology release``: publish a release into a graph and pin it (B7, W.e).

Two maintenance operations under one graph lock, each in the W.e shape:
intent in the graph stream, the effect's rename (the commit point), then an
installation event anchoring the intent:

1. ``maint.ontology_release`` installs ``ontology/<version>/`` (skipped if
   that version is installed with the same artifact hash);
2. ``maint.ontology_pin`` switches the manifest pin from the current to the
   new release and seeds the alias prefixes of classes the release adds
   (existing prefixes never change).

Refused before any event: archived graph (``graph.archived``), the version
installed with other content (``admin.release_exists``), a bump smaller
than the changes (``admin.version_bump_insufficient``, also downgrades),
a higher major on a graph whose data has error findings under the new
version (``check_graph`` under the lock, B7.14: ``admin.migration_required``
with the counts and the first 20 findings). A larger bump than needed is a
warning.
Re-running a release that is already pinned changes nothing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.graphs import merged_alias_prefixes
from graph_cli_admin.services_admin.ontology import (
    COMPILED_FILE,
    PinnedModel,
    compile_source,
    diff,
    release_files,
)

MAX_FINDINGS = 20


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
    meta = RequestMeta.of(
        "ontology release", {"graph": str(entry.id), "ontology": target.to_json()}
    )
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
            _check_data(compiled.model, session, bump_details, head.seq)
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
                compiled,
                previous=PinnedModel(old_model, current.artifact_hash),
                release_id=release_id,
                released_at=ctx.clock.now(),
            )
            change = events.release_change(target.version, target.artifact_hash, release_id)
            receipt = ctx.intent(session, entry, release_decisions, meta, change)
            session.install_release(target.version, files, receipt)
            ctx.anchor(entry, release_decisions, meta, receipt)
            data["released"] = True
        change = events.pin_change(current, target)
        receipt = ctx.intent(session, entry, pin_decisions, meta, change)
        prefixes = merged_alias_prefixes(entry.alias_prefixes, compiled)
        data["alias_prefixes_added"] = {
            str(curie): prefix
            for curie, prefix in prefixes.items()
            if curie not in entry.alias_prefixes
        }
        pinned = replace(manifest, entry=replace(entry, pin=target, alias_prefixes=prefixes))
        session.replace_manifest(pinned, receipt)
        ctx.anchor(entry, pin_decisions, meta, receipt)
        data["pinned"] = True
        return Released(pinned.entry, data, head.seq, warnings)


def _check_data(
    model: CompiledModel, session: MaintenanceSession, bump: dict[str, JsonValue], seq: int
) -> None:
    """B7.14: data under a new major must check clean, else a migration is needed."""
    errors = [f for f in check_graph(model, session.snapshot()) if f.severity is Severity.ERROR]
    if errors:
        counts = Counter(f.code for f in errors)
        raise GraphCliError(
            "admin.migration_required",
            f"{len(errors)} error findings under {bump['to']}; the data needs a migration",
            details={
                **bump,
                "seq": seq,
                "counts": dict(sorted(counts.items())),
                "findings": [f.to_json() for f in errors[:MAX_FINDINGS]],
            },
        )


def _check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )
