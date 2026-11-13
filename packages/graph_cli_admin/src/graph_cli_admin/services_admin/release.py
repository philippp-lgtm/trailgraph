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
installed with other content (``admin.release_exists``), a ``renamed_from`` that
does not fit the pinned release (``ontology.rename_invalid``; a leftover of the
previous release is the warning ``ontology.rename_stale``), a bump smaller
than the changes (``admin.version_bump_insufficient``, also downgrades),
a higher major on a graph whose data has error findings under the new
version (``check_graph`` under the lock, B7.14: ``admin.migration_required``
with the counts and the first 20 findings). A larger bump than needed is a
warning.
Re-running a release that is already pinned changes nothing.

A major release may carry a data migration (``migration.yaml`` in the version directory,
plan M1-M5; ``services_admin.migration``): ``plan`` computes the migrated revisions under the
lock and ``check_graph`` judges the data after them, so ``admin.migration_required`` now means
the data does not fit *after* the migration (or a rename rewrites data and there is no file).
``write`` runs the sequence of plan M3 under the lock it holds: install the release (the file
is part of its identity), write the migration marker, append the ``maint.migrate`` intent,
commit the migration tx, switch the pin and the alias prefixes, remove the marker. A crash
before the commit leaves the old state (a stale marker is ignored and overwritten); a crash
after it leaves the incomplete state, which reads see under the target model, which refuses
writes, and which the same release run again finishes (``admin.migration_incomplete`` for any
other).

Every bump also re-checks the published function catalog against the new
model (``graph_cli.kinetic.check_catalog``, K5): a function that no longer
resolves refuses the release with ``admin.functions_broken`` (first 20
problems in the details). The published action catalog gets the same check
(``check_actions``, ``admin.actions_broken``), against the function names the graph
will have. The fix is ``graph push`` with the ontology and the fixed or deleted
files together; ``plan`` then leaves the replaced catalog to the push, which checks
the new files against the same model.

``plan`` makes every check under the lock and ``write`` writes, so
``graph push`` (``sources``) runs a release, a skill publish and a function and
action publish under one lock, checking all before the first write.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from graph_cli.access_control.terms import named_read_types, named_terms
from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import TermRef, TxId
from graph_cli.kernel.json_access import as_object, req_list, req_str
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, ManifestData, SlotKind
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import Head, MaintenanceSession
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.kinetic.action_compile import check_actions
from graph_cli.kinetic.compile import check_catalog
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import Renames, derive_renames
from graph_cli.ontology_layer.model import CompiledModel, load_compiled
from graph_cli.ontology_layer.semver import Version
from graph_cli.services.migration import build_migration_tx, migration_record
from graph_cli.services.term_model import extended_types, term_model
from graph_cli_admin.services_admin import migration as migrations
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.graphs import PrefixChanges, merged_alias_prefixes
from graph_cli_admin.services_admin.migration import MigrationRun
from graph_cli_admin.services_admin.ontology import (
    COMPILED_FILE,
    Compiled,
    PinnedModel,
    compile_source,
    diff,
    installed_migration,
    module_bump_warnings,
    pinned_model,
    read_migration,
    release_files,
    rename_warnings,
)

MAX_FINDINGS = 20
FUNCTIONS_DAMAGED = frozenset({"functions.slot_invalid", "format.unsupported_version"})
ACTIONS_DAMAGED = frozenset({"actions.slot_invalid", "format.unsupported_version"})
DAMAGED: Final[dict[SlotKind, frozenset[str]]] = {
    "functions": FUNCTIONS_DAMAGED,
    "actions": ACTIONS_DAMAGED,
}


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
    prefix_changes: PrefixChanges | None = None  # ``None`` when nothing is released
    migration: MigrationRun | None = None  # ``None`` for a release without a data migration
    clear_marker: bool = False  # a marker no migration needs any more: stale, or finished

    @property
    def unchanged(self) -> bool:
        return self.old is None and not self.clear_marker


