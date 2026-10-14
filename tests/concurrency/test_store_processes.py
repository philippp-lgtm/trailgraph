"""P: concurrent FileStore writers and readers in real processes.

Contracts B1.8, B4.2, B4.10, B13.8.
"""

from pathlib import Path
from typing import Any

import pytest
from support.procs import release, result, spawn
from support.stores import StoreEnv

from graph_cli.storage.common.chain import GENESIS, scan
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow


def graph(tmp_path: Path) -> tuple[StoreEnv, FileStore]:
    env = StoreEnv("files", "inventory", tmp_path)
    assert isinstance(env.store, FileStore)
    return env, env.store


def common(env: StoreEnv, store: FileStore, barrier: Path) -> dict[str, Any]:
    return {
        "graph_dir": str(store.layout.root),
        "graph_id": str(env.graph_id),
        "ontology": env.ontology,
        "barrier": str(barrier),
    }


def writers(
    env: StoreEnv, store: FileStore, barrier: Path, *, count: int, each: int, kind: str
) -> list[Any]:
    base = common(env, store, barrier)
    return [
        spawn("store", {**base, "mode": "writer", "worker": n, "count": each, "kind": kind})
        for n in range(count)
    ]


def assert_chain(store: FileStore, expected: int) -> None:
    data = b"".join(d for _, d in store.txlog.read_all())
    tx_scan = scan(data, GENESIS)
    assert (tx_scan.end.seq, tx_scan.corruption, tx_scan.tail) == (expected, None, "clean")
    events = b"".join(d for _, d in store.auditlog.read_all())
    audit_scan = scan(events, GENESIS)
    assert (audit_scan.end.seq, audit_scan.corruption) == (expected, None)


def test_no_lost_writes(tmp_path: Path) -> None:
    """P2: 6 processes x 30 commits; seq 1..180 contiguous, chains valid, every tx present."""
    env, store = graph(tmp_path)
    barrier = tmp_path / "go"
    procs = writers(env, store, barrier, count=6, each=30, kind="random")
    release(barrier)
    outputs = [result(p, timeout=120) for p in procs]
    records = store.changes_since(0, limit=10**6)
    assert [r.seq for r in records] == list(range(1, 181))
    assert_chain(store, 180)
    committed = {str(r.tx_id) for r in records}
    for output in outputs:
        assert len(output["tx_ids"]) == 30
        assert set(output["tx_ids"]) <= committed
    with store.session(timeout_s=5) as session:
        assert not session.recovery.recovered
        assert session.head().seq == 180


def test_aliases_are_unique_across_processes(tmp_path: Path) -> None:
    """P3, B1.8: 8 processes x 25 creates allocate 200 unique aliases; the counter is 200."""
    env, store = graph(tmp_path)
    barrier = tmp_path / "go"
    procs = writers(env, store, barrier, count=8, each=25, kind="creates")
    release(barrier)
    aliases = [alias for p in procs for alias in result(p, timeout=120)["aliases"]]
    assert len(aliases) == len(set(aliases)) == 200
    prefixes = {alias.rpartition("-")[0] for alias in aliases}
    (prefix,) = prefixes
    assert sorted(int(a.rpartition("-")[2]) for a in aliases) == list(range(1, 201))
    with store.session(timeout_s=5) as session:
        assert session.alias_counters()[prefix] == 200
    snapshot = store.snapshot()
    for alias in aliases:
        assert snapshot.resolve_alias(alias) is not None


def test_readers_never_see_partial_txs(tmp_path: Path) -> None:
    """P4, B4.10: lock-free readers during writers see whole txs and no dangling edges."""
    env, store = graph(tmp_path)
    env.random(5)
    barrier, stop = tmp_path / "go", tmp_path / "stop"
    base = common(env, store, barrier)
    readers = [spawn("store", {**base, "mode": "reader", "stop": str(stop)}) for _ in range(3)]
    procs = writers(env, store, barrier, count=3, each=20, kind="random")
    release(barrier)
    for proc in procs:
        result(proc, timeout=120)
    release(stop)
    reports = [result(r, timeout=120) for r in readers]
    assert_chain(store, 65)
    for report in reports:
        assert report["snapshots"] >= 2
        assert report["seqs"][-1] == 65
    observed = {seq for report in reports for seq in report["seqs"]}
    assert len(observed) > 2, "readers should observe intermediate states"
