"""An in-process AdminContext with test clock, seeded ids and a fault switch.

The CLI tests run graph-admin as a subprocess; tests that inject faults
into the maintenance flows call the services with this context instead.
"""

import os
from pathlib import Path

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.catalog.config import Config
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.catalog.identity import IdentityInputs, resolve_actor
from graph_cli.catalog.resolve import Roots
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.kernel.ids import GraphId, IdProvider, Uuid7Provider
from graph_cli.kernel.records import EnvInfo
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.keyrings import KeyRings, KeySettings
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore
from graph_cli_admin.services_admin import error_codes
from graph_cli_admin.services_admin.context import AdminContext
from support.clock import StepClock
from support.ids import SeededIdProvider
from support.stores import FaultSwitch, RecordingSyncer

TEST_KEK_HEX = bytes(range(32)).hex()


def key_settings(root: Path) -> KeySettings:
    """The key stores and the KEK of the graphs below ``root``: beside it, never inside."""
    keys = root.with_name(f"{root.name}-keys")
    kek = keys / "kek"
    if not kek.exists():
        keys.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(kek, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(TEST_KEK_HEX + "\n")
    return KeySettings(dir=keys, kek_file=kek)


def key_rings(root: Path, *, clock: Clock | None = None, ids: IdProvider | None = None) -> KeyRings:
    clock = clock or SystemClock()
    return KeyRings(key_settings(root), clock=clock, ids=ids or Uuid7Provider(clock))


def rings_for(root: Path) -> KeyRings:
    """Key rings that open the sealed graphs below ``root``: those the CLI created in a test
    home (``<home>/.local/share/graph_cli``, its KEK and key stores in the XDG directories)
    or those created through ``admin_context``."""
    if root.parts[-3:] == (".local", "share", "graph_cli"):
        home = root.parents[2]
        kek = home / ".config" / "graph_cli" / "kek"
        if kek.exists():
            clock = SystemClock()
            settings = KeySettings(home / ".local" / "share" / "graph_cli_keys", kek)
            return KeyRings(settings, clock=clock, ids=Uuid7Provider(clock))
    return key_rings(root)


def graph_store(root: Path, graph_id: GraphId) -> FileStore:
    """The store of a graph below ``root`` with its codec: sealed graphs open their keys."""
    return FileStoreMaintenance(root, rings=rings_for(root)).store(graph_id)


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
    ids = SeededIdProvider(clock, 21)
    rings = key_rings(root, ids=ids)
    return AdminContext(
        roots=roots,
        config=Config(roots.config_file),
        actor=actor,
        clock=clock,
        ids=ids,
        catalog=FileCatalog(root, rings=rings),
        audit=FileAuditLog(root, syncer=syncer, faults=faults),
        maintenance=FileStoreMaintenance(root, syncer=syncer, faults=faults, rings=rings),
        policy=BuiltinPolicySource(),
        env=EnvInfo("<data root>", "flag"),
        lock_timeout_s=5.0,
    )