@dataclass(frozen=True, slots=True)
class Decisions:
    release: tuple[Decision, ...]
    pin: tuple[Decision, ...]
    migrate: tuple[Decision, ...] = ()  # ``maint.migrate``, decided when the release migrates


def authorize(
    ctx: AdminContext,
    entry: GraphEntry,
    meta: RequestMeta,
    *,
    reason: str | None = None,
    migrate: bool = False,
) -> Decisions:
    """The decisions of a release, before the lock (B6.3). ``migrate``: the release carries
    a migration file or renames, so ``maint.migrate`` is decided too (plan M4; a project
    admin has no such right: ``access.ceiling``)."""
    resource = f"graph:{entry.id}"
    pairs = [("maint.ontology_release", resource)]
    release = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    pairs = [("maint.ontology_pin", resource)]
    pin = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    migration: tuple[Decision, ...] = ()
    if migrate:
        pairs = [("maint.migrate", resource)]
        migration = ctx.authorize(pairs, request=meta, graph=entry.id, reason=reason)
    ctx.read_data(entry, meta)  # a release checks the data; its findings quote values
    return Decisions(release, pin, migration)


def wants_migration(
    ctx: AdminContext, entry: GraphEntry, compiled: Compiled, migration: bytes | None
) -> bool:
    """Whether releasing ``compiled`` may rewrite data: it has a migration file, or the models
    (a lock-free look at the pinned release) have renames. Decides ``maint.migrate`` before the
    lock; a release already pinned changes nothing."""
    if compiled.pin == entry.pin:
        return False
    if migration is not None:
        return True
    renames = derive_renames(pinned_model(ctx.catalog, entry), compiled.model)
    return migrations.has_renames(renames)


def release(
    ctx: AdminContext,
    *,
    graph_ref: str,
    source: Path,
    module: str | None,
    reason: str | None = None,
    dry_run: bool = False,
) -> Released:
    """Release and pin ``source``; ``dry_run`` makes every check under the lock, shows what
    would change (``migration`` in the data) and writes nothing."""
    compiled = compile_source(source, module)
    migration = read_migration(source)
    entry = ctx.resolve(graph_ref, "ontology release")
    check_state(entry)
    arguments: dict[str, JsonValue] = {"graph": str(entry.id), "ontology": compiled.pin.to_json()}
    if dry_run:  # a real release's request stays what it was
        arguments["dry_run"] = True
    meta = RequestMeta.of("ontology release", arguments)
    decisions = authorize(
        ctx,
        entry,
        meta,
        reason=reason,
        migrate=wants_migration(ctx, entry, compiled, migration),
    )
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        planned = plan(ctx, session, compiled, migration=migration, dry_run=dry_run)
        if planned.unchanged or dry_run:
            return Released(planned.manifest.entry, planned.data, planned.seq, planned.warnings)
        pinned = write(ctx, session, planned, decisions, meta, reason=reason)
        return Released(pinned, planned.data, planned.seq, planned.warnings)


