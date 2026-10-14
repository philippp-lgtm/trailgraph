"""FileStore worker for P tests: ``writer`` commits txs, ``reader`` checks snapshots.

All workers wait for the barrier file, so they start at the same time.
"""

import sys
from pathlib import Path
from uuid import UUID

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.query_ast import Query
from graph_cli.storage.files.store import FileStore
from support.clock import StepClock
from support.drafts import intent
from support.ids import SeededIdProvider
from support.memlog import model
from support.procs import wait_for
from support.stores import ALL_STATUSES, snapshot_state
from support.txbuilder import TxBuilder
from support.workers import args, emit


def open_store(spec: dict[str, object]) -> FileStore:
    return FileStore(
        Path(str(spec["graph_dir"])), GraphId(UUID(str(spec["graph_id"]))), durability="fsync"
    )


def writer(spec: dict[str, object]) -> None:
    store = open_store(spec)
    worker = int(str(spec["worker"]))
    clock = StepClock()
    builder = TxBuilder(
        model(str(spec["ontology"])),
        graph_id=store.graph_id,
        ids=SeededIdProvider(clock, seed=1000 + worker),
        clock=clock,
        seed=worker,
    )
    creates_only = spec["kind"] == "creates"
    cls = next(c for c in builder.concrete_classes if builder.prefix_of(c))
    tx_ids, aliases = [], []
    wait_for(Path(str(spec["barrier"])))
    for _ in range(int(str(spec["count"]))):
        with store.session(timeout_s=60) as session:
            draft = builder.begin(
                snapshot_state(session.snapshot()), session.head(), session.alias_counters()
            )
            if creates_only:
                draft.create(cls)
            else:
                builder.random_tx(draft)
            tx = draft.build()
            session.commit(tx, session.append_audit(intent(tx)))
        tx_ids.append(str(tx.tx_id))
        aliases += [alias for _, alias in tx.alias_allocations]
    emit({"tx_ids": tx_ids, "aliases": aliases})


def check_snapshot(store: FileStore) -> int:
    """One isolation check; returns the snapshot seq. Raises AssertionError on a violation."""
    snapshot = store.snapshot()
    seq = snapshot.seq
    items = snapshot.query(Query(record_status=ALL_STATUSES, limit=10**9)).items
    for revision in items:
        assert revision.tx_seq <= seq, "revision from a later tx"
        for edge in revision.edges:
            assert snapshot.get(edge.target) is not None, f"dangling edge at seq {seq}"
    for tx in store.changes_since(0, limit=10**9):
        for revision in tx.revisions:
            visible = snapshot.get_revision(revision.id, revision.rev)
            if tx.seq <= seq:
                assert visible == revision, f"tx {tx.seq} only partly visible at seq {seq}"
            else:
                assert visible is None, f"tx {tx.seq} visible at seq {seq}"
    return seq


def reader(spec: dict[str, object]) -> None:
    store = open_store(spec)
    stop = Path(str(spec["stop"]))
    wait_for(Path(str(spec["barrier"])))
    seqs: list[int] = []
    while not stop.exists() or not seqs:
        seqs.append(check_snapshot(store))
    seqs.append(check_snapshot(store))
    emit({"snapshots": len(seqs), "seqs": sorted(set(seqs))})


def main() -> None:
    spec = args()
    try:
        {"writer": writer, "reader": reader}[spec["mode"]](spec)
    except (AssertionError, GraphCliError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
