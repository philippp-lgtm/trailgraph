"""Write-service worker for K and P tests: the real pipeline over a data root.

Arguments: ``root``, ``graph``, ``documents`` (change set documents applied
in order, each as its own request), optional ``point`` (a crash point at
which the process sends itself SIGKILL), optional ``barrier`` (wait for the
file first), ``worker`` (seed) and ``uid`` (the principal; the CLI's own uid
when a ``graph`` retry must share the idempotency scope). Production parts throughout: system
clock, UUIDv7 provider, FileCatalog, FileAuditLog, built-in policy. Emits
one result per document: ``{"seq", "replay", "aliases"}`` or ``{"error"}``.
"""

import os
import signal
from pathlib import Path
from typing import Any

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.records import EnvInfo, RequestMeta
from graph_cli.services.write import WriteConfig, WriteRequest, WriteService
from graph_cli.storage.common.faults import CrashPoint
from graph_cli.storage.files.audit_log import FileAuditLog
from support.procs import wait_for
from support.services import app_actor
from support.workers import args, emit


def main() -> None:
    spec = args()
    root = Path(spec["root"])
    target = CrashPoint(spec["point"]) if spec.get("point") else None

    def kill_at(point: CrashPoint) -> None:
        if point == target:
            os.kill(os.getpid(), signal.SIGKILL)

    clock = SystemClock()
    service = WriteService(
        catalog=FileCatalog(root, faults=kill_at if target else None),
        audit=FileAuditLog(root, durability="fsync", lock_timeout_s=60),
        policy=BuiltinPolicySource(),
        clock=clock,
        ids=Uuid7Provider(clock),
        env=EnvInfo("<data root>", "flag"),
        config=WriteConfig(lock_timeout_s=60),
    )
    actor = app_actor(uid=spec["uid"]) if "uid" in spec else app_actor()
    if spec.get("barrier"):
        wait_for(Path(spec["barrier"]))
    results: list[dict[str, Any]] = []
    for document in spec["documents"]:
        request = WriteRequest(
            actor=actor,
            graph=spec["graph"],
            changeset=service.parse(document),
            dry_run=False,
            request=RequestMeta("apply", hash_value(document)),
        )
        try:
            result = service.apply(request)
        except GraphCliError as exc:
            results.append({"error": exc.code})
            continue
        aliases = [e.alias for e in result.entities if e.rev == 1 and e.alias]
        results.append({"seq": result.seq, "replay": result.idempotent_replay, "aliases": aliases})
    emit({"worker": spec.get("worker", 0), "results": results})


if __name__ == "__main__":
    main()
