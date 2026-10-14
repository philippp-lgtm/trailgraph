"""Write port contract (contracts B3.4, B3.5, B3.12, B4.2, B4.5, B5.7, B13.8; plan Q2, D3, D6)."""

import threading
from dataclasses import replace

import pytest
from support.drafts import draft, intent
from support.stores import StoreEnv, snapshot_state

from graph_cli.kernel.canonical import jsonl_line
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditEventDraft
from graph_cli.kernel.records import tx_to_json
from graph_cli.storage.common.chain import GENESIS, scan
from graph_cli.storage.common.faults import CrashPoint


def _audit_count(env: StoreEnv) -> int:
    return env.audit_head()[0]


def test_idempotency_lookup_scope(env: StoreEnv) -> None:
    tx, _ = env.write(lambda d: d.create(), idempotency_key="key-1", principal="test:1")
    with env.store.session(timeout_s=5) as session:
        found = session.lookup_idempotency("key-1", "test:1")
        assert found is not None and found.tx_id == tx.tx_id
        assert found.changeset_hash == tx.changeset_hash
        assert session.lookup_idempotency("key-1", "test:2") is None
        assert session.lookup_idempotency("key-2", "test:1") is None


def test_alias_counters_follow_allocations(env: StoreEnv) -> None:
    cls = next(c for c in env.builder.concrete_classes if env.builder.prefix_of(c))
    prefix = env.builder.prefix_of(cls)
    assert prefix is not None
    with env.store.session(timeout_s=5) as session:
        assert session.alias_counters() == {}
    env.write(lambda d: [d.create(cls), d.create(cls)])
    env.write(lambda d: d.create(cls, alias=f"{prefix}-0010"))
    with env.store.session(timeout_s=5) as session:
        assert session.alias_counters()[prefix] == 10


def test_append_audit_seals_the_graph_stream(env: StoreEnv) -> None:
    with env.store.session(timeout_s=5) as session:
        tx_ids = [EventId(env.ids.new_id()) for _ in range(2)]
        receipts = [session.append_audit(draft(env.stream, e, env.clock.now())) for e in tx_ids]
    assert [r.seq for r in receipts] == [1, 2]
    assert [r.event_id for r in receipts] == tx_ids
    events = list(env.audit_events())
    assert [e["seq"] for e in events] == [1, 2]
    assert all(e["stream"] == env.stream for e in events)
    assert [e["hash"] for e in events] == [r.hash for r in receipts]
    assert env.audit_head() == (2, receipts[1].hash)
    lines = b"".join(jsonl_line(e) for e in events)
    assert scan(lines, GENESIS).end.seq == 2


@pytest.mark.parametrize(
    "body_change",
    [
        {"event_id": None},
        {"event_id": "not-a-uuid"},
        {"seq": 1},
        {"hash": "sha256:x"},
        {"prev_hash": "sha256:x"},
        {"stream": "installation"},
    ],
)
def test_append_audit_refuses_bad_drafts(env: StoreEnv, body_change: dict[str, object]) -> None:
    good = draft(env.stream, EventId(env.ids.new_id()), env.clock.now())
    body = {**good.body, **body_change}
    body = {k: v for k, v in body.items() if v is not None}
    with env.store.session(timeout_s=5) as session:
        with pytest.raises(GraphCliError) as info:
            session.append_audit(AuditEventDraft(env.stream, body))  # type: ignore[arg-type]
        assert info.value.code == "internal.error"
        with pytest.raises(GraphCliError):
            session.append_audit(replace(good, stream=INSTALLATION_STREAM))
    assert _audit_count(env) == 0


def test_commit_preconditions_leave_the_store_unchanged(env: StoreEnv) -> None:
    env.write(lambda d: d.create())
    head = env.store.head()
    with env.store.session(timeout_s=5) as session:
        built = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        built.create()
        tx = replace(built.build(), seq=5)
        receipt = session.append_audit(intent(tx))
        with pytest.raises(GraphCliError) as info:
            session.commit(tx, receipt)
        assert info.value.code == "internal.error"
        assert info.value.details["rule"] == "seq"
    assert env.store.head() == head
    assert len(env.store.changes_since(0)) == 1


