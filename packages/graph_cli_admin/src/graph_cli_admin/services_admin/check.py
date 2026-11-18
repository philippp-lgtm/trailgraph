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
  The published view catalog is checked against the model and those functions
  (``check_views``): ``admin.views_broken``, ``views`` in the data (only when views are
  published). When several are broken, the functions are refused first, then the actions, then
  the views (fix, check again).
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
- The same preview shows what the release does with the policy and the catalogs (plan M8): a
  deny rule that names a renamed term and no new one fails with ``admin.policy_renamed_term``,
  as the release would; published functions and actions that would be compiled again and
  published with the release are listed in ``republished``; those that cannot be are
  ``admin.functions_broken`` / ``admin.actions_broken`` / ``admin.views_broken`` as before. A
  release that would show marked values under a name declared without the marking fails with
  ``ontology.marking_name_reused`` (``marking_names``), its warnings come along.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from graph_cli.access_control.terms import rule_entries
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, has_errors
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead, Snapshot
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import Renames, derive_renames
from graph_cli.ontology_layer.migration import Retype
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.services_admin import marking_names, renamed_terms
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
    follow_published,
    functions_broken,
    republished_json,
    views_broken,
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
    # a marker left over after the pin switch is no interrupted release: the next release
    # deletes it
    interrupted = (
        marker
        if marker is not None
        and marker_committed(snapshot, marker)
        and not marker.reached_by(entry.pin)
        else None
    )
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
    pin = entry.pin
    renames: Renames | None = None
    retypes: tuple[Retype, ...] = ()
    file: migrations.Migration | None = None
    hop: Hop | None = None  # the release this check previews, when it would make one
    if source is None:
        model = _migrated_model(ctx, entry, interrupted) or pinned_model(ctx.catalog, entry)
        findings = check_graph(model, snapshot)
    else:
        compiled = compile_source(source, module)
        model, pin = compiled.model, compiled.pin
        if pin.version != entry.pin.version and interrupted is None:
            old = pinned_model(ctx.catalog, entry)
            renames = derive_renames(old, model)
            file = migrations.parse(read_migration(source))
            retypes = migrations.retypes(file)
            doc = None if file is None else file.doc
            hop = Hop(entry.pin.version, pin.version, old, model, renames, doc)
        findings, block, extra = _authored(ctx, entry, model, file, renames, read, snapshot)
        warnings += extra
    codes = Counter(f.code for f in findings)
    slot = ctx.catalog.read_functions(entry.id, sources=True)
    published = ctx.catalog.read_actions(entry.id, sources=True)
    catalog = ctx.catalog.read_views(entry.id, sources=True)
    # what the release would do with the catalogs: publish them again or leave them broken
    recompile = file is not None or (renames is not None and migrations.has_renames(renames))
    followed_functions, followed_actions, followed_views = follow_published(
        slot, published, catalog, model, pin, recompile=recompile
    )
    problems, action_problems = followed_functions.problems, followed_actions.problems
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
    if catalog is not None:  # a graph without views answers as it did
        data["views"] = {
            "count": catalog.count,
            "problems": [p.to_json() for p in followed_views.problems],
        }
    if block is not None:
        data["migration"] = block
    republished = republished_json(followed_functions, followed_actions, followed_views)
    if republished:
        data["republished"] = republished
    if has_errors(findings):
        raise GraphCliError(
            "admin.data_errors",
            f"the data of {entry.name} has errors under {model.bundle_version}: {data['counts']}",
            details={k: data[k] for k in ("ontology", "counts", "codes", "findings")},
        )
    bump: dict[str, JsonValue] = {"to": model.bundle_version, "pinned": source is None}
    if renames is not None and (migrations.has_renames(renames) or retypes):
        entries = rule_entries(ctx.policy.current(entry.id).documents)
        found = renamed_terms.hazards(entries, entry.id, renames, retypes, model)
        if found:
            raise renamed_terms.refusal(found, bump)
    if hop is not None:
        warnings += marking_names.check(ctx, entry, hop, bump)
    if problems:
        raise functions_broken(problems, bump, advice="")
    if action_problems:
        raise actions_broken(action_problems, bump, advice="")
    if followed_views.problems:
        raise views_broken(followed_views.problems, bump, advice="")
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
    new: CompiledModel,
    file: migrations.Migration | None,
    renames: Renames | None,
    read: GraphStoreRead,
    snapshot: Snapshot,
) -> tuple[list[Finding], dict[str, JsonValue] | None, tuple[WarningRecord, ...]]:
    """The data under an authored ontology: the findings, the ``migration`` block and its
    warnings. A version other than the pin is checked after its migration, if it has one
    (``file`` and ``renames`` are set only for such a version)."""
    if file is None or renames is None:
        return check_graph(new, snapshot), None, ()  # nothing to migrate: the data is as it is
    old = pinned_model(ctx.catalog, entry)
    version = new.bundle_version
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