def plan(
    ctx: AdminContext,
    session: MaintenanceSession,
    compiled: Compiled,
    *,
    migration: bytes | None = None,
    dry_run: bool = False,
    functions_replaced: bool = False,
    actions_replaced: bool = False,
    function_names: frozenset[str] | None = None,
) -> Planned:
    """Every check of a release, under the lock; refuses before anything is written.

    ``migration`` is the bytes of the version directory's ``migration.yaml`` (``None``:
    there is none). ``dry_run`` plans with a placeholder tx id, so a dry run draws no id.
    ``functions_replaced`` / ``actions_replaced``: the caller publishes new function or
    action files after the release and checks them against the new model itself, so the
    published catalog of that kind is not checked here. ``function_names`` are the
    functions the graph will have (the pushed ones when ``functions_replaced``); omitted,
    the published ones."""
    target = compiled.pin
    manifest = session.manifest()  # W.6: re-read under the lock
    entry = manifest.entry
    check_state(entry)
    current = entry.pin
    file = migrations.parse(migration)  # the shape: migration.invalid
    installed = session.release_hashes()
    if target.version in installed:
        _check_identity(session, target.version, installed[target.version], target, file)
    head = session.head()
    data: dict[str, JsonValue] = {
        "graph_id": str(entry.id),
        "from": current.to_json(),
        "to": target.to_json(),
        "released": False,
        "pinned": False,
    }
    if dry_run:
        data["dry_run"] = True
    marker, committed = _marker(session)
    if current == target:
        clear = marker is not None
        if marker is not None and committed and not _finished(marker, current):
            raise migrations.incomplete(marker, current.version)
        return Planned(manifest, compiled, None, False, {}, data, head.seq, (), clear_marker=clear)
    old_model = load_compiled(
        session.read_release_file(current.version, COMPILED_FILE),
        expected_hash=current.artifact_hash,
    )
    report, bump = diff(old_model, compiled.model)
    data["diff"] = report.to_json()
    rename_notes = rename_warnings(old_model, compiled.model)  # refuses an invalid renamed_from
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
    major = Version.parse(target.version).major > Version.parse(current.version).major
    renames = derive_renames(old_model, compiled.model)
    if file is not None:
        migrations.validate(
            file,
            old_model,
            compiled.model,
            renames,
            from_version=current.version,
            major=major,
            language=entry.language,
        )
    completing: MigrationMarker | None = None
    clear = marker is not None
    if marker is not None and committed and not _finished(marker, current):
        if (
            file is None
            or (marker.from_version, marker.to_version)
            != (
                current.version,
                target.version,
            )
            or (marker.artifact_hash, marker.migration_hash) != (target.artifact_hash, file.hash)
        ):
            raise migrations.incomplete(marker, current.version)
        completing, clear = marker, False  # the data is migrated: only the pin is left
    run: MigrationRun | None = None
    if major and head.seq:
        run = _check_data(
            ctx,
            session,
            old_model,
            compiled.model,
            renames,
            file,
            completing,
            bump_details,
            head,
            entry,
            dry_run=dry_run,
        )
    elif file is not None:
        run = MigrationRun(file, renames, None, None, completing)
    functions = current_catalog(session, "functions")
    if not functions_replaced and functions is not None:
        broken = catalog_problems(functions, compiled.model)
        if broken:
            raise functions_broken(broken, bump_details)
    if not actions_replaced:
        actions = current_catalog(session, "actions")
        if actions is not None:
            if function_names is None:
                function_names = names_of(functions)
            broken = catalog_problems(actions, compiled.model, function_names=function_names)
            if broken:
                raise actions_broken(broken, bump_details)
    warnings = module_bump_warnings(old_model, compiled.model, report) + rename_notes
    warnings += policies_affected(ctx.policy.current(entry.id), entry, compiled.model)
    if bump == "excessive":
        warnings += (
            WarningRecord(
                "admin.version_bump_excessive",
                f"{current.version} -> {target.version} is more than {report.level} needs",
                bump_details,
            ),
        )
    if run is not None and file is not None:
        warnings += migrations.unused_steps(run)
        data["migration"] = migrations.preview(file, run.plan, completing=completing is not None)
    changes = merged_alias_prefixes(
        entry.alias_prefixes, compiled, renames=renames, retypes=migrations.retypes(file)
    )
    prefixes, skipped = changes.merged, changes.skipped
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
    return Planned(
        manifest, compiled, old, install, prefixes, data, head.seq, warnings, changes, run, clear
    )


