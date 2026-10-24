"""``graph-admin ontology release``: publish a release into a graph and pin it (B7, W.e).

Two maintenance operations under one graph lock, each in the W.e shape:
intent in the graph stream, the effect's rename (the commit point), then an
installation event anchoring the intent:

1. ``maint.ontology_release`` installs ``ontology/<version>/`` (skipped if
   that version is installed with the same artifact hash);
2. ``maint.ontology_pin`` switches the manifest pin from the current to the
   new release and seeds the alias prefixes of classes the release adds
   (existing prefixes never change; a renamed class keeps the prefix of its
   old name; a seed another class already uses is left out with the warning
   ``admin.alias_prefix_taken``).

Refused before any event: archived graph (``graph.archived``), the version
installed with other content (``admin.release_exists``), a bump smaller
than the changes (``admin.version_bump_insufficient``, also downgrades),
a higher major on a graph whose data has error findings under the new
version (``check_graph`` under the lock, B7.14: ``admin.migration_required``
with the counts and the first 20 findings). A larger bump than needed is a
warning.
Re-running a release that is already pinned changes nothing.

``plan`` makes every check under the lock and ``write`` writes, so
``graph push`` (``sources``) runs a release and a skill publish under one
lock, checking both before the first write.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData
from graph_cli.kernel.ports.storage import MaintenanceSession
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.graphs import merged_alias_prefixes
from graph_cli_admin.services_admin.ontology import (
    COMPILED_FILE,
    Compiled,
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


@dataclass(frozen=True, slots=True)
class Planned:
    """A release checked under the graph lock: everything ``write`` writes. ``old`` is
    ``None`` when the graph is pinned to the release already (nothing to write)."""

    manifest: ManifestData
    compiled: Compiled
    old: PinnedModel | None
    install: bool
    prefixes: dict[TermRef, str]
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]

    @property
    def unchanged(self) -> bool:
        return self.old is None


@dataclass(frozen=True, slots=True)
class Decisions:
    release: tuple[Decision, ...]
    pin: tuple[Decision, ...]


def authorize(
    ctx: AdminContext, entry: GraphEntry, meta: RequestMeta, *, reason: str | None = None
) -> Decisions:
    resource = f"graph:{entry.id}"
    pairs = [("maint.ontology_release", resource)]
    release = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    pairs = [("maint.ontology_pin", resource)]
    pin = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    ctx.read_data(entry, meta)  # a release checks the data; its findings quote values
    return Decisions(release, pin)


def release(ctx: AdminContext, *, graph_ref: str, source: Path, module: str | None) -> Released:
    compiled = compile_source(source, module)
    entry = ctx.resolve(graph_ref, "ontology release")
    check_state(entry)
    meta = RequestMeta.of(
        "ontology release", {"graph": str(entry.id), "ontology": compiled.pin.to_json()}
    )
    decisions = authorize(ctx, entry, meta)
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        planned = plan(ctx, session, compiled)
        if planned.unchanged:
            return Released(planned.manifest.entry, planned.data, planned.seq, ())
        pinned = write(ctx, session, planned, decisions, meta)
        return Released(pinned, planned.data, planned.seq, planned.warnings)


def plan(ctx: AdminContext, session: MaintenanceSession, compiled: Compiled) -> Planned:
    """Every check of a release, under the lock; refuses before anything is written."""
    target = compiled.pin
    manifest = session.manifest()  # W.6: re-read under the lock
    entry = manifest.entry
    check_state(entry)
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
        return Planned(manifest, compiled, None, False, {}, data, head.seq, ())
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
    prefixes, skipped = merged_alias_prefixes(entry.alias_prefixes, compiled)
    data["alias_prefixes_added"] = {
        str(curie): prefix
        for curie, prefix in prefixes.items()
        if curie not in entry.alias_prefixes
    }
    owners = {prefix: curie for curie, prefix in entry.alias_prefixes.items()}
    warnings += tuple(
        WarningRecord(
            "admin.alias_prefix_taken",
            f"{curie} gets no aliases: prefix {prefix} is used by {owners[prefix]}",
            {"type": str(curie), "prefix": prefix, "used_by": str(owners[prefix])},
        )
        for curie, prefix in skipped.items()
    )
    old = PinnedModel(old_model, current.artifact_hash)
    install = target.version not in installed
    return Planned(manifest, compiled, old, install, prefixes, data, head.seq, warnings)


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    planned: Planned,
    decisions: Decisions,
    meta: RequestMeta,
    *,
    reason: str | None = None,
) -> GraphEntry:
    """Install (unless installed) and pin a planned release; answers the pinned entry."""
    assert planned.old is not None, "nothing to commit"
    entry = planned.manifest.entry
    target = planned.compiled.pin
    if planned.install:
        release_id = ctx.ids.new_id()
        files = release_files(
            planned.compiled,
            previous=planned.old,
            release_id=release_id,
            released_at=ctx.clock.now(),
        )
        change = events.release_change(target.version, target.artifact_hash, release_id)
        receipt = ctx.intent(session, entry, decisions.release, meta, change, reason=reason)
        session.install_release(target.version, files, receipt)
        ctx.anchor(entry, decisions.release, meta, receipt, reason=reason)
        planned.data["released"] = True
    change = events.pin_change(entry.pin, target)
    receipt = ctx.intent(session, entry, decisions.pin, meta, change, reason=reason)
    pinned = replace(
        planned.manifest, entry=replace(entry, pin=target, alias_prefixes=planned.prefixes)
    )
    session.replace_manifest(pinned, receipt)
    ctx.anchor(entry, decisions.pin, meta, receipt, reason=reason)
    planned.data["pinned"] = True
    return pinned.entry


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


def check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )
