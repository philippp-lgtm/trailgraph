"""``build_migration_tx``: the changeset, its hash, the blobs and the stamp of a migration tx
(migration plan M4), and that the store accepts what it builds."""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from support.drafts import intent
from support.migrations import allow_migrate
from support.modules import compile_modules, header
from support.snapshots import AT, edge, eid, revision
from support.txbuilder import actor

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId, GraphId, TxId
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.records import OntologyPin, RequestMeta, Revision, revision_content_hash
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.migration import MigrationDoc, migration_hash, parse_migration
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.migration import build_migration_tx, migration_record
from graph_cli.storage.memory.store import InMemoryStore
from graph_cli.temporal_knowledge.migrate import MigrationPlan, MigrationStamp, plan_migration

GRAPH = GraphId(UUID("0192f000-0000-7000-8000-0000000000a1"))
HASH = "sha256:" + "ab" * 32
OTHER_HASH = "sha256:" + "cd" * 32
STAMP_TX = TxId(UUID("0192f000-0000-7000-8000-0000000000b2"))
STAMP_EVENT = EventId(UUID("0192f000-0000-7000-8000-0000000000b3"))
RECORDED = AT + timedelta(minutes=5)
PIN = OntologyPin("example", "2.0.0", "sha256:" + "ef" * 32)
MIGRATION = """\
format: graph_cli.migration/1
from: 1.0.0
summary: Move the example classes to area_a.
steps: []
"""

OLD = """classes:
  Note:
    summary: A note.
    attributes:
      title: {summary: Title., type: string}
  Scan:
    summary: A stored scan.
    is_a: files:File
"""
NEW_MAIN = """classes:
  Note:
    summary: A note.
    attributes:
      title: {summary: Title., type: string}
"""
AREA = (
    header("area_a", "aa", "1.0.0", ("files",))
    + """classes:
  Scan:
    summary: A stored scan.
    is_a: files:File
    renamed_from: ex:Scan
"""
)


def models() -> tuple[CompiledModel, CompiledModel]:
    old = compile_modules(
        "example", {"example": header("example", "ex", "1.0.0", ("files",)) + OLD}
    )
    new = compile_modules(
        "example",
        {
            "example": header("example", "ex", "2.0.0", ("area_a", "files")) + NEW_MAIN,
            "area_a": AREA,
        },
    )
    return old, new


def seeded_store() -> InMemoryStore:
    """Two scans and a note, committed in one tx (seq 1)."""
    store = InMemoryStore(GRAPH)
    base = [
        revision("e1", "ex:Scan", {"title": "A", "content": HASH, "media_type": "text/plain"}),
        revision(
            "e2", "ex:Scan", {"title": "B", "content": OTHER_HASH, "media_type": "text/plain"}
        ),
        revision("e3", "ex:Note", {"title": "N"}, [edge("ex:related", "e1", name="n>s")]),
    ]
    tx_id = TxId(UUID("0192f000-0000-7000-8000-0000000000c1"))
    event_id = EventId(UUID("0192f000-0000-7000-8000-0000000000c2"))
    revisions = []
    for item in base:
        stamped = replace(item, recorded_at=AT, created_at=AT, tx_id=tx_id, tx_seq=1)
        revisions.append(replace(stamped, content_hash=revision_content_hash(stamped)))
    changeset = {"schema": "graph_cli.changeset/1", "ops": []}
    tx = AuthorizedTx(
        graph_id=GRAPH,
        tx_id=tx_id,
        event_id=event_id,
        seq=1,
        recorded_at=AT,
        actor=actor(),
        decisions=(),
        changeset=changeset,
        changeset_hash=hash_value(changeset),
        resolution={"locals": {}, "refs": {}},
        revisions=tuple(revisions),
        alias_allocations=(),
        ontology=OntologyPin("example", "1.0.0", "sha256:" + "01" * 32),
        reason=None,
        idempotency_key=None,
        request=RequestMeta("apply", hash_value({})),
        blobs=tuple(sorted({HASH, OTHER_HASH})),
    )
    with store.session(timeout_s=1) as session:
        for digest in (HASH, OTHER_HASH):
            staged = store.stage_blob([digest.encode()], max_bytes=100)
            staged.hash = digest
            session.publish_blob(staged)
        session.commit(tx, session.append_audit(intent(tx)))
    return store


def doc() -> MigrationDoc:
    parsed = parse_migration(MIGRATION)
    assert parsed.doc is not None
    return parsed.doc


def planned(store: InMemoryStore) -> tuple[MigrationPlan, CompiledModel]:
    old, new = models()
    stamp = MigrationStamp(STAMP_TX, store.head().seq + 1, RECORDED, "2.0.0")
    plan = plan_migration(old, new, derive_renames(old, new), doc(), store.snapshot(), stamp=stamp)
    return plan, new


def built(store: InMemoryStore, **changes: object) -> tuple[AuthorizedTx, MigrationPlan]:
    plan, new = planned(store)
    old, _ = models()
    record = migration_record(
        from_version="1.0.0",
        to_version="2.0.0",
        renames=derive_renames(old, new),
        doc=doc(),
    )
    tx = build_migration_tx(
        plan,
        new,
        graph_id=GRAPH,
        tx_id=STAMP_TX,
        event_id=STAMP_EVENT,
        seq=store.head().seq + 1,
        recorded_at=RECORDED,
        actor=actor("maint:1"),
        decisions=allow_migrate(GRAPH),
        ontology=PIN,
        reason="move the scans",
        request=RequestMeta("ontology release", hash_value({"version": "2.0.0"})),
        record=record,
    )
    return replace(tx, **changes), plan  # type: ignore[arg-type]


