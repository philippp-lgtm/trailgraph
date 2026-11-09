"""Key store worker: create keys (``mint``) or shred one (``shred``), optionally dying at a
fault point, or race for keys with other workers behind a barrier (``race``)."""

import os
import signal
from pathlib import Path
from uuid import UUID

from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TxId, Uuid7Provider
from graph_cli.kernel.sealing import Kid
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.keystore import KeyStore, read_kek_file
from support.procs import wait_for
from support.workers import args, emit


def main() -> None:
    spec = args()
    target = CrashPoint(spec["point"]) if spec.get("point") else None

    def kill_at(point: CrashPoint) -> None:
        if point == target:
            os.kill(os.getpid(), signal.SIGKILL)

    clock = SystemClock()
    store = KeyStore.load(
        Path(spec["path"]),
        GraphId(UUID(spec["graph_id"])),
        (read_kek_file(Path(spec["kek_file"])),),
        clock=clock,
        ids=Uuid7Provider(clock),
        faults=kill_at,
    )
    with store:
        if spec["mode"] == "mint":
            emit({"kid": str(store.entity_key(EntityId(UUID(spec["entity"]))))})
        elif spec["mode"] == "shred":
            store.shred([Kid(UUID(spec["kid"]))], EventId(UUID(spec["event"])))
            emit({"shredded": True})
        else:
            wait_for(Path(spec["barrier"]))
            shared = [str(store.entity_key(EntityId(UUID(spec["entity"])))) for _ in range(5)]
            own = [str(store.tx_key(TxId(UUID(tx)))) for tx in spec["txs"]]
            files = [str(store.file_key()) for _ in range(spec["files"])]
            emit({"shared": shared, "own": own, "files": files})


if __name__ == "__main__":
    main()
