"""``graph-admin graph check``: the data of a graph against an ontology (batch, R.7).

- ``check_graph`` on the head snapshot: the checks of the write path plus
  the ones only a batch check runs, rules with ``when: batch`` and ``in``
  cardinality. A read: no graph lock, no audit event, nothing written.
- Under the pinned release by default (checked against the pin, B7.6).
  With an authored ontology the same data is checked under that ontology
  instead, without releasing it (what a major release checks, warnings
  included).
- Authorized as ``maint.audit_verify`` on the graph, like ``files verify``.
- The published function catalog is checked against the same model
  (``graph_cli.kinetic.check_catalog``): a function that no longer resolves
  (``function.stale`` for the stored query text) fails with
  ``admin.functions_broken``; ``functions`` in the data lists the problems. The
  published action catalog is checked the same way (``check_actions``, against the
  function names of the graph): ``admin.actions_broken``, ``actions`` in the data.
  When both are broken, the functions are refused first (fix, check again).
- Error findings fail with ``admin.data_errors`` (counts and findings in the
  details); warnings and info findings alone succeed.
- An authored ontology of another version with a ``migration.yaml`` is previewed as the
  release would run it (plan M9): the migration is applied to an overlay, the findings are
  those of the migrated data, and the ``migration`` block of the data counts what each derived
  rename and step changes, with sample entity ids and the steps that matched nothing. Nothing
  is written. Without the file the data is checked as it is, so what a rename without a
  migration leaves shows as findings. A migration that is committed but not pinned (an
  interrupted release) is the warning ``graph.migration_incomplete``; the data is checked
  under its target version.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, has_errors
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead, Snapshot
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli_admin.services_admin import migration as migrations
from graph_cli_admin.services_admin.audit import counts_of
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import (
    COMPILED_FILE,
    compile_source,
    pinned_model,
    read_migration,
)
from graph_cli_admin.services_admin.release import (
    actions_broken,
    catalog_problems,
    functions_broken,
    names_of,
)


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    entry: GraphEntry
    data: dict[str, JsonValue]
    seq: int
    warnings: tuple[WarningRecord, ...]


def check(
    ctx: AdminContext, *, graph_ref: str, source: Path | None, module: str | None
) -> CheckOutcome:
    entry = ctx.resolve(graph_ref, "graph check")
    meta = RequestMeta.of("graph check", {"graph": str(entry.id)})
    ctx.authorize([("maint.audit_verify", f"graph:{entry.id}")], request=meta, graph=entry.id)
    ctx.read_data(entry, meta)
    read, _ = ctx.catalog.store_for(entry.id)
    snapshot = read.snapshot()
    marker = read.migration_marker()
    interrupted = marker if marker is not None and marker_committed(snapshot, marker) else None
    warnings = snapshot.warnings
    block: dict[str, JsonValue] | None = None
    if interrupted is not None and not read.writer_active():
        warnings += (
            WarningRecord(
                "graph.migration_incomplete",
                f"graph {entry.name} has a migration to {interrupted.to_version} that is committed"
                " but not pinned (an interrupted release); finish it by running the same release"
                " again",
                {
                    "graph": entry.name,
                    "from": interrupted.from_version,
                    "to": interrupted.to_version,
                },
            ),
        )
    if source is None:
        model = _migrated_model(ctx, entry, interrupted) or pinned_model(ctx.catalog, entry)
        findings = check_graph(model, snapshot)
    else:
        compiled = compile_source(source, module)
        model = compiled.model
        findings, block, extra = _authored(
            ctx,
            entry,
            compiled.pin.version,
            model,
            read_migration(source),
            read,
            snapshot,
            interrupted,
        )
        warnings += extra
    codes = Counter(f.code for f in findings)
    slot = ctx.catalog.read_functions(entry.id)
    problems = catalog_problems(slot, model)
    published = ctx.catalog.read_actions(entry.id)
    action_problems = catalog_problems(published, model, function_names=names_of(slot))
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "ontology": {"bundle": model.bundle_id, "version": model.bundle_version},
        "pinned": source is None,
        "counts": counts_of(findings),
        "codes": dict(sorted(codes.items())),
        "findings": [f.to_json() for f in findings],
        "functions": {
            "count": slot.count if slot is not None else 0,
            "problems": [p.to_json() for p in problems],
        },
        "actions": {
            "count": published.count if published is not None else 0,
            "problems": [p.to_json() for p in action_problems],
        },
    }
    if block is not None:
        data["migration"] = block
    if has_errors(findings):
        raise GraphCliError(
            "admin.data_errors",
            f"the data of {entry.name} has errors under {model.bundle_version}: {data['counts']}",
            details={k: data[k] for k in ("ontology", "counts", "codes", "findings")},
        )
    if problems:
        raise functions_broken(
            problems, {"to": model.bundle_version, "pinned": source is None}, advice=""
        )
    if action_problems:
        raise actions_broken(
            action_problems, {"to": model.bundle_version, "pinned": source is None}, advice=""
        )
    return CheckOutcome(entry, data, snapshot.seq, warnings)


def _migrated_model(
    ctx: AdminContext, entry: GraphEntry, marker: MigrationMarker | None
) -> CompiledModel | None:
    """The model of an interrupted migration's target version: its data has those names."""
    if marker is None or (marker.to_version, marker.artifact_hash) == (
        entry.pin.version,
        entry.pin.artifact_hash,
    ):
        return None
    data = ctx.catalog.read_release_file(entry.id, marker.to_version, COMPILED_FILE)
    return load_compiled(data, expected_hash=marker.artifact_hash)


def _authored(
    ctx: AdminContext,
    entry: GraphEntry,
    version: str,
    new: CompiledModel,
    raw: bytes | None,
    read: GraphStoreRead,
    snapshot: Snapshot,
    interrupted: MigrationMarker | None,
) -> tuple[list[Finding], dict[str, JsonValue] | None, tuple[WarningRecord, ...]]:
    """The data under an authored ontology: the findings, the ``migration`` block and its
    warnings. A version other than the pin is checked after its migration, if it has one."""
    if version == entry.pin.version or interrupted is not None:
        return check_graph(new, snapshot), None, ()  # nothing to migrate: the data is as it is
    file = migrations.parse(raw)
    if file is None:
        return check_graph(new, snapshot), None, ()  # no migration: the data as it is
    old = pinned_model(ctx.catalog, entry)
    renames = derive_renames(old, new)
    major = Version.parse(version).major > Version.parse(entry.pin.version).major
    migrations.validate(
        file,
        old,
        new,
        renames,
        from_version=entry.pin.version,
        major=major,
        language=entry.language,
    )
    run = migrations.plan_data(
        ctx,
        snapshot,
        read.head(),
        old,
        new,
        renames,
        file,
        language=entry.language,
        dry_run=True,
    )
    plan = run.plan
    assert plan is not None
    block = migrations.preview(file, plan, completing=False)
    return migrations.judge(new, snapshot, plan), block, migrations.unused_steps(run)