def _check_identity(
    session: MaintenanceSession,
    version: str,
    installed_hash: str,
    target: OntologyPin,
    file: migrations.Migration | None,
) -> None:
    """An installed version is identified by its artifact hash and its migration file
    (plan M1): the same version with other content or another migration is
    ``admin.release_exists``."""
    kept = installed_migration(lambda name: session.read_release_file(version, name))
    kept_hash = None if kept is None else migrations.hash_of(kept)
    wanted = None if file is None else file.hash
    if installed_hash == target.artifact_hash and kept_hash == wanted:
        return
    details: dict[str, JsonValue] = {
        "version": version,
        "expected": installed_hash,
        "actual": target.artifact_hash,
    }
    what = "other content"
    if kept_hash != wanted:
        details["migration_hash"] = {"expected": kept_hash, "actual": wanted}
        what = "another migration file" if installed_hash == target.artifact_hash else what
    raise GraphCliError(
        "admin.release_exists", f"release {version} is installed with {what}", details=details
    )


def _marker(session: MaintenanceSession) -> tuple[MigrationMarker | None, bool]:
    """The graph's migration marker and whether its tx is in the log (``marker_committed``:
    a stale marker, whose tx never committed, is not)."""
    marker = session.migration_marker()
    if marker is None:
        return None, False
    return marker, marker_committed(session.snapshot(), marker)


def _finished(marker: MigrationMarker, pin: OntologyPin) -> bool:
    """The pin has reached the marker's version: only the marker is left over."""
    return (marker.to_version, marker.artifact_hash) == (pin.version, pin.artifact_hash)


def _check_data(
    ctx: AdminContext,
    session: MaintenanceSession,
    old: CompiledModel,
    new: CompiledModel,
    renames: Renames,
    file: migrations.Migration | None,
    completing: MigrationMarker | None,
    bump: dict[str, JsonValue],
    head: Head,
    entry: GraphEntry,
    *,
    dry_run: bool,
) -> MigrationRun:
    """B7.14 under the migration (plan M3): the data of a major release, after the migration
    the release carries, must check clean. Without a file or renames that is the data as it
    is; a release in the incomplete state has migrated it already and plans nothing."""
    snapshot = session.snapshot()
    plan = None
    run = MigrationRun(file, renames, None, None, completing)
    if completing is None and (file is not None or migrations.has_renames(renames)):
        run = migrations.plan_data(
            ctx,
            snapshot,
            head,
            old,
            new,
            renames,
            file,
            language=entry.language,
            dry_run=dry_run,
        )
        plan = run.plan
        assert plan is not None
        if file is None and plan.revisions:
            raise migrations.required_for_renames(plan, check_graph(new, snapshot), bump, head.seq)
    findings = migrations.judge(new, snapshot, plan)
    if any(f.severity is Severity.ERROR for f in findings):
        raise migrations.required(findings, bump, head.seq, migrated=plan is not None)
    return run


def write(
    ctx: AdminContext,
    session: MaintenanceSession,
    planned: Planned,
    decisions: Decisions,
    meta: RequestMeta,
    *,
    reason: str | None = None,
) -> GraphEntry:
    """Install (unless installed), migrate and pin a planned release; answers the pinned
    entry. The order is that of plan M3: release, marker, ``maint.migrate`` intent, the
    migration tx, pin, marker removed."""
    entry = planned.manifest.entry
    if planned.old is None:  # the pin is switched already: a marker is all that is left
        session.remove_migration_marker()
        return entry
    target = planned.compiled.pin
    run = planned.migration
    writes = run is not None and run.writes
    if planned.clear_marker and not writes:
        session.remove_migration_marker()  # stale, or the migration it names is finished
    file = run.migration if run is not None else None
    if planned.install:
        release_id = ctx.ids.new_id()
        files = release_files(
            planned.compiled,
            previous=planned.old,
            release_id=release_id,
            released_at=ctx.clock.now(),
            migration=None if file is None else file.raw,
        )
        change = events.release_change(
            target.version,
            target.artifact_hash,
            release_id,
            None if file is None else file.hash,
        )
        receipt = ctx.intent(session, entry, decisions.release, meta, change, reason=reason)
        session.install_release(target.version, files, receipt)
        ctx.anchor(entry, decisions.release, meta, receipt, reason=reason)
        planned.data["released"] = True
    if run is not None and writes:
        _migrate(ctx, session, planned, run, decisions, meta, reason)
    changes = planned.prefix_changes
    moved = changes is not None and bool(changes.moved)
    change = events.pin_change(
        entry.pin, target, changes.to_json() if changes is not None and moved else None
    )
    receipt = ctx.intent(session, entry, decisions.pin, meta, change, reason=reason)
    pinned = replace(
        planned.manifest, entry=replace(entry, pin=target, alias_prefixes=planned.prefixes)
    )
    session.replace_manifest(pinned, receipt)
    ctx.anchor(entry, decisions.pin, meta, receipt, reason=reason)
    planned.data["pinned"] = True
    if writes or (run is not None and run.completing is not None):
        session.remove_migration_marker()
    return pinned.entry


