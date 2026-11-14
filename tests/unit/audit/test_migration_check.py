"""Migration txs against their intents, predecessors and pin (plan M4, M5)."""

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest
from support.audit_events import (
    ENV,
    REQUEST,
    allow,
    graph_entry,
    intent,
    maint_actor,
    sealed_lines,
)
from support.memlog import MemLog
from support.migrations import migration_record

from graph_cli.audit import events as builders
from graph_cli.audit import finding_codes as codes
from graph_cli.audit.events import TxIdentity
from graph_cli.audit.verify import (
    MaintEffects,
    check_migrations,
    cross_check,
    verify_graph,
)
from graph_cli.kernel.canonical import JsonValue, hash_value, parse_json
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ids import EntityId, EventId, TxId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditLine
from graph_cli.kernel.records import OntologyPin, Revision, TxRecord

Event = dict[str, JsonValue]


def other_pin(pin: OntologyPin, version: str) -> OntologyPin:
    return OntologyPin(pin.bundle, version, hash_value({"version": version}))


class Migrated:
    """Two write txs, then a migration of every entity to the model's own version (so the
    version checks hold), as the release builds it; ``pin`` appends the pin intent."""

    def __init__(self, *, entities: int = 3) -> None:
        self.log = MemLog("inventory")
        self.graph_id = self.log.graph_id
        self.drafts: list[AuditEventDraft] = []
        self.txs: list[TxRecord] = []
        self.entities = self.write(entities)
        self.write(0)  # an update of the first entity
        pin = self.log.builder.pin
        self.from_pin, self.to_pin = other_pin(pin, "0.9.0"), pin
        self.tx = self.migrate()

    def write(self, count: int) -> list[EntityId]:
        draft = self.log.draft()
        ids = [draft.create() for _ in range(count)]
        if not count:
            first = next(iter(self.txs[0].revisions)).id
            draft.update(first)
        tx = draft.build()
        before = {}
        for revision in tx.revisions:
            previous = self.log.at().current(revision.id)
            if previous is not None:
                before[revision.id] = previous.content_hash
        self.drafts.append(intent(tx, before=before))
        self.txs.append(self.log.commit(tx))
        return ids

    def migrate(self) -> TxRecord:
        draft = self.log.migration()
        for entity in self.entities:
            draft.revise(entity)
        to = self.log.builder.model.bundle_version
        record = migration_record(from_version="0.9.0", to_version=to)
        tx = draft.build(
            changeset={"schema": "graph_cli.migration/1", "ops": [], "migration": record}
        )
        self.authorized = tx
        self.drafts.append(self.migration_intent(tx))
        committed = self.log.commit(tx, migration=True)
        self.txs.append(committed)
        return committed

    def migration_intent(
        self, tx: TxIdentity, edit: Callable[[dict[str, JsonValue]], object] | None = None
    ) -> AuditEventDraft:
        change = builders.migration_change(
            tx,
            revisions_hash=builders.revisions_hash(tx.revisions),
            counts={"entities": len(tx.revisions), "edges": 0},
        )
        migration = change["migration"]
        assert isinstance(migration, dict)
        if edit is not None:
            edit(migration)
        return builders.maintenance(
            actor=maint_actor(),
            decisions=[allow("maint.migrate", f"graph:{self.graph_id}")],
            request=REQUEST,
            ts=self.log.builder.clock.now(),
            event_id=tx.event_id,
            env=ENV,
            graph=graph_entry(self.graph_id, self.from_pin),
            change=change,
        )

    def pin(self, version: str | None = None) -> None:
        new = self.to_pin if version is None else other_pin(self.to_pin, version)
        self.drafts.append(
            builders.maintenance(
                actor=maint_actor(),
                decisions=[allow("maint.ontology_pin", f"graph:{self.graph_id}")],
                request=REQUEST,
                ts=self.log.builder.clock.now(),
                event_id=EventId(self.log.builder.ids.new_id()),
                env=ENV,
                graph=graph_entry(self.graph_id, self.from_pin),
                change=builders.pin_change(self.from_pin, new),
            )
        )

    def lines(self) -> list[bytes]:
        return sealed_lines(self.drafts)

    def events(self) -> list[Event]:
        return [dict(as_object(parse_json(line), "e")) for line in self.lines()]

    def raw(self) -> list[AuditLine]:
        return [AuditLine(f"memory:{n}", line) for n, line in enumerate(self.lines())]

    def check(
        self,
        txs: Sequence[TxRecord] | None = None,
        marker: MigrationMarker | None = None,
        **flags: bool,
    ) -> list[Finding]:
        return check_migrations(
            self.graph_id,
            self.events(),
            self.txs if txs is None else txs,
            marker=marker,
            **flags,
        )

    def marker(self, tx: TxRecord | None = None) -> MigrationMarker:
        named = tx or self.tx
        return MigrationMarker(
            seq=named.seq,
            tx_id=TxId(named.tx_id),
            from_version="0.9.0",
            to_version=self.to_pin.version,
            artifact_hash=self.to_pin.artifact_hash,
            migration_hash="sha256:" + "b" * 64,
        )

    def with_revision(self, edit: Callable[[Revision], Revision]) -> list[TxRecord]:
        """The txs with the first revision of the migration tx edited."""
        first, *rest = self.tx.revisions
        changed = dataclasses.replace(self.tx, revisions=(edit(first), *rest))
        return [*self.txs[:-1], changed]