def test_receipts_are_bound_to_session_and_tx(env: StoreEnv) -> None:
    with env.store.session(timeout_s=5) as session:
        built = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        built.create()
        tx = built.build()
        receipt = session.append_audit(intent(tx))
        session.commit(tx, receipt)
        second = env.builder.begin(
            snapshot_state(session.snapshot()), session.head(), session.alias_counters()
        )
        second.create()
        tx2 = replace(second.build(), event_id=tx.event_id)
        with pytest.raises(GraphCliError) as info:
            session.commit(tx2, receipt)
        assert info.value.details["rule"] == "receipt_reused"
    with env.store.session(timeout_s=5) as other:
        third = env.builder.begin(
            snapshot_state(other.snapshot()), other.head(), other.alias_counters()
        )
        third.create()
        tx3 = replace(third.build(), event_id=tx.event_id)
        with pytest.raises(GraphCliError) as info:
            other.commit(tx3, receipt)
        assert info.value.details["rule"] == "receipt_session"
    assert env.store.head().seq == 1


AUDIT_POINTS = [
    CrashPoint.AUDIT_BEFORE_WRITE,
    CrashPoint.AUDIT_AFTER_WRITE_BEFORE_SYNC,
    CrashPoint.AUDIT_AFTER_SYNC,
]
TX_POINTS = [CrashPoint.TX_BEFORE_WRITE, CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC]


@pytest.mark.parametrize("point", AUDIT_POINTS)
def test_failing_audit_append_refuses_and_changes_nothing(env: StoreEnv, point: CrashPoint) -> None:
    """B3.4 (store side): no receipt, no tx; an unacknowledged event is rolled back."""
    env.write(lambda d: d.create())
    head, audit_head = env.store.head(), env.audit_head()
    env.faults.armed = {point}
    with pytest.raises(GraphCliError) as info:
        env.write(lambda d: d.create())
    assert (info.value.code, info.value.retryable) == ("audit.unavailable", True)
    assert env.store.head() == head
    assert env.audit_head() == audit_head
    env.faults.armed = set()
    env.write(lambda d: d.create())
    assert env.store.head().seq == 2


@pytest.mark.parametrize("point", TX_POINTS)
def test_failing_tx_append_leaves_an_orphan_intent(env: StoreEnv, point: CrashPoint) -> None:
    head = env.store.head()
    env.faults.armed = {point}
    with pytest.raises(GraphCliError) as info:
        env.write(lambda d: d.create())
    assert (info.value.code, info.value.retryable) == ("backend.unavailable", True)
    assert env.store.head() == head
    assert _audit_count(env) == 1  # benign orphan intent (section 4, W.13)
    env.faults.armed = set()
    env.write(lambda d: d.create())
    assert env.store.head().seq == 1
    assert _audit_count(env) == 2


def test_fault_after_the_commit_point_is_a_warning(env: StoreEnv) -> None:
    """B3.5: once the tx is durable the write succeeds, with a warning."""
    env.faults.armed = {CrashPoint.TX_AFTER_SYNC}
    tx, result = env.write(lambda d: d.create())
    assert [w.code for w in result.warnings] == ["storage.materialization_deferred"]
    assert env.store.head().seq == 1
    assert env.store.snapshot().get(tx.revisions[0].id) == tx.revisions[0]


def test_lock_timeout(env: StoreEnv) -> None:
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with env.store.session(timeout_s=5):
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(10)
        with pytest.raises(GraphCliError) as info, env.store.session(timeout_s=0.05):
            pass
        assert (info.value.code, info.value.retryable) == ("backend.lock_timeout", True)
    finally:
        release.set()
        holder.join()
    with env.store.session(timeout_s=0.05):
        pass


def test_session_is_unusable_after_exit(env: StoreEnv) -> None:
    with env.store.session(timeout_s=5) as session:
        pass
    with pytest.raises(GraphCliError) as info:
        session.head()
    assert info.value.code == "internal.error"


def test_seq_contiguity_and_chain_after_500_random_ops(env: StoreEnv) -> None:
    """B4.2, B4.5 on both fixtures."""
    env.random(500)
    records = env.store.changes_since(0, limit=10**6)
    assert [r.seq for r in records] == list(range(1, 501))
    lines = b"".join(jsonl_line(tx_to_json(r)) for r in records)
    result = scan(lines, GENESIS)
    assert (result.end.seq, result.corruption, result.tail) == (500, None, "clean")
    assert result.end.hash == env.store.head().hash
    events = b"".join(jsonl_line(e) for e in env.audit_events())
    assert scan(events, GENESIS).end.seq == 500
    seen: set[EntityId] = set()
    for record in records:
        for revision in record.revisions:
            seen.add(revision.id)
    assert env.store.snapshot().get_many(sorted(seen)).keys() == seen
