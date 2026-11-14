"""The migration tx: what a release with a data migration commits (plan M3, M4).

``plan_migration`` computes the new revisions; this module wraps them into the
``AuthorizedTx`` that ``MaintenanceSession.migrate`` commits. The release service
(``graph_cli_admin``) supplies the identity of the tx (ids, seq, time, actor, decisions); the
stamp it gave ``plan_migration`` must equal those fields, which ``check_tx`` verifies.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthorizedTx, Decision
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.changeset import MIGRATION_CHANGESET_SCHEMA
from graph_cli.kernel.ids import EventId, GraphId, TxId
from graph_cli.kernel.records import OntologyPin, RequestMeta
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.migration import MigrationDoc, migration_hash
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.migrate import MigrationPlan


def migration_record(
    *, from_version: str, to_version: str, renames: Renames, doc: MigrationDoc | None
) -> dict[str, JsonValue]:
    """The ``migration`` object of the changeset: the release's complete record, with the
    derived renames, so the tx explains itself without the release directory.

    Without a migration file (the builder stays total) ``migration_hash`` and ``summary`` are
    left out and ``steps`` is empty."""
    record: dict[str, JsonValue] = {"from": from_version, "to": to_version}
    if doc is not None:
        record["migration_hash"] = migration_hash(doc)
        record["summary"] = doc.summary
    record["renames"] = renames.to_json()
    record["steps"] = [step.to_json() for step in doc.steps] if doc is not None else []
    return record


def build_migration_tx(
    plan: MigrationPlan,
    new_model: CompiledModel,
    *,
    graph_id: GraphId,
    tx_id: TxId,
    event_id: EventId,
    seq: int,
    recorded_at: datetime,
    actor: ActorContext,
    decisions: tuple[Decision, ...],
    ontology: OntologyPin,
    reason: str | None,
    request: RequestMeta,
    record: Mapping[str, JsonValue],
) -> AuthorizedTx:
    """The tx of ``plan``: its revisions (in plan order), no ops, the ``migration`` record.

    ``decisions`` must allow ``maint.migrate``; ``ontology`` is the target pin; the blobs are
    listed under ``new_model`` so migrated ``files:File`` subclasses keep their content
    listed. No aliases are allocated and there is no idempotency key."""
    changeset: dict[str, JsonValue] = {
        "schema": MIGRATION_CHANGESET_SCHEMA,
        "ops": [],
        "migration": dict(record),
    }
    return AuthorizedTx(
        graph_id=graph_id,
        tx_id=tx_id,
        event_id=event_id,
        seq=seq,
        recorded_at=recorded_at,
        actor=actor,
        decisions=decisions,
        changeset=changeset,
        changeset_hash=hash_value(changeset),
        resolution={"locals": {}, "refs": {}},
        revisions=tuple(plan.revisions.values()),
        alias_allocations=(),
        ontology=ontology,
        reason=reason,
        idempotency_key=None,
        request=request,
        blobs=plan.blobs(new_model),
    )