def codes_of(findings: Sequence[Finding]) -> list[str]:
    return sorted(f.code for f in findings)


def test_a_pinned_migration_is_clean() -> None:
    world = Migrated()
    world.pin()
    assert world.check() == []
    assert cross_check(world.graph_id, world.events(), world.txs) == []
    assert verify_graph(world.raw(), graph_id=world.graph_id, txs=world.txs).findings == ()


def test_a_graph_without_migrations_has_nothing_to_judge() -> None:
    world = Migrated()
    assert check_migrations(world.graph_id, world.events(), world.txs[:2]) == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("alias", "ZZ-0099"),
        ("record_status", "retracted"),
        ("created_at", datetime(2020, 1, 1, tzinfo=UTC)),
        ("valid_from", "2020-01-01"),
        ("valid_to", "2030-01-01"),
    ],
)
def test_a_changed_identity_field_is_reported_with_its_revision(name: str, value: object) -> None:
    world = Migrated()
    world.pin()
    txs = world.with_revision(lambda r: dataclasses.replace(r, **{name: value}))
    (finding,) = world.check(txs)
    assert finding.code == codes.MIGRATION_CHANGED_IDENTITY
    assert finding.severity is Severity.ERROR
    assert finding.target == {"graph_id": str(world.graph_id), "tx_seq": world.tx.seq}
    assert isinstance(finding.actual, list)
    assert finding.actual[0] == {
        "id": str(world.tx.revisions[0].id),
        "rev": world.tx.revisions[0].rev,
        "fields": [name],
    }


def test_a_revision_without_a_previous_one_is_the_same_finding() -> None:
    world = Migrated()
    world.pin()
    # the txs that created the entities are missing from the log the verifier walks
    (finding,) = world.check([world.tx])
    assert finding.code == codes.MIGRATION_CHANGED_IDENTITY
    assert isinstance(finding.actual, list)
    assert all(entry["fields"] == ["previous_revision"] for entry in finding.actual)  # type: ignore[index,call-overload]


def test_the_findings_of_one_tx_are_one_finding_listing_a_few_revisions() -> None:
    world = Migrated(entities=25)
    world.pin()
    txs = [
        *world.txs[:-1],
        dataclasses.replace(
            world.tx,
            revisions=tuple(dataclasses.replace(r, alias="ZZ-1") for r in world.tx.revisions),
        ),
    ]
    (finding,) = world.check(txs)
    assert "25 migrated revisions" in finding.message
    assert isinstance(finding.actual, list) and len(finding.actual) == 20


def test_a_revision_with_another_ontology_version_is_a_version_mismatch() -> None:
    world = Migrated()
    world.pin()
    txs = world.with_revision(lambda r: dataclasses.replace(r, ontology_version="0.9.0"))
    (finding,) = world.check(txs)
    assert finding.code == codes.MIGRATION_VERSION_MISMATCH
    assert finding.actual == {"revisions.ontology_version": ["0.9.0"]}


def test_a_tx_pin_that_is_not_the_target_is_a_version_mismatch() -> None:
    world = Migrated()
    world.pin()
    changed = dataclasses.replace(world.tx, ontology=world.from_pin)
    (finding,) = world.check([*world.txs[:-1], changed])
    assert finding.code == codes.MIGRATION_VERSION_MISMATCH
    assert finding.actual == {"tx.ontology.version": "0.9.0"}


