"""``graph do`` worker for P tests: the real action service over a data root.

Arguments: ``root``, ``graph``, ``action``, ``params`` (``name=value`` texts), optional
``reason``, optional ``barrier`` (wait for the file first) and ``worker`` (the id in the
result). Production parts throughout: system clock, UUIDv7 provider, FileCatalog,
FileAuditLog, built-in policy. Emits one result: ``{"seq"}`` or ``{"error"}``.
"""

from pathlib import Path

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.records import EnvInfo
from graph_cli.services.actions import ActionService
from graph_cli.services.read import ReadService
from graph_cli.services.write import WriteConfig, WriteService
from graph_cli.storage.files.audit_log import FileAuditLog
from support.procs import wait_for
from support.services import app_actor
from support.workers import args, emit


def main() -> None:
    spec = args()
    root = Path(spec["root"])
    clock, ids, env = SystemClock(), Uuid7Provider(SystemClock()), EnvInfo("<data root>", "flag")
    catalog = FileCatalog(root)
    audit = FileAuditLog(root, durability="fsync", lock_timeout_s=60)
    policy = BuiltinPolicySource()
    reader = ReadService(catalog=catalog, audit=audit, policy=policy, clock=clock, ids=ids, env=env)
    writer = WriteService(
        catalog=catalog,
        audit=audit,
        policy=policy,
        clock=clock,
        ids=ids,
        env=env,
        config=WriteConfig(lock_timeout_s=60),
    )
    service = ActionService(catalog=catalog, reader=reader, writer=writer)
    if spec.get("barrier"):
        wait_for(Path(spec["barrier"]))
    pairs = [tuple(text.split("=", 1)) for text in spec["params"]]
    try:
        done = service.do(
            app_actor(),
            catalog.resolve(spec["graph"]),
            spec["action"],
            pairs,  # type: ignore[arg-type]
            dry_run=False,
            reason=spec.get("reason"),
            idempotency_key=None,
        )
        outcome = {"seq": done.write.seq}
    except GraphCliError as exc:
        outcome = {"error": exc.code}
    emit({"worker": spec.get("worker", 0), "results": [outcome]})


if __name__ == "__main__":
    main()