def _migrate(
    ctx: AdminContext,
    session: MaintenanceSession,
    planned: Planned,
    run: MigrationRun,
    decisions: Decisions,
    meta: RequestMeta,
    reason: str | None,
) -> None:
    """The migration effect of plan M3 under the held lock: the marker (so a crash after the
    commit is recognized), the ``maint.migrate`` intent whose event id the tx carries, the
    commit of the migration tx."""
    plan, stamp, file = run.plan, run.stamp, run.migration
    assert plan is not None and stamp is not None and file is not None
    entry, target = planned.manifest.entry, planned.compiled.pin
    started = time.monotonic()
    taken = ctx.decide_under_lock(entry, decisions.migrate, meta, reason=reason)
    record = migration_record(
        from_version=entry.pin.version,
        to_version=target.version,
        renames=run.renames,
        doc=file.doc,
    )
    tx = build_migration_tx(
        plan,
        planned.compiled.model,
        graph_id=entry.id,
        tx_id=stamp.tx_id,
        event_id=ctx.event_id(),
        seq=stamp.tx_seq,
        recorded_at=stamp.recorded_at,
        actor=ctx.actor,
        decisions=taken,
        ontology=target,
        reason=reason,
        request=meta,
        record=record,
    )
    marker = MigrationMarker(
        seq=tx.seq,
        tx_id=TxId(tx.tx_id),
        from_version=entry.pin.version,
        to_version=target.version,
        artifact_hash=target.artifact_hash,
        migration_hash=file.hash,
    )
    session.write_migration_marker(marker)
    change = events.migration_change(
        tx,
        revisions_hash=events.revisions_hash(tx.revisions),
        counts={"entities": len(tx.revisions), "edges": plan.edges_changed},
    )
    receipt = ctx.intent(session, entry, taken, meta, change, reason=reason, event_id=tx.event_id)
    session.migrate(tx, receipt)
    block = planned.data.get("migration")
    if isinstance(block, dict):
        block["migrated"] = migrations.migrated(plan)
        block["tx_seq"] = tx.seq
        block["duration_s"] = round(time.monotonic() - started, 3)


def current_catalog(session: MaintenanceSession, kind: SlotKind) -> KineticSlot | None:
    """The published catalog of ``kind``; a damaged slot is replaced like a missing one."""
    try:
        return session.read_functions() if kind == "functions" else session.read_actions()
    except GraphCliError as exc:
        if exc.code not in DAMAGED[kind]:
            raise
        return None


def names_of(slot: KineticSlot | None) -> frozenset[str]:
    """The names in a published catalog (deprecated ones too: they still hold the name)."""
    if slot is None:
        return frozenset()
    key = slot.spec.list_key
    return frozenset(req_str(as_object(e, key), "name") for e in req_list(slot.compiled, key))


