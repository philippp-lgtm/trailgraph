"""The migration of a release, as the maintenance tools see it (migration plan M1-M3, M9).

A version directory may hold ``migration.yaml``. This module turns its bytes into a checked
``Migration``, judges it against the two models of a release, plans the data change as a pure
computation and describes it for the answers of ``ontology release``, ``graph push`` and
``graph check --ontology``. Writing is ``release.write``.

Refusals, in the order a release meets them: ``migration.invalid`` (the file's shape, then its
steps against the models), ``admin.migration_unexpected`` (a migration on a minor or patch
release), ``admin.migration_from_mismatch`` (``from`` is not the pinned version),
``admin.migration_incomplete`` (another release in the incomplete state), and
``admin.migration_required`` (the data does not fit after the migration, or a rename rewrites
data and there is no file).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ids import TxId
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.ports.storage import Head, Snapshot
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.migration import (
    MigrationDoc,
    MigrationProblem,
    Retype,
    check_migration,
    migration_hash,
    parse_migration,
)
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import MIGRATION_FILE
from graph_cli.temporal_knowledge.migrate import MigrationPlan, MigrationStamp, plan_migration
from graph_cli_admin.services_admin.context import AdminContext

MAX_FINDINGS: Final = 20
MAX_PROBLEMS: Final = 50
PLACEHOLDER_TX: Final = TxId(UUID(int=0))  # the tx id of a dry run: nothing is written


@dataclass(frozen=True, slots=True)
class Migration:
    """A migration file that passed the shape check: its bytes as authored, the document and
    its identity (``migration_hash`` over the normalized document)."""

    raw: bytes
    doc: MigrationDoc
    hash: str


@dataclass(frozen=True, slots=True)
class MigrationRun:
    """What a release does to the data. ``plan`` is ``None`` when nothing is computed: a
    release in the incomplete state, which only finishes the pin (``completing`` is its
    marker), or a graph without data. ``stamp`` identifies the tx the plan was made for."""

    migration: Migration | None
    renames: Renames
    plan: MigrationPlan | None
    stamp: MigrationStamp | None
    completing: MigrationMarker | None = None

    @property
    def writes(self) -> bool:
        """Whether the release commits a migration tx."""
        return self.plan is not None and bool(self.plan.revisions)


def invalid(problems: tuple[MigrationProblem, ...] | list[MigrationProblem]) -> GraphCliError:
    """``migration.invalid`` with the problems (``path`` and ``message``) in the details."""
    shown = list(problems[:MAX_PROBLEMS])
    first = shown[0]
    where = f"{first.path}: " if first.path else ""
    return GraphCliError(
        "migration.invalid",
        f"{MIGRATION_FILE} has {len(problems)} problems; first: {where}{first.message}",
        details={
            "file": MIGRATION_FILE,
            "count": len(problems),
            "problems": [p.to_json() for p in shown],
        },
    )


def parse(raw: bytes | None) -> Migration | None:
    """The migration file's shape (``migration.invalid`` for any problem); ``None`` without a
    file. The steps are checked against the models by ``validate``."""
    if raw is None:
        return None
    parsed = parse_migration(raw)
    if parsed.doc is None:
        raise invalid(parsed.problems)
    return Migration(raw, parsed.doc, migration_hash(parsed.doc))


def hash_of(raw: bytes) -> str | None:
    """The identity of a stored ``migration.yaml``; ``None`` for bytes that are no migration."""
    parsed = parse_migration(raw)
    return None if parsed.doc is None else migration_hash(parsed.doc)


def validate(
    migration: Migration,
    old: CompiledModel,
    new: CompiledModel,
    renames: Renames,
    *,
    from_version: str,
    major: bool,
    language: str | None,
) -> None:
    """The file against the release: a major bump (``admin.migration_unexpected``), ``from``
    equal to the pinned version (``admin.migration_from_mismatch``) and every step against
    the old and the new model (``migration.invalid``)."""
    to_version = new.bundle_version
    if not major:
        raise GraphCliError(
            "admin.migration_unexpected",
            f"{MIGRATION_FILE} accompanies a major release; {from_version} -> {to_version} is not"
            " one (renames and data changes need a higher major version)",
            details={"file": MIGRATION_FILE, "from": from_version, "to": to_version},
        )
    if migration.doc.from_version != from_version:
        raise GraphCliError(
            "admin.migration_from_mismatch",
            f"{MIGRATION_FILE} says from: {migration.doc.from_version} but the graph is pinned to"
            f" {from_version}; a graph several majors behind releases them one by one",
            details={
                "file": MIGRATION_FILE,
                "from": migration.doc.from_version,
                "expected": from_version,
                "to": to_version,
            },
        )
    problems = check_migration(migration.doc, old, new, renames, language=language)
    if problems:
        raise invalid(problems)


def retypes(migration: Migration | None) -> tuple[Retype, ...]:
    """The ``retype`` steps of the file, for the alias prefixes that follow them."""
    if migration is None:
        return ()
    return tuple(s for s in migration.doc.steps if isinstance(s, Retype))


def has_renames(renames: Renames) -> bool:
    return bool(renames.classes or renames.relations or renames.attributes)


def stamp(ctx: AdminContext, head: Head, version: str, *, dry_run: bool) -> MigrationStamp:
    """What the migrated revisions record: the tx the release will commit (its id is new, a
    placeholder for a dry run), the seq after the head, a time not before the head's."""
    now = ctx.clock.now()
    at = now if head.recorded_at is None else max(now, head.recorded_at)
    tx_id = PLACEHOLDER_TX if dry_run else TxId(ctx.ids.new_id())
    return MigrationStamp(tx_id, head.seq + 1, at, version)