def test_the_changeset_is_exactly_schema_ops_and_the_migration_record() -> None:
    tx, _ = built(seeded_store())
    assert set(tx.changeset) == {"schema", "ops", "migration"}
    assert tx.changeset["schema"] == "graph_cli.migration/1"
    assert tx.changeset["ops"] == []
    record = tx.changeset["migration"]
    assert isinstance(record, dict)
    assert set(record) == {"from", "to", "migration_hash", "summary", "renames", "steps"}
    assert (record["from"], record["to"]) == ("1.0.0", "2.0.0")
    assert record["migration_hash"] == migration_hash(doc())
    assert record["summary"] == "Move the example classes to area_a."
    assert record["steps"] == []
    old, new = models()
    assert record["renames"] == derive_renames(old, new).to_json()
    assert record["renames"]["classes"] == {"ex:Scan": "aa:Scan"}  # type: ignore[index]


def test_the_record_without_a_migration_file_is_total_and_null_free() -> None:
    old, new = models()
    record = migration_record(
        from_version="1.0.0", to_version="2.0.0", renames=derive_renames(old, new), doc=None
    )
    assert set(record) == {"from", "to", "renames", "steps"}
    assert record["steps"] == []
    assert None not in record.values()


def test_the_tx_fields_hash_and_empty_resolution() -> None:
    tx, plan = built(seeded_store())
    assert tx.changeset_hash == hash_value(tx.changeset)
    assert tx.resolution == {"locals": {}, "refs": {}}
    assert tx.alias_allocations == () and tx.idempotency_key is None
    assert tx.ontology == PIN and tx.reason == "move the scans"
    assert tx.allowed_actions() == {"maint.migrate"}
    assert (tx.graph_id, tx.tx_id, tx.event_id, tx.seq) == (GRAPH, STAMP_TX, STAMP_EVENT, 2)
    assert [r.id for r in tx.revisions] == list(plan.revisions) == [eid("e1"), eid("e2")]


def test_the_stamp_of_the_plan_is_the_stamp_of_the_tx() -> None:
    tx, _ = built(seeded_store())
    for revision_ in tx.revisions:
        assert (revision_.tx_id, revision_.tx_seq) == (tx.tx_id, tx.seq)
        assert revision_.recorded_at == tx.recorded_at
        assert revision_.ontology_version == tx.ontology.version == "2.0.0"


def test_the_blobs_of_a_moved_file_subclass_are_listed_sorted_and_unique() -> None:
    tx, plan = built(seeded_store())
    assert {r.type for r in tx.revisions} == {"aa:Scan"}
    assert tx.blobs == tuple(sorted({HASH, OTHER_HASH})) == (HASH, OTHER_HASH)
    assert tx.blobs == plan.blobs(models()[1])


def test_the_store_migrates_with_what_the_builder_made() -> None:
    store = seeded_store()
    tx, _ = built(store)
    with store.session(timeout_s=1) as session:
        receipt = session.append_audit(intent(tx))
        with pytest.raises(GraphCliError) as refused:
            session.commit(tx, receipt)  # an ordinary commit refuses a migration tx
        assert refused.value.details["rule"] == "migration_via_commit"
        result = session.migrate(tx, receipt)
    assert result.seq == 2
    snapshot = store.snapshot()
    assert snapshot.get(eid("e1")) == tx.revisions[0]
    assert snapshot.get(eid("e1")).type == "aa:Scan"  # type: ignore[union-attr]
    assert snapshot.get(eid("e3")) is not None and snapshot.get(eid("e3")).rev == 1  # type: ignore[union-attr]
    stored = snapshot.tx(2)
    assert stored is not None and stored.changeset == tx.changeset
    marker = MigrationMarker(
        2, tx.tx_id, "1.0.0", "2.0.0", PIN.artifact_hash, migration_hash(doc())
    )
    assert marker_committed(snapshot, marker)


def test_a_changed_stamp_is_refused_by_the_store() -> None:
    store = seeded_store()
    tx, _ = built(store, recorded_at=RECORDED + timedelta(seconds=1))
    with store.session(timeout_s=1) as session:
        receipt = session.append_audit(intent(tx))
        with pytest.raises(GraphCliError) as refused:
            session.migrate(tx, receipt)
    assert refused.value.details["rule"] == "revision_recorded_at"


def test_a_revision_of_another_entity_is_refused() -> None:
    store = seeded_store()
    tx, _ = built(store)
    stray: Revision = replace(tx.revisions[0], id=eid("new"), rev=1, prev_rev=None)
    stray = replace(stray, created_at=tx.recorded_at, alias=None)
    stray = replace(stray, content_hash=revision_content_hash(stray))
    with store.session(timeout_s=1) as session:
        receipt = session.append_audit(intent(replace(tx, revisions=(stray,))))
        with pytest.raises(GraphCliError) as refused:
            session.migrate(replace(tx, revisions=(stray,)), receipt)
    assert refused.value.details["rule"] == "migration_new_entity"