def catalog_problems(
    slot: KineticSlot | None,
    model: CompiledModel,
    *,
    function_names: frozenset[str] | None = None,
) -> list[Problem]:
    """The problems a published catalog has against ``model``; none for a missing one.
    ``function_names`` are the functions the graph has: an action may not share a name."""
    if slot is None:
        return []
    if slot.kind == "functions":
        return check_catalog(slot.compiled, model)
    return check_actions(slot.compiled, model, function_names=function_names or frozenset())


def catalog_broken(
    kind: SlotKind,
    problems: list[Problem],
    bump: dict[str, JsonValue],
    *,
    advice: str,
) -> GraphCliError:
    """``admin.<kind>_broken``: the first problems of the published catalog against the
    model of ``bump['to']``."""
    names = sorted({p.name for p in problems})
    return GraphCliError(
        f"admin.{kind}_broken",
        _would_not_resolve(kind, names, str(bump["to"])) + advice,
        details={
            **bump,
            "count": len(problems),
            kind: list[JsonValue](names[:MAX_FINDINGS]),
            "problems": [p.to_json() for p in problems[:MAX_FINDINGS]],
        },
    )


def functions_broken(
    problems: list[Problem],
    bump: dict[str, JsonValue],
    *,
    advice: str = "; fix or delete them in the same push",
) -> GraphCliError:
    """``admin.functions_broken`` (see ``catalog_broken``)."""
    return catalog_broken("functions", problems, bump, advice=advice)


def actions_broken(
    problems: list[Problem],
    bump: dict[str, JsonValue],
    *,
    advice: str = "; fix or delete them in the same push",
) -> GraphCliError:
    """``admin.actions_broken`` (see ``catalog_broken``)."""
    return catalog_broken("actions", problems, bump, advice=advice)


def _would_not_resolve(kind: SlotKind, names: list[str], version: str) -> str:
    shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
    noun = "function" if kind == "functions" else "action"
    plural = "s" if len(names) != 1 else ""
    return f"{len(names)} published {noun}{plural} would not resolve under {version}: {shown}"


def catalog_affected(
    kind: SlotKind,
    slot: KineticSlot | None,
    model: CompiledModel,
    *,
    function_names: frozenset[str] | None = None,
) -> tuple[WarningRecord, ...]:
    """``admin.<kind>_affected`` when the published catalog would not resolve under
    ``model`` (what ``ontology diff --graph`` shows before a release refuses)."""
    problems = catalog_problems(slot, model, function_names=function_names)
    if not problems:
        return ()
    names = sorted({p.name for p in problems})
    return (
        WarningRecord(
            f"admin.{kind}_affected",
            _would_not_resolve(kind, names, model.bundle_version),
            {
                "to": model.bundle_version,
                "count": len(problems),
                kind: list[JsonValue](names[:MAX_FINDINGS]),
                "problems": [p.to_json() for p in problems[:MAX_FINDINGS]],
            },
        ),
    )


def functions_affected(slot: KineticSlot | None, model: CompiledModel) -> tuple[WarningRecord, ...]:
    return catalog_affected("functions", slot, model)


def actions_affected(
    actions: KineticSlot | None, functions: KineticSlot | None, model: CompiledModel
) -> tuple[WarningRecord, ...]:
    """The action twin of ``functions_affected``; the action names are checked against the
    published functions."""
    return catalog_affected("actions", actions, model, function_names=names_of(functions))


