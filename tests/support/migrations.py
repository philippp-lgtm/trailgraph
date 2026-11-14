"""Test-only builder of migration txs for the storage tests (plan M4).

Like ``support.txbuilder`` this is a deliberate double of the service: storage tests must
not depend on ``services.migration``. A draft revises current revisions by field (``type``,
``attrs``, ``edges``, ...) and builds the tx with the migration changeset shape and an allow
decision for ``maint.migrate``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from graph_cli.kernel.actions import Action
from graph_cli.kernel.authz import AuthorizedTx, Decision
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TxId
from graph_cli.kernel.ports.storage import Head
from graph_cli.kernel.records import RequestMeta, Revision, revision_content_hash
from support.txbuilder import State, TxBuilder, actor

POLICY = hash_value({"policy": "test"})


def allow_migrate(graph_id: GraphId) -> tuple[Decision, ...]:
    return (Decision("allow", Action("maint.migrate"), f"graph:{graph_id}", "test.allow", POLICY),)


def migration_record(
    summary: str = "example migration", *, from_version: str = "1.0.0", to_version: str = "2.0.0"
) -> dict[str, JsonValue]:
    return {
        "from": from_version,
        "to": to_version,
        "migration_hash": hash_value({"summary": summary}),
        "summary": summary,
        "renames": {"classes": {}, "relations": {}, "attributes": []},
        "steps": [],
    }


@dataclass
class MigrationDraft:
    builder: TxBuilder
    state: State
    head: Head
    changes: dict[EntityId, dict[str, object]] = field(default_factory=dict)

    def revise(self, entity: EntityId, **changes: object) -> None:
        """Give ``entity`` a next revision with ``changes`` applied to its current one."""
        self.changes.setdefault(entity, {}).update(changes)

    def build(
        self,
        *,
        decisions: tuple[Decision, ...] | None = None,
        changeset: Mapping[str, JsonValue] | None = None,
    ) -> AuthorizedTx:
        builder = self.builder
        recorded_at = builder.clock.now()
        if self.head.recorded_at is not None:
            recorded_at = max(recorded_at, self.head.recorded_at)
        tx_id, event_id = TxId(builder.ids.new_id()), EventId(builder.ids.new_id())
        seq = self.head.seq + 1
        revisions = tuple(
            self._revision(entity, changes, tx_id, seq, recorded_at)
            for entity, changes in sorted(self.changes.items())
        )
        body: dict[str, JsonValue] = (
            dict(changeset)
            if changeset is not None
            else {
                "schema": "graph_cli.migration/1",
                "ops": [],
                "migration": migration_record(),
            }
        )
        return AuthorizedTx(
            graph_id=builder.graph_id,
            tx_id=tx_id,
            event_id=event_id,
            seq=seq,
            recorded_at=recorded_at,
            actor=actor("maint:1"),
            decisions=allow_migrate(builder.graph_id) if decisions is None else decisions,
            changeset=body,
            changeset_hash=hash_value(body),
            resolution={"locals": {}, "refs": {}},
            revisions=revisions,
            alias_allocations=(),
            ontology=builder.pin,
            reason="example release",
            idempotency_key=None,
            request=RequestMeta("ontology release", hash_value({"release": "2.0.0"})),
        )

    def _revision(
        self,
        entity: EntityId,
        changes: Mapping[str, object],
        tx_id: TxId,
        seq: int,
        recorded_at: datetime,
    ) -> Revision:
        previous = self.state.get(entity)
        assert previous is not None, f"unknown entity {entity}"
        fields: dict[str, object] = {
            "rev": previous.rev + 1,
            "prev_rev": previous.rev,
            "recorded_at": recorded_at,
            "tx_id": tx_id,
            "tx_seq": seq,
            "ontology_version": self.builder.model.bundle_version,
            **changes,
        }
        revision = replace(previous, **fields)  # type: ignore[arg-type]
        return replace(revision, content_hash=revision_content_hash(revision))


def begin_migration(builder: TxBuilder, state: State, head: Head) -> MigrationDraft:
    return MigrationDraft(builder, state, head)
