"""An in-process AdminContext with test clock, seeded ids and a fault switch.

The CLI tests run graph-admin as a subprocess; tests that inject faults
into the maintenance flows call the services with this context instead.
"""

from pathlib import Path

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.catalog.config import Config
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.catalog.identity import IdentityInputs, resolve_actor
from graph_cli.catalog.resolve import Roots
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.records import EnvInfo
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli_admin.services_admin import error_codes
from graph_cli_admin.services_admin.context import AdminContext
from support.clock import StepClock
from support.ids import SeededIdProvider
from support.stores import FaultSwitch, RecordingSyncer


def admin_context(root: Path, faults: FaultSwitch | None = None) -> AdminContext:
    error_codes.register()
    root.mkdir(parents=True, exist_ok=True)
    clock = StepClock()
    syncer = RecordingSyncer()
    actor, _ = resolve_actor(
        IdentityInputs(env={}, uid=501, user_name="maintainer", pid=1),
        client=ClientInfo("graph-admin", "0.1.0", Entrypoint.MAINT),
    )
    roots = Roots(root, root / "config.toml", root / "state", "flag")
    return AdminContext(
        roots=roots,
        config=Config(roots.config_file),
        actor=actor,
        clock=clock,
        ids=SeededIdProvider(clock, 21),
        catalog=FileCatalog(root),
        audit=FileAuditLog(root, syncer=syncer, faults=faults),
        maintenance=FileStoreMaintenance(root, syncer=syncer, faults=faults),
        policy=BuiltinPolicySource(),
        env=EnvInfo("<data root>", "flag"),
        lock_timeout_s=5.0,
    )