def policies_affected(
    policy: PolicySet, entry: GraphEntry, model: CompiledModel
) -> tuple[WarningRecord, ...]:
    """What the installed policies name for ``entry`` that ``model`` no longer has (write
    allow lists plan W8): the types and terms of the installation's write grants and of the
    graph layer's term deny rules, with the warnings of ``policy check``:
    ``admin.policy_unknown_type`` for a type the model lacks, and for a type named in a write
    grant that is now abstract or has subclasses (an exact rule misses the entities of its
    subclasses);
    ``admin.policy_unknown_term`` for an attribute, relation or lifecycle attribute the type
    lacks (fail closed: the entry has no effect until a release adds it).

    The read rules (type read limits plan V2, V3) get their own messages under the same code:
    a type of a ``reads`` allow list that is unknown, or now abstract or with subclasses (the
    subclasses stay hidden: fail closed, but probably not meant), and a type a deny rule hides
    that is unknown or now abstract or with subclasses (the subclasses are visible: the deny
    names the exact type). A policy without write grants, term rules and read rules gives
    none."""
    named = named_terms(policy.documents, entry.id)
    read_named = named_read_types(policy.documents, entry.id)
    if not named and not read_named:
        return ()
    terms = term_model(model).types
    extended = extended_types(model.classes)
    version = model.bundle_version
    warnings: list[WarningRecord] = []
    unknown: set[str] = set()
    for item in named:
        found = terms.get(item.type)
        if item.kind == "type":
            if found is None:
                if item.type in unknown:
                    continue  # one warning per type, whichever rule names it
                unknown.add(item.type)
                warnings.append(
                    WarningRecord(
                        "admin.policy_unknown_type",
                        f"release {version} has no type {item.type} in graph {entry.name}, which"
                        " an installed policy names; its write grants and term rules apply to"
                        " nothing until a release has it",
                        {"graph": entry.name, "type": item.type},
                    )
                )
            elif item.grant and item.type in extended:
                warnings.append(
                    WarningRecord(
                        "admin.policy_unknown_type",
                        f"type {item.type} of graph {entry.name} is abstract or has subclasses in"
                        f" release {version}: its write grant is an exact rule and now misses the"
                        " entities of the subclasses; grant their types too",
                        {"graph": entry.name, "type": item.type},
                    )
                )
        elif found is not None:
            attribute = found.attributes.get(item.term)
            missing = (
                item.term not in found.relations
                if item.kind == "relation"
                else attribute is None or (item.kind == "lifecycle" and not attribute.lifecycle)
            )
            if missing:
                what = {
                    "relation": "relation",
                    "attribute": "attribute",
                    "lifecycle": "lifecycle attribute",
                }[item.kind]
                warnings.append(
                    WarningRecord(
                        "admin.policy_unknown_term",
                        f"type {item.type} of graph {entry.name} has no {what} {item.term} in"
                        f" release {version}, which an installed policy names; nobody can write"
                        " a term that does not exist (fail closed), so the policy entry has no"
                        " effect until a release adds it",
                        {
                            "graph": entry.name,
                            "type": item.type,
                            "term": item.term,
                            "kind": item.kind,
                        },
                    )
                )
    for read in read_named:
        details: dict[str, JsonValue] = {"graph": entry.name, "type": read.type}
        if read.type not in terms:
            text = (
                "a read allow list (reads) of an installed policy names it; it lets nobody see"
                " the type until a release has it"
                if read.grant
                else "an installed policy hides it; the deny applies to nothing until a release"
                " has it"
            )
            warnings.append(
                WarningRecord(
                    "admin.policy_unknown_type",
                    f"release {version} has no type {read.type} in graph {entry.name}, which"
                    f" {text}",
                    details,
                )
            )
        elif read.type in extended:
            text = (
                "its read allow list (reads) is an exact rule and the subclasses stay hidden"
                " (fail closed); list their types too"
                if read.grant
                else "the read deny of an installed policy is an exact rule and now leaves the"
                " entities of the subclasses visible; hide their types too"
            )
            warnings.append(
                WarningRecord(
                    "admin.policy_unknown_type",
                    f"type {read.type} of graph {entry.name} is abstract or has subclasses in"
                    f" release {version}: {text}",
                    details,
                )
            )
    return tuple(warnings)


def check_state(entry: GraphEntry) -> None:
    if entry.state == "archived":
        raise GraphCliError(
            "graph.archived", f"graph {entry.name} is archived", details={"graph_id": str(entry.id)}
        )
