"""Write and read timings of FileStore on a generated graph (not run in CI).

Builds a graph of the inventory toy ontology (default: 300 txs with 15
creates each plus random edits, about 4,500 entities) in a temporary
directory, then times what one `graph` process pays: a fresh `FileStore`
per operation, so no replay cache carries over.

    uv run python scripts/bench_store.py [--txs N] [--creates K] [--runs R]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.stores import ALL_STATUSES, StoreEnv
from support.txbuilder import TxDraft

from graph_cli.kernel.query_ast import Query
from graph_cli.storage.files.store import FileStore


def _median_ms(run: Callable[[], object], runs: int) -> float:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        run()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--txs", type=int, default=300)
    parser.add_argument("--creates", type=int, default=15)
    parser.add_argument("--runs", type=int, default=15)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        env = StoreEnv("files", "inventory", Path(tmp))

        def bulk(draft: TxDraft) -> None:
            for _ in range(args.creates):
                draft.create()
            env.builder.random_tx(draft)

        for _ in range(args.txs):
            env.write(bulk)
        assert isinstance(env.store, FileStore)
        root, graph = env.store.layout.root, env.graph_id
        log_bytes = sum(p.stat().st_size for p in env.store.layout.txlog.iterdir())

        def fresh(**options: object) -> FileStore:
            return FileStore(root, graph, syncer=env.syncer, **options)  # type: ignore[arg-type]

        def write(**options: object) -> Callable[[], object]:
            def run() -> object:
                env.store = fresh(**options)
                return env.write(lambda draft: draft.create())

            return run

        def read() -> object:
            snapshot = fresh().snapshot()
            kind = sorted(env.builder.model.classes)[0]
            return snapshot.query(Query(types=(kind,), record_status=ALL_STATUSES, limit=50))

        rows = [("read (snapshot + find 50)", _median_ms(read, args.runs))]
        rows.append(("write", _median_ms(write(), args.runs)))
        rows.append(("write, full replay", _median_ms(write(full_check_every=1), args.runs)))
        everything = Query(record_status=ALL_STATUSES, limit=10**9)
        entities = len(fresh().snapshot().query(everything).items)
        print(f"txs {env.store.head().seq}, log {log_bytes / 1e6:.1f} MB, entities {entities}")
        for name, ms in rows:
            print(f"{name:28} {ms:8.1f} ms (median of {args.runs})")


if __name__ == "__main__":
    main()