@pytest.mark.parametrize("key", ["from", "to"])
def test_an_intent_naming_other_versions_than_the_changeset_is_a_version_mismatch(
    key: str,
) -> None:
    world = Migrated()
    world.drafts[-1] = world.migration_intent(world.authorized, lambda m: m.update({key: "7.7.7"}))
    world.pin()
    (finding,) = world.check()
    assert finding.code == codes.MIGRATION_VERSION_MISMATCH
    assert finding.actual == {f"intent.{key}": "7.7.7"}


def test_a_wrong_revisions_hash_or_entity_count_is_a_revisions_mismatch() -> None:
    for edit, key in (
        (lambda m: m.update(revisions_hash="sha256:" + "0" * 64), "revisions_hash"),
        (lambda m: m.update(counts={"entities": 2, "edges": 0}), "counts.entities"),
    ):
        world = Migrated()
        world.drafts[-1] = world.migration_intent(world.authorized, edit)
        world.pin()
        (finding,) = world.check()
        assert finding.code == codes.MIGRATION_REVISIONS_MISMATCH
        assert list(finding.actual) == [key]  # type: ignore[arg-type]


def test_a_changed_revision_changes_the_digest() -> None:
    world = Migrated()
    world.pin()
    txs = world.with_revision(lambda r: dataclasses.replace(r, content_hash="sha256:" + "1" * 64))
    assert codes_of(world.check(txs)) == [codes.MIGRATION_REVISIONS_MISMATCH]


def test_a_migration_without_a_pin_or_marker_is_unpinned() -> None:
    world = Migrated()
    (finding,) = world.check()
    assert (finding.code, finding.severity) == (codes.MIGRATION_UNPINNED, Severity.ERROR)


def test_a_migration_whose_marker_is_present_is_incomplete_not_unpinned() -> None:
    world = Migrated()
    (finding,) = world.check(marker=world.marker())
    assert (finding.code, finding.severity) == (codes.MIGRATION_INCOMPLETE, Severity.WARNING)


def test_the_marker_of_another_tx_does_not_excuse_the_migration() -> None:
    world = Migrated()
    assert codes_of(world.check(marker=world.marker(world.txs[0]))) == [codes.MIGRATION_UNPINNED]


def test_a_pin_to_another_version_does_not_count() -> None:
    world = Migrated()
    world.pin("9.9.9")
    assert codes_of(world.check()) == [codes.MIGRATION_UNPINNED]


def test_a_pin_before_the_migration_intent_does_not_count() -> None:
    world = Migrated()
    migration_intent = world.drafts.pop()
    world.pin()  # to the target version, but earlier in the stream
    world.drafts.append(migration_intent)
    assert codes_of(world.check()) == [codes.MIGRATION_UNPINNED]


def test_a_pin_after_an_incomplete_state_completes_it() -> None:
    world = Migrated()
    world.pin()
    assert world.check(marker=world.marker()) == []  # the pin switched, the marker is still there


def test_without_effects_the_pin_is_not_judged() -> None:
    world = Migrated()
    assert world.check(check_pin=False) == []
    report = verify_graph(world.raw(), graph_id=world.graph_id, txs=world.txs)
    assert report.findings == ()


def test_verify_graph_feeds_the_marker_from_the_effects() -> None:
    world = Migrated()
    effects = MaintEffects(manifest_pin=None, releases={}, migration_marker=world.marker())
    report = verify_graph(world.raw(), graph_id=world.graph_id, txs=world.txs, effects=effects)
    assert codes_of(report.findings) == [codes.MIGRATION_INCOMPLETE]
    report = verify_graph(
        world.raw(),
        graph_id=world.graph_id,
        txs=world.txs,
        effects=MaintEffects(manifest_pin=None, releases={}),
    )
    assert codes_of(report.findings) == [codes.MIGRATION_UNPINNED]


def test_a_tx_without_its_intent_gets_only_the_checks_that_need_none() -> None:
    world = Migrated()
    world.drafts.pop()  # the migration intent
    findings = world.check()
    assert findings == []  # no intent: tx_without_intent says it, the pin is not judged
    assert [f.code for f in cross_check(world.graph_id, world.events(), world.txs)] == [
        codes.TX_WITHOUT_INTENT
    ]
    txs = world.with_revision(lambda r: dataclasses.replace(r, alias="ZZ-1"))
    assert codes_of(world.check(txs)) == [codes.MIGRATION_CHANGED_IDENTITY]
