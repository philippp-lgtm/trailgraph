"""The admin composition root (contracts B2.2, B12.10, section 2 "Wired by").

The only admin module that reads the process environment. It resolves
config and roots with the same catalog code as the app, adds graph-admin's
``--data-root`` and builds the ``AdminContext`` for the services: actor
(entrypoint ``maint``), clock, ids, catalog, audit log, maintenance port,
policy (the installed files, falling back past an invalid one) and the op
log. Everything is built lazily per command, so a broken config renders as
an error envelope.

The admin API of graph-server builds one ``Services`` per request with an
``AdminRequest``: the request's actor (the token's principal, entrypoint
``maint``) takes the place of the process's identity, and the data root is
always the server's own (B14.15).

A host whose config says ``mode = "remote"`` keeps no data: the commands
the server runs go to its admin API (``remote`` builds the command's
``RemoteSession`` with ``GRAPH_CLI_ADMIN_TOKEN``, else the admin token
``graph connect`` stored for the project of the pointer found from the
working directory, else ``[remote] admin_token_file``); every other command
that opens the data root is refused there (``admin.remote_mode``, B14.13),
so no second writer appears by accident.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli.catalog.config import Config, load_config
from graph_cli.catalog.file_catalog import FileCatalog, StorageSettings
from graph_cli.catalog.identity import resolve_actor, system_inputs
from graph_cli.catalog.oplog import OpLogFile
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.catalog.resolve import config_path, resolve_roots
from graph_cli.cli_kit.runner import Invocation
from graph_cli.kernel.actor import ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.records import EnvInfo
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli_admin import __version__
from graph_cli_admin.services_admin.context import AdminContext

if TYPE_CHECKING:
    from graph_cli.catalog.remote import RemoteSession

PROGRAM = "graph-admin"
ADMIN_TOKEN_ENV: Final = "GRAPH_CLI_ADMIN_TOKEN"  # the admin token in remote mode (B14.15)
ADMIN_ROUTE: Final = "/v1/admin/commands/"
TOKEN_HINT: Final = (
    "set GRAPH_CLI_ADMIN_TOKEN or [remote] admin_token_file to a token created with"
    " graph-admin token create --scope admin"
)


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


@dataclass(frozen=True)
class AdminRequest:
    """What an admin API request decides instead of the process (B14.15)."""

    actor: ActorContext
    warnings: tuple[WarningRecord, ...]  # from the request's asserted facets


@dataclass
class Services:
    """Handed to the commands; builds the context on first use."""

    env: Mapping[str, str]
    cwd: Path
    request: AdminRequest | None = None  # set by the server's admin API, one per request
    _remote: RemoteSession | None = field(default=None, init=False, repr=False)

    def config(self) -> tuple[Config, tuple[WarningRecord, ...]]:
        return load_config(config_path(self.env))

    def remote_mode(self) -> bool:
        """Whether config says ``mode = "remote"``; a broken config is reported by the
        command itself."""
        try:
            return self.request is None and self.config()[0].mode == "remote"
        except GraphCliError:
            return False

    def remote(self, warnings: list[WarningRecord]) -> RemoteSession:
        """The command's link to the server's admin API (remote mode only), built once."""
        if self._remote is None:
            self._remote = self._connect(warnings)
        return self._remote

    def _connect(self, warnings: list[WarningRecord]) -> RemoteSession:
        import graph_cli_admin
        from graph_cli.catalog.build import read_build
        from graph_cli.catalog.compat import cached_compat
        from graph_cli.catalog.endpoint import parse_endpoint
        from graph_cli.catalog.pointer import find_pointer
        from graph_cli.catalog.remote import (
            ADMIN_TOKEN_FILE,
            RemoteClient,
            RemoteSession,
            project_token_path,
            read_token,
        )
        from graph_cli.catalog.wiring import asserted_facets
        from graph_cli_admin.maintenance_cli.admin_skills import admin_compat

        config, config_warnings = self.config()
        endpoint = parse_endpoint(config.endpoint or "")
        if isinstance(endpoint, str):
            raise GraphCliError("internal.error", "remote mode without a valid endpoint")
        actor, actor_warnings = self._actor(config)
        warnings += [*config_warnings, *actor_warnings]
        if endpoint.plain_http and not endpoint.loopback:
            warnings.append(
                WarningRecord(
                    "remote.plain_http",
                    f"{endpoint.display} is plain HTTP: the token travels without encryption",
                    {"endpoint": endpoint.display},
                )
            )
        file = config.admin_token_file
        pointer, pointer_warnings = find_pointer(self.cwd)
        warnings += pointer_warnings
        project = pointer.project if pointer is not None else None
        token = read_token(
            self.env.get(ADMIN_TOKEN_ENV),
            self._path(file) if file is not None else None,
            config=config.path,
            key="remote.admin_token_file",
            hint=TOKEN_HINT,
            project_file=project_token_path(config.path, endpoint, project, ADMIN_TOKEN_FILE),
        )
        build = read_build(graph_cli_admin.__file__)
        try:
            state_root: Path | None = self.state_root()
        except GraphCliError:
            state_root = None  # no cache: computed for this command only
        client = RemoteClient(
            endpoint,
            token,
            timeout_s=config.remote_timeout_s,
            version=__version__,
            program=PROGRAM,
            build=build,
            compat=cached_compat(state_root, PROGRAM, build, admin_compat),
            warnings=warnings,
            token_hint=TOKEN_HINT,
        )
        return RemoteSession(
            client, asserted_facets(actor), None, None, warnings, commands=ADMIN_ROUTE
        )

    def state_root(self) -> Path:
        """This host's state directory (a client keeps the wheels it fetched there)."""
        config, _ = self.config()
        roots, _ = resolve_roots(
            self.env, config=config, cwd=self.cwd, data_root_flag=None, allow_flag=False
        )
        return roots.state_root

    def skill_dirs(self, scope: str, directory: str | None) -> tuple[Path, Path | None]:
        """``(agent configuration directory, project directory)``, as for ``graph skills``:
        ``project`` is ``<dir>/.claude`` (``<dir>`` defaults to the working directory),
        ``user`` is ``$CLAUDE_CONFIG_DIR``, else ``$HOME/.claude``."""
        from graph_cli.catalog.wiring import CLAUDE_CONFIG_ENV

        if scope == "project":
            project = self._path(directory) if directory is not None else self.cwd.resolve()
            return project / ".claude", project
        configured = self.env.get(CLAUDE_CONFIG_ENV)
        if configured:
            return self._path(configured), None
        home = self.env.get("HOME")
        if not home:
            raise GraphCliError(
                "skills.target_invalid",
                "HOME is not set; the user skill directory is unknown",
                details={"scope": "user", "cause": "no_home"},
            )
        return self._path(home) / ".claude", None

    def _path(self, value: str) -> Path:
        home = self.env.get("HOME")
        if home and (value == "~" or value.startswith("~/")):
            value = home + value[1:]
        return (self.cwd / value).resolve()

    def _actor(self, config: Config) -> tuple[ActorContext, tuple[WarningRecord, ...]]:
        if self.request is not None:  # never the server's own identity
            return self.request.actor, self.request.warnings
        return resolve_actor(
            system_inputs(),
            client=ClientInfo(PROGRAM, __version__, Entrypoint.MAINT),
            device_id=config.device_id,
            providers=config.identity_providers,
        )

    def context(self, *, data_root: Path | None, warnings: list[WarningRecord]) -> AdminContext:
        config, config_warnings = self.config()
        if self.request is not None and data_root is not None:
            raise GraphCliError("internal.error", "a request never names the data root")
        if config.mode == "remote":
            raise GraphCliError(
                "admin.remote_mode",
                f'{config.path} says mode = "remote": the data lives with the server;'
                " run maintenance where the data lives",
                details={"file": str(config.path)},
            )
        roots, root_warnings = resolve_roots(
            self.env, config=config, cwd=self.cwd, data_root_flag=data_root, allow_flag=True
        )
        clock = SystemClock()
        actor, actor_warnings = self._actor(config)
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
