"""P: concurrent FileStore writers and readers in real processes.

Contracts B1.8, B4.2, B4.10, B5.3, B13.8.
"""

from pathlib import Path
from typing import Any

import pytest
from support.audit_events import verify_graph_stream
from support.procs import release, result, spawn
from support.stores import StoreEnv

from graph_cli.audit.verify import verify_installation
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
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


def test_audit_chains_stay_valid_across_graphs_and_denials(tmp_path: Path) -> None:
    """B5.3: writers in two graphs plus processes appending denials; every stream
    verifies clean and the installation stream holds every denial once."""
    envs = [StoreEnv("files", "inventory", tmp_path, seed=seed, real_audit=True) for seed in (0, 1)]
    barrier = tmp_path / "go"
    procs = []
    for env in envs:
        assert isinstance(env.store, FileStore)
        base = common(env, env.store, barrier)
        procs += [
            spawn(
                "store",
                {
                    **base,
                    "mode": "writer",
                    "worker": n,
                    "count": 15,
                    "kind": "random",
                    "real_audit": True,
                },
            )
            for n in range(2)
        ]
    deniers = [
        spawn(
            "store",
            {
                "mode": "denier",
                "worker": n,
                "count": 20,
                "data_root": str(tmp_path),
                "barrier": str(barrier),
            },
        )
        for n in range(3)
    ]
    release(barrier)
    for proc in procs:
        assert len(result(proc, timeout=120)["tx_ids"]) == 15
    seqs = sorted(seq for proc in deniers for seq in result(proc, timeout=120)["seqs"])
    assert seqs == list(range(1, 61))
    reports = {}
    for env in envs:
        report = verify_graph_stream(env.store, env.audit, env.graph_id)
        assert report.findings == () and report.head[0] == 30
        reports[str(env.graph_id)] = report
    installation = verify_installation(
        envs[0].audit.read_lines(INSTALLATION_STREAM), graphs=reports
    )
    assert installation.findings == () and installation.head[0] == 60
