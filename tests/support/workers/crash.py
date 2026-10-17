"""Crash worker: stage a blob, open a FileStore session and commit, dying at a fault point."""

import os
import signal
from pathlib import Path
from uuid import UUID

from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.store import FileStore
from support.crash_runner import tx_from_payload
from support.workers import args, emit


def main() -> None:
    spec = args()
    target = CrashPoint(spec["point"])

    def kill_at(point: CrashPoint) -> None:
        if point == target:
            os.kill(os.getpid(), signal.SIGKILL)

    store = FileStore(
        Path(spec["graph_dir"]),
        GraphId(UUID(spec["graph_id"])),
        durability="fsync",
        faults=kill_at,
    )
    staged = None
    if "blob_file" in spec:
        data = Path(spec["blob_file"]).read_bytes()
        staged = store.stage_blob([data], max_bytes=len(data))
    with store.session(timeout_s=10) as session:
        if staged is not None:
            session.publish_blob(staged)
        if "tx" in spec:
            tx = tx_from_payload(spec["tx"])
            receipt = session.append_audit(AuditEventDraft(store.stream, spec["audit"]))
            session.commit(tx, receipt)
    emit({"completed": True})


if __name__ == "__main__":
    main()
