"""T: hand-edited files and recovery (contracts B4.11, B5.9, K.b, section 4.2).

kill -9 cannot tear a line, so torn tails are made here by truncating files
mid-line (K.b).
"""

import base64
from collections.abc import Mapping
from pathlib import Path

import pytest
from support.drafts import intent
from support.stores import ALL_STATUSES, StoreEnv, snapshot_state

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.segments import SegmentedLog
from graph_cli.storage.files.store import FileStore


def setup(
    tmp_path: Path, txs: int = 4
) -> tuple[StoreEnv, FileStore, list[Mapping[str, JsonValue]]]:
    env = StoreEnv("files", "inventory", tmp_path)
    env.random(txs)
    assert isinstance(env.store, FileStore)
    notes: list[Mapping[str, JsonValue]] = []
    store = FileStore(
        env.store.layout.root,
        env.graph_id,
        syncer=env.syncer,
        faults=env.faults,
        oplog=notes.append,
    )
    env.store = store
    return env, store, notes


def last_segment(log: SegmentedLog) -> Path:
    return log.path(log.segments()[-1][1])


def cut_last_line(path: Path, keep: int = 20) -> bytes:
    """Truncate the last line to its first ``keep`` bytes; return the removed bytes' prefix."""
    data = path.read_bytes()
    start = data.rstrip(b"\n").rfind(b"\n") + 1
    path.write_bytes(data[: start + keep])
    return data[start : start + keep]


@pytest.mark.parametrize("log_name", ["txlog", "auditlog"])
def test_torn_tail_is_ignored_by_readers_and_truncated_by_the_writer(
    tmp_path: Path, log_name: str
) -> None:
    env, store, notes = setup(tmp_path)
    log: SegmentedLog = getattr(store, log_name)
    torn = cut_last_line(last_segment(log))
    reader = FileStore(store.layout.root, env.graph_id)
    snapshot = reader.snapshot()
    expected_seq = 3 if log_name == "txlog" else 4
    assert snapshot.seq == expected_seq
    assert snapshot.warnings == () or log_name == "txlog"
    with store.session(timeout_s=5) as session:
        assert session.recovery.recovered
    assert log.scan().tail == "clean"
    (note,) = notes
    (step,) = [s for s in note["steps"] if s["event"] == "tail_truncated"]  # type: ignore[union-attr]
    assert base64.b64decode(str(step["removed_b64"])) == torn  # type: ignore[index]
    env.random(1)
    assert env.store.head().seq == expected_seq + 1


def test_torn_tx_tail_of_the_checkpointed_tx_warns_readers(tmp_path: Path) -> None:
    """Cutting a tx that head.json already covers makes the checkpoint inconsistent."""
    env, store, _ = setup(tmp_path)
    cut_last_line(last_segment(store.txlog))
    snapshot = FileStore(store.layout.root, env.graph_id).snapshot()
    assert snapshot.seq == 3
    assert [w.code for w in snapshot.warnings] == ["storage.recovery_pending"]


def damage_interior(path: Path) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    lines[1] = lines[1].replace(b'"seq":2', b'"seq":2 ', 1)
    path.write_bytes(b"".join(lines))


def test_interior_damage_behind_the_checkpoint(tmp_path: Path) -> None:
    """Writers replay the whole log and refuse; lock-free point reads continue, scans
    (which replay the log) report the damage (plan D9)."""
    env, store, _ = setup(tmp_path)
    created = store.snapshot().query(Query(record_status=ALL_STATUSES, limit=100)).items
    damage_interior(last_segment(store.txlog))
    fresh = FileStore(store.layout.root, env.graph_id)  # a new process: no cached replay
    with pytest.raises(GraphCliError) as info, fresh.session(timeout_s=5):
        pass
    assert info.value.code == "backend.corrupt"
    assert info.value.details["expected_seq"] == 2
    reader = FileStore(store.layout.root, env.graph_id).snapshot()
    assert reader.seq == 4
    for revision in created:
        assert reader.get(revision.id) == revision
    with pytest.raises(GraphCliError) as info:
        reader.query(Query())
    assert info.value.code == "backend.corrupt"