def plan_data(
    ctx: AdminContext,
    snapshot: Snapshot,
    head: Head,
    old: CompiledModel,
    new: CompiledModel,
    renames: Renames,
    migration: Migration | None,
    *,
    language: str | None,
    dry_run: bool,
) -> MigrationRun:
    """The migrated revisions of ``snapshot`` and the stamp of their tx (a pure computation:
    nothing is written, an id is drawn only for a real run)."""
    made = stamp(ctx, head, new.bundle_version, dry_run=dry_run)
    plan = plan_migration(
        old,
        new,
        renames,
        None if migration is None else migration.doc,
        snapshot,
        stamp=made,
        language=language,
    )
    return MigrationRun(migration, renames, plan, made)


def judge(new: CompiledModel, snapshot: Snapshot, plan: MigrationPlan | None) -> list[Finding]:
    """Every finding of the data under ``new`` after the migration: those of the plan (a
    value that cannot be converted) and ``check_graph`` over the overlay; the snapshot itself
    when nothing is planned."""
    if plan is None:
        return check_graph(new, snapshot)
    return [*plan.findings, *check_graph(new, plan.overlay(snapshot))]


def required(
    findings: list[Finding], bump: dict[str, JsonValue], seq: int, *, migrated: bool
) -> GraphCliError:
    """``admin.migration_required``: the data still has error findings under the new major
    (after the migration, when there is one)."""
    errors = [f for f in findings if f.severity is Severity.ERROR]
    counts = Counter(f.code for f in errors)
    after = " after the migration" if migrated else ""
    advice = (
        f"; change the steps of {MIGRATION_FILE} or the data"
        if migrated
        else f"; the data needs a migration ({MIGRATION_FILE} next to the module files)"
    )
    return GraphCliError(
        "admin.migration_required",
        f"{len(errors)} error findings under {bump['to']}{after}{advice}",
        details={
            **bump,
            "seq": seq,
            "counts": dict(sorted(counts.items())),
            "findings": [f.to_json() for f in errors[:MAX_FINDINGS]],
            "migrated": migrated,
        },
    )


def required_for_renames(
    plan: MigrationPlan, findings: list[Finding], bump: dict[str, JsonValue], seq: int
) -> GraphCliError:
    """``admin.migration_required`` for a release whose renames rewrite stored data and has
    no ``migration.yaml``: the file is the opt-in (``from``, ``summary``; steps may be
    empty). ``findings`` are those of the data as it is under the new version, and the
    details carry the preview of what the renames would change."""
    entities = len(plan.revisions)
    errors = [f for f in findings if f.severity is Severity.ERROR]
    counts = Counter(f.code for f in errors)
    return GraphCliError(
        "admin.migration_required",
        f"the renames of {bump['to']} rewrite {entities} stored entities; add {MIGRATION_FILE}"
        f" (from: {bump['from']}, a summary, steps may be empty) next to the module files",
        details={
            **bump,
            "seq": seq,
            "counts": dict(sorted(counts.items())),
            "findings": [f.to_json() for f in errors[:MAX_FINDINGS]],
            "migrated": False,
            "file": MIGRATION_FILE,
            "preview": preview(None, plan, completing=False),
        },
    )


def incomplete(marker: MigrationMarker, pinned: str) -> GraphCliError:
    """``admin.migration_incomplete``: a migration is committed but not pinned, and this
    release is not the one that would finish it."""
    return GraphCliError(
        "admin.migration_incomplete",
        f"the migration {marker.from_version} -> {marker.to_version} is committed but not"
        f" pinned (the graph is pinned to {pinned}); finish it by releasing"
        f" {marker.to_version} again with the same {MIGRATION_FILE}, before any other release",
        details={
            "from": marker.from_version,
            "to": marker.to_version,
            "pinned": pinned,
            "artifact_hash": marker.artifact_hash,
            "migration_hash": marker.migration_hash,
        },
    )


def unused_steps(run: MigrationRun) -> tuple[WarningRecord, ...]:
    """``admin.migration_step_unused`` per step that changed no entity."""
    if run.plan is None or run.migration is None:
        return ()
    steps = run.migration.doc.steps
    return tuple(
        WarningRecord(
            "admin.migration_step_unused",
            f"steps[{index}] ({steps[index].NAME}) matches nothing in the data",
            {"index": index, "step": steps[index].NAME},
        )
        for index in run.plan.unused_steps
    )


def preview(
    migration: Migration | None, plan: MigrationPlan | None, *, completing: bool
) -> dict[str, JsonValue]:
    """The ``migration`` block of an answer (plan M9): the file's identity, and, when a plan
    was made, the counts per derived rename and per step (entities, edges, by type, sample
    ids), the findings that remain and the steps that matched nothing."""
    block: dict[str, JsonValue] = {
        "file": migration is not None,
        "migration_hash": None if migration is None else migration.hash,
        "summary": None if migration is None else migration.doc.summary,
        "completing": completing,
    }
    if plan is not None:
        block.update(plan.to_json())
    return block


def migrated(plan: MigrationPlan) -> dict[str, JsonValue]:
    """``migrated`` of an answer after the write: entities and edges changed, and the
    entities per rename and step."""
    by_step: dict[str, JsonValue] = {}
    for effect in plan.renames:
        owner = effect.what.get("class")
        label = f"{owner}.{effect.what['from']}" if owner else str(effect.what["from"])
        by_step[f"{effect.kind} {label}"] = effect.entities
    for effect in plan.steps:
        by_step[f"steps[{effect.index}].{effect.kind}"] = effect.entities
    return {"entities": len(plan.revisions), "edges": plan.edges_changed, "by_step": by_step}
