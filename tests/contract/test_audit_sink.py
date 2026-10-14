"""AuditSink contract: installation stream appends, reads and heads (contracts B5.1-B5.3, B5.7)."""

import pytest
from support.drafts import draft
from support.stores import StoreEnv

from graph_cli.kernel.canonical import GENESIS_HASH, jsonl_line
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditStream
from graph_cli.storage.common.chain import GENESIS, scan
from graph_cli.storage.common.faults import CrashPoint


def _installation(env: StoreEnv, count: int) -> list[EventId]:
    ids = [EventId(env.ids.new_id()) for _ in range(count)]
    for event_id in ids:
        env.audit.append_installation(
            draft(INSTALLATION_STREAM, event_id, env.clock.now(), status="denied")
        )
    return ids


def test_empty_streams(env: StoreEnv) -> None:
    assert env.audit.head(INSTALLATION_STREAM) == (0, GENESIS_HASH)
    assert list(env.audit.read(INSTALLATION_STREAM)) == []
    assert env.audit.head(env.stream) == (0, GENESIS_HASH)
    assert list(env.audit.read(AuditStream("graph:0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))) == []


def test_installation_appends_chain_and_read_back(env: StoreEnv) -> None:
    ids = _installation(env, 3)
    events = list(env.audit.read(INSTALLATION_STREAM))
    assert [e["event_id"] for e in events] == [str(i) for i in ids]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert all(e["stream"] == INSTALLATION_STREAM for e in events)
    result = scan(b"".join(jsonl_line(e) for e in events), GENESIS)
    assert result.corruption is None
    assert env.audit.head(INSTALLATION_STREAM) == (3, result.end.hash)
    assert [e["seq"] for e in env.audit.read(INSTALLATION_STREAM, from_seq=2)] == [2, 3]


def test_receipt_describes_the_appended_event(env: StoreEnv) -> None:
    event_id = EventId(env.ids.new_id())
    receipt = env.audit.append_installation(draft(INSTALLATION_STREAM, event_id, env.clock.now()))
    assert (receipt.stream, receipt.event_id, receipt.seq) == (INSTALLATION_STREAM, event_id, 1)
    assert env.audit.head(INSTALLATION_STREAM) == (1, receipt.hash)


def test_installation_refuses_graph_drafts(env: StoreEnv) -> None:
    with pytest.raises(GraphCliError) as info:
        env.audit.append_installation(draft(env.stream, EventId(env.ids.new_id()), env.clock.now()))
    assert info.value.code == "internal.error"
    assert env.audit.head(INSTALLATION_STREAM)[0] == 0


def test_failing_installation_append(env: StoreEnv) -> None:
    env.faults.armed = {CrashPoint.AUDIT_BEFORE_WRITE}
    with pytest.raises(GraphCliError) as info:
        _installation(env, 1)
    assert (info.value.code, info.value.retryable) == ("audit.unavailable", True)
    assert env.audit.head(INSTALLATION_STREAM)[0] == 0


def test_graph_and_installation_streams_are_separate(env: StoreEnv) -> None:
    _installation(env, 2)
    env.write(lambda d: d.create())
    assert env.audit.head(INSTALLATION_STREAM)[0] == 2
    assert env.audit.head(env.stream)[0] == 1
    (event,) = env.audit.read(env.stream)
    assert event["stream"] == env.stream
