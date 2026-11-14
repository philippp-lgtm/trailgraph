"""Migration worker: marker, intent, ``migrate``, marker removal, dying at a fault point."""

import os
import signal
from pathlib import Path
from uuid import UUID

from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from support.crash_runner import tx_from_payload
from support.workers import args, emit


def main() -> None:
    spec = args()
    target = CrashPoint(spec["point"])

    def kill_at(point: CrashPoint) -> None:
        if point == target:
            os.kill(os.getpid(), signal.SIGKILL)

    graph_dir = Path(spec["graph_dir"])
    graph = GraphId(UUID(spec["graph_id"]))
    maintenance = FileStoreMaintenance(graph_dir.parent.parent, durability="fsync", faults=kill_at)
    with maintenance.maintenance_session(graph, timeout_s=10) as session:
        session.write_migration_marker(MigrationMarker.from_json(spec["marker"]))
        tx = tx_from_payload(spec["tx"])
        receipt = session.append_audit(
            AuditEventDraft(maintenance.store(graph).stream, spec["audit"])
        )
        session.migrate(tx, receipt)
        session.remove_migration_marker()
    emit({"completed": True})


if __name__ == "__main__":
    main()
