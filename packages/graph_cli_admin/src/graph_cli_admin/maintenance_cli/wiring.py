"""The admin composition root (contracts B2.2, B12.10, section 2 "Wired by").

The only admin module that reads the process environment. It resolves
config and roots with the same catalog code as the app, adds graph-admin's
``--data-root`` and builds the ``AdminContext`` for the services: actor
(entrypoint ``maint``), clock, ids, catalog, audit log, maintenance port,
policy (the installed files, falling back past an invalid one) and the op
log. Everything is built lazily per command, so a broken config renders as
an error envelope.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from graph_cli.catalog.config import Config, load_config
from graph_cli.catalog.file_catalog import FileCatalog, StorageSettings
from graph_cli.catalog.identity import resolve_actor, system_inputs
from graph_cli.catalog.oplog import OpLogFile
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.catalog.resolve import config_path, resolve_roots
from graph_cli.cli_kit.runner import Invocation
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.records import EnvInfo
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli_admin import __version__
from graph_cli_admin.services_admin.context import AdminContext

PROGRAM = "graph-admin"


def process_env() -> dict[str, str]:
    return dict(os.environ)


def invocation(env: Mapping[str, str]) -> Invocation:
    """JSON mode from ``GRAPH_CLI_OUTPUT`` or config ``[output] default`` (B9.1, B12.7)."""
    output = env.get("GRAPH_CLI_OUTPUT")
    if output is None:
        try:
            output = load_config(config_path(env))[0].output
        except GraphCliError:
            output = None  # the command reports the broken config itself
    return Invocation(PROGRAM, output == "json", env.get("GRAPH_CLI_DEBUG") == "1")


@dataclass
class Services:
    """Handed to the commands; builds the context on first use."""

    env: Mapping[str, str]
    cwd: Path

    def config(self) -> tuple[Config, tuple[WarningRecord, ...]]:
        return load_config(config_path(self.env))

    def context(self, *, data_root: Path | None, warnings: list[WarningRecord]) -> AdminContext:
        config, config_warnings = self.config()
        roots, root_warnings = resolve_roots(
            self.env, config=config, cwd=self.cwd, data_root_flag=data_root, allow_flag=True
        )
        clock = SystemClock()
        client = ClientInfo(PROGRAM, __version__, Entrypoint.MAINT)
        actor, actor_warnings = resolve_actor(
            system_inputs(),
            client=client,
            device_id=config.device_id,
            providers=config.identity_providers,
        )
        warnings += [*config_warnings, *root_warnings, *actor_warnings]
        oplog = OpLogFile(roots.state_root, clock)

        def fallback(error: GraphCliError) -> None:
            """An invalid installed policy must not stop the maintainer replacing it (B6.14);
            one warning per file, also when the policy is read again under a lock."""
            warning = WarningRecord(
                "admin.policy_fallback",
                f"{error.message}; deciding without it until it is replaced",
                {"file": error.details.get("file")},
            )
            if warning not in warnings:
                warnings.append(warning)

        settings = StorageSettings(config.lock_timeout_s, config.durability)
        policy = FilePolicySource(roots.data_root, fallback=fallback)
        policy.installation()  # a broken installation policy warns on every command
        return AdminContext(
            roots=roots,
            config=config,
            actor=actor,
            clock=clock,
            ids=Uuid7Provider(clock),
            catalog=FileCatalog(roots.data_root, settings=settings, oplog=oplog),
            audit=FileAuditLog(
                roots.data_root,
                durability=config.durability,
                lock_timeout_s=config.lock_timeout_s,
                oplog=oplog,
            ),
            maintenance=FileStoreMaintenance(
                roots.data_root, durability=config.durability, oplog=oplog
            ),
            policy=policy,
            env=EnvInfo(
                str(roots.data_root) if config.record_data_root else None, roots.root_source
            ),
            lock_timeout_s=config.lock_timeout_s,
        )