def test_interior_damage_in_the_read_range_warns_readers(tmp_path: Path) -> None:
    """B4.11: without a checkpoint the reader scans the damage and stops before it."""
    env, store, _ = setup(tmp_path)
    damage_interior(last_segment(store.txlog))
    store.layout.head.unlink()
    reader = FileStore(store.layout.root, env.graph_id)
    snapshot = reader.snapshot()
    assert snapshot.seq == 1
    assert [w.code for w in snapshot.warnings] == ["storage.recovery_pending"]
    assert snapshot.query(Query(record_status=ALL_STATUSES)).total >= 1
    assert reader.head().seq == 1
    with pytest.raises(GraphCliError) as info, store.session(timeout_s=5):
        pass
    assert info.value.code == "backend.corrupt"


def test_invalid_interior_audit_line_refuses_writes(tmp_path: Path) -> None:
    """Writers read the audit stream from the checkpoint on; without one, all of it (D9)."""
    env, store, _ = setup(tmp_path)
    damage_interior(last_segment(store.auditlog))
    store.layout.head.unlink()
    with pytest.raises(GraphCliError) as info, store.session(timeout_s=5):
        pass
    assert info.value.code == "backend.corrupt"
    assert FileStore(store.layout.root, env.graph_id).snapshot().seq == 4


def test_existing_revision_file_with_wrong_content_is_corrupt(tmp_path: Path) -> None:
    """C (section 4.1 K3): roll-forward never overwrites a mismatching revision file."""
    env, store, _ = setup(tmp_path)
    env.faults.armed = {CrashPoint.TX_AFTER_SYNC}
    tx, result = env.write(lambda d: d.create())
    env.faults.armed = set()
    assert result.warnings
    (revision,) = tx.revisions
    path = store.layout.revision_path(revision.id, revision.rev)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}\n")
    with pytest.raises(GraphCliError) as info, store.session(timeout_s=5):
        pass
    assert info.value.code == "backend.corrupt"
    assert path.read_bytes() == b"{}\n"


def test_session_recovery_is_quiet_on_a_clean_graph(tmp_path: Path) -> None:
    _, store, notes = setup(tmp_path)
    with store.session(timeout_s=5) as session:
        assert not session.recovery.recovered
    assert notes == []


def test_missing_head_is_rewritten_by_recovery(tmp_path: Path) -> None:
    env, store, notes = setup(tmp_path)
    before = store.layout.head.read_bytes()
    store.layout.head.unlink()
    with store.session(timeout_s=5) as session:
        steps = [s["event"] for s in session.recovery.details["steps"]]  # type: ignore[union-attr]
    assert steps == ["head_rewritten"]
    assert store.layout.head.read_bytes() == before
    assert len(notes) == 1
    with store.session(timeout_s=5) as session:
        tx_head = session.head()
    assert tx_head == env.store.head()


def test_failed_append_rollback_keeps_the_log_clean(tmp_path: Path) -> None:
    env, store, _ = setup(tmp_path)
    size = last_segment(store.txlog).stat().st_size
    env.faults.armed = {CrashPoint.TX_AFTER_WRITE_BEFORE_SYNC}
    with pytest.raises(GraphCliError):
        env.write(lambda d: d.create())
    env.faults.armed = set()
    assert last_segment(store.txlog).stat().st_size == size
    with store.session(timeout_s=5) as session:
        assert not session.recovery.recovered
        assert session.head().seq == 4
        draft = env.builder.begin(
            snapshot_state(session.snapshot()),
            session.head(),
            session.alias_counters(),
        )
        draft.create()
        built = draft.build()
        session.commit(built, session.append_audit(intent(built)))
    assert store.head().seq == 5
