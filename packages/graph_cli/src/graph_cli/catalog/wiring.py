"""The app composition root (contracts B2.2, B2.7, B12, section 2 "Wired by").

The app's only reader of the process environment and the working
directory. It resolves config and roots with the code graph-admin uses (no
``--data-root``; ``GRAPH_CLI_ROOT`` warns), finds the project pointer,
builds the actor (entrypoint ``app``, never from env or flags), and wires
clock, ids, catalog, audit log, the installed policy and the services.
Graph selection asks the policy first (B6.15); a project the policy has no
scope for warns once per command (``access.project_unknown``). Everything is
built lazily on the first command that needs it, so a broken config
renders as an error envelope; nothing is created in the data root (B12.4).
The write and file services, and their modules, are built only when a
command uses them: a read starts without them.
With ``[audit] access_log``, a command that built the context is recorded
in the access log after its envelope is written (``record_access``), with
the command's ``Timings``: building the context is the ``context`` phase,
the services add theirs.

The server builds one ``AppServices`` per request with a ``RequestScope``
(B14.8): the request's actor (token principal, entrypoint ``server``),
pointer, ``GRAPH_CLI_GRAPH`` and the origins of uploaded files take the
place of the process's identity, the working directory's pointer, its
environment variable and the configured file roots.

In remote mode (``[runtime] mode = "remote"``) there is no local context:
``remote`` builds the command's ``RemoteSession`` instead, with the token
(``GRAPH_CLI_TOKEN``, else ``[remote] token_file``), the asserted facets of
this process and the pointer as seen from the working directory (B14.10).
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from graph_cli import __version__
from graph_cli.access_control.effective import project_warning
from graph_cli.catalog.access_log import AccessLogFile, access_record
from graph_cli.catalog.activity import SocketActivity, activity_dir
from graph_cli.catalog.config import Config, load_config
from graph_cli.catalog.file_catalog import FileCatalog, StorageSettings
from graph_cli.catalog.file_roots import FileRoots, GivenOrigins
from graph_cli.catalog.identity import resolve_actor, system_inputs
from graph_cli.catalog.oplog import OpLogFile
from graph_cli.catalog.pointer import GRAPH_ENV, PointerFile, find_pointer, select_graph
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.catalog.resolve import Roots, config_path, resolve_roots
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ActivitySink
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import EnvInfo, RequestMeta
from graph_cli.kernel.timing import Timings
from graph_cli.services.read import ReadService
from graph_cli.storage.files.audit_log import FileAuditLog

if TYPE_CHECKING:
    from graph_cli.catalog.remote import RemoteSession
    from graph_cli.services.files import FileService, Origins
    from graph_cli.services.write import WriteService

PROGRAM: Final = "graph"
CLAUDE_CONFIG_ENV: Final = "CLAUDE_CONFIG_DIR"  # relocates the user's ~/.claude (D15 step 0)
SPILL_ENV: Final = "GRAPH_CLI_SPILL_BYTES"  # overrides [output] spill_bytes; 0 turns it off
TOKEN_ENV: Final = "GRAPH_CLI_TOKEN"  # the bearer token in remote mode (B14.10)


def process_env() -> dict[str, str]:
    return dict(os.environ)


def process_executable() -> str | None:
    """The path this program runs from, resolved (D15 Q4: a check names the program
    that checked, so a checkout's ``uv run graph`` is not mistaken for the installed one)."""
    if not sys.argv or not sys.argv[0]:
        return None
    path = Path(sys.argv[0])
    return str(path.resolve()) if path.exists() else sys.argv[0]


def output_mode(env: Mapping[str, str]) -> tuple[bool, bool]:
    """``(json, debug)``: JSON from ``GRAPH_CLI_OUTPUT`` or config ``[output] default``
    (B9.1, B12.7), debug from ``GRAPH_CLI_DEBUG`` (B9.10)."""
    output = env.get("GRAPH_CLI_OUTPUT")
    if output is None:
        try:
            output = load_config(config_path(env))[0].output
        except GraphCliError:
            output = None  # the command reports the broken config itself
    return output == "json", env.get("GRAPH_CLI_DEBUG") == "1"


@dataclass(frozen=True)
class _Backend:
    """What the write and file services are built from, on first use."""

    audit: FileAuditLog
    policy: FilePolicySource
    clock: SystemClock
    ids: Uuid7Provider
    env: EnvInfo
    activity: ActivitySink
    origins: Origins
    timings: Timings


@dataclass(frozen=True)
class AppContext:
    roots: Roots
    config: Config
    pointer: PointerFile | None
    actor: ActorContext
    catalog: FileCatalog
    reader: ReadService
    backend: _Backend = field(repr=False)
    graph_env: str | None
    command: str = ""  # the running command, for audited selection refusals

    @functools.cached_property
    def writer(self) -> WriteService:
        from graph_cli.services.write import WriteConfig, WriteService

        backend = self.backend
        return WriteService(
            catalog=self.catalog,
            audit=backend.audit,
            policy=backend.policy,
            clock=backend.clock,
            ids=backend.ids,
            env=backend.env,
            config=WriteConfig(lock_timeout_s=self.config.lock_timeout_s),
            timings=backend.timings,
        )

    @functools.cached_property
    def files(self) -> FileService:
        from graph_cli.services.files import FileService

        return FileService(
            catalog=self.catalog,
            writer=self.writer,
            reader=self.reader,
            origins=self.backend.origins,
            max_bytes=self.config.max_file_bytes,
            activity=self.backend.activity,
        )

    def select(self, flag: str | None) -> GraphEntry:
        """B12.5: ``--graph`` > ``GRAPH_CLI_GRAPH`` > pointer default."""
        return select_graph(
            self.catalog,
            flag=flag,
            env_value=self.graph_env,
            pointer=self.pointer,
            visibility=_Visibility(self),
        )

    def select_for(self, ref: str, pointer: PointerFile | None) -> GraphEntry:
        """A named graph as a project with ``pointer`` would see it (D15 Q3); the policy
        judges the invoking actor."""
        return select_graph(
            self.catalog, flag=ref, env_value=None, pointer=pointer, visibility=_Visibility(self)
        )


@dataclass(frozen=True)
class _Visibility:
    """The policy's view for graph selection (``catalog.pointer.Visibility``)."""

    context: AppContext

    def visible(self, entry: GraphEntry) -> bool:
        return self.context.reader.visible(self.context.actor, entry)

    def refuse(self, entry: GraphEntry, ref: str) -> GraphCliError:
        request = RequestMeta.of(self.context.command, {"graph": ref})
        return self.context.reader.refuse_hidden(self.context.actor, entry, ref, request)


@dataclass(frozen=True)
class RequestScope:
    """What a server request decides instead of the process (B14.8)."""

    actor: ActorContext
    warnings: tuple[WarningRecord, ...]  # from the request's asserted facets
    pointer: PointerFile | None
    graph_env: str | None
    origins: Mapping[Path, Mapping[str, JsonValue] | None]  # uploaded file -> its origin


@dataclass
class AppServices:
    """Handed to the commands; builds the context on first use, once."""

    env: Mapping[str, str]
    cwd: Path
    executable: str | None = None  # the running program's own path (sys.argv[0]), F6
    timings: Timings = field(default_factory=Timings)
    request: RequestScope | None = None  # set by the server, one per request
    _context: AppContext | None = field(default=None, init=False, repr=False)
    _remote: RemoteSession | None = field(default=None, init=False, repr=False)
    _warnings: tuple[WarningRecord, ...] = field(default=(), init=False, repr=False)

    def _actor(
        self, config: Config, pointer: PointerFile | None
    ) -> tuple[ActorContext, tuple[WarningRecord, ...]]:
        return resolve_actor(
            system_inputs(),
            client=ClientInfo(PROGRAM, __version__, Entrypoint.APP),
            device_id=config.device_id,
            project=pointer.project if pointer is not None else None,
            providers=config.identity_providers,
        )

    def path(self, value: str) -> Path:
        """A path from the command line or env: ``~`` is ``HOME``, relative is from cwd."""
        home = self.env.get("HOME")
        if home and (value == "~" or value.startswith("~/")):
            value = home + value[1:]
        return (self.cwd / value).resolve()

    def skill_dirs(self, scope: str, directory: str | None) -> tuple[Path, Path | None]:
        """D15 Q3: ``(agent configuration directory, project directory)``. ``user`` is
        ``$CLAUDE_CONFIG_DIR``, else ``$HOME/.claude`` (D15 step 0); ``project`` is
        ``<dir>/.claude`` with ``<dir>`` defaulting to the working directory."""
        if scope == "project":
            project = self.path(directory) if directory is not None else self.cwd.resolve()
            return project / ".claude", project
        configured = self.env.get(CLAUDE_CONFIG_ENV)
        if configured:
            return self.path(configured), None
        home = self.env.get("HOME")
        if not home:
            raise GraphCliError(
                "skills.target_invalid",
                "HOME is not set; the user skill directory is unknown",
                details={"scope": "user", "cause": "no_home"},
            )
        return self.path(home) / ".claude", None

    def pointer_at(self, directory: Path) -> tuple[PointerFile | None, tuple[WarningRecord, ...]]:
        """The project pointer as seen from ``directory`` (B12.6 walk-up)."""
        return find_pointer(directory)

    def context(self, warnings: list[WarningRecord], command: str | None = None) -> AppContext:
        if self._context is None:
            with self.timings.phase("context"):
                self._context, self._warnings = self._build(command or "")
        warnings += self._warnings
        return self._context

    def remote_mode(self) -> bool:
        """Whether config says ``mode = "remote"``; a broken config is reported by the
        command itself, locally."""
        try:
            return load_config(config_path(self.env))[0].mode == "remote"
        except GraphCliError:
            return False

    def remote(self, warnings: list[WarningRecord]) -> RemoteSession:
        """The command's link to the server (remote mode only), built once."""
        if self._remote is None:
            self._remote = self._connect(warnings)
        return self._remote

    def remote_roots(self) -> tuple[Config, Roots, FileRoots]:
        """Config, roots (the state directory for caches) and file roots of this client."""
        config, _ = load_config(config_path(self.env))
        roots, _ = resolve_roots(
            self.env, config=config, cwd=self.cwd, data_root_flag=None, allow_flag=False
        )
        return config, roots, FileRoots.from_config(config.file_roots, self.env)

    def _connect(self, warnings: list[WarningRecord]) -> RemoteSession:
        from graph_cli.catalog.endpoint import parse_endpoint
        from graph_cli.catalog.remote import RemoteClient, RemoteSession

        config, config_warnings = load_config(config_path(self.env))
        endpoint = parse_endpoint(config.endpoint or "")
        if isinstance(endpoint, str):
            raise GraphCliError("internal.error", "remote mode without a valid endpoint")
        pointer, pointer_warnings = find_pointer(self.cwd)
        actor, actor_warnings = self._actor(config, pointer)
        warnings += [*config_warnings, *pointer_warnings, *actor_warnings]
        if endpoint.plain_http and not endpoint.loopback:
            warnings.append(
                WarningRecord(
                    "remote.plain_http",
                    f"{endpoint.display} is plain HTTP: the token travels without encryption",
                    {"endpoint": endpoint.display},
                )
            )
        client = RemoteClient(
            endpoint,
            self._token(config),
            timeout_s=config.remote_timeout_s,
            version=__version__,
        )
        shown: dict[str, JsonValue] | None = None
        if pointer is not None:
            shown = {"graphs": list(pointer.graphs), "file": str(pointer.path)}
            if pointer.default is not None:
                shown["default"] = pointer.default
        return RemoteSession(
            client,
            asserted_facets(actor),
            shown,
            self.env.get(GRAPH_ENV) or None,
            warnings,
        )

    def _token(self, config: Config) -> str:
        """``GRAPH_CLI_TOKEN``, else the token file, which nobody else may read."""
        from graph_cli.catalog.remote import unauthorized

        given = self.env.get(TOKEN_ENV, "").strip()
        if given:
            return given
        if config.token_file is None:
            raise unauthorized()
        path = self.path(config.token_file)
        try:
            if path.stat().st_mode & 0o077:
                raise GraphCliError(
                    "config.invalid",
                    f"{config.path}: remote.token_file {path} may be read by others;"
                    f" chmod 600 {path}",
                    details={"file": str(config.path), "key": "remote.token_file"},
                )
            token = path.read_text().strip()
        except OSError as exc:
            raise GraphCliError(
                "config.invalid",
                f"{config.path}: remote.token_file cannot be read: {exc.strerror or exc}",
                details={"file": str(config.path), "key": "remote.token_file"},
            ) from exc
        if not token:
            raise unauthorized()
        return token

    def spill_config(self) -> Config:
        """The config for large answers: the command's, else read now (defaults if broken)."""
        if self._context is not None:
            return self._context.config
        try:
            return load_config(config_path(self.env))[0]
        except GraphCliError:
            return Config(config_path(self.env))

    def record_access(
        self,
        document: Mapping[str, JsonValue],
        *,
        json_mode: bool,
        stdout_bytes: int,
        spilled_bytes: int | None = None,
    ) -> None:
        """The access log line of a finished command and its envelope. Commands that
        never opened the installation (``describe commands``, a broken config) are not
        recorded: without the context there is no actor and no config to ask."""
        context = self._context
        if context is None or not context.config.access_log:
            return
        record = access_record(
            ts=SystemClock().now(),
            actor=context.actor,
            document=document,
            json_mode=json_mode,
            stdout_bytes=stdout_bytes,
            spilled_bytes=spilled_bytes,
            timing=self.timings.to_json(),
        )
        AccessLogFile(context.roots.state_root).append(record)

    def _build(self, command: str) -> tuple[AppContext, tuple[WarningRecord, ...]]:
        config, config_warnings = load_config(config_path(self.env))
        if config.mode == "remote":  # every command goes through the server (B14.10)
            raise GraphCliError("internal.error", "remote mode has no local context")
        roots, root_warnings = resolve_roots(
            self.env, config=config, cwd=self.cwd, data_root_flag=None, allow_flag=False
        )
        request = self.request
        if request is None:
            pointer, pointer_warnings = find_pointer(self.cwd)
            actor, actor_warnings = self._actor(config, pointer)
        else:  # never the server's own working directory or identity
            pointer, pointer_warnings = request.pointer, ()
            actor, actor_warnings = request.actor, request.warnings
        clock = SystemClock()
        ids = Uuid7Provider(clock)
        oplog = OpLogFile(roots.state_root, clock)
        catalog = FileCatalog(
            roots.data_root,
            settings=StorageSettings(config.lock_timeout_s, config.durability),
            oplog=oplog,
        )
        audit = FileAuditLog(
            roots.data_root,
            durability=config.durability,
            lock_timeout_s=config.lock_timeout_s,
            oplog=oplog,
        )
        policy = FilePolicySource(roots.data_root)  # fails closed on an invalid file
        env = EnvInfo(str(roots.data_root) if config.record_data_root else None, roots.root_source)
        activity: ActivitySink = (
            SocketActivity(activity_dir(roots.state_root)) if config.signal_reads else NULL_ACTIVITY
        )
        reader = ReadService(
            catalog=catalog,
            audit=audit,
            policy=policy,
            clock=clock,
            ids=ids,
            env=env,
            activity=activity,
            timings=self.timings,
        )
        context = AppContext(
            roots=roots,
            config=config,
            pointer=pointer,
            actor=actor,
            catalog=catalog,
            reader=reader,
            backend=_Backend(
                audit=audit,
                policy=policy,
                clock=clock,
                ids=ids,
                env=env,
                activity=activity,
                origins=FileRoots.from_config(config.file_roots, self.env)
                if request is None
                else GivenOrigins(request.origins),
                timings=self.timings,
            ),
            graph_env=self.env.get(GRAPH_ENV) or None if request is None else request.graph_env,
            command=command,
        )
        unknown = project_warning(actor, policy.current(None))  # fails closed on a bad policy
        warnings = (
            *config_warnings,
            *root_warnings,
            *pointer_warnings,
            *actor_warnings,
            *((unknown,) if unknown is not None else ()),
        )
        return context, warnings


def asserted_facets(actor: ActorContext) -> dict[str, JsonValue]:
    """The ``actor`` object of a request: this process's asserted facets (B14.4)."""
    found: dict[str, JsonValue] = {}
    for name, keys in (
        ("agent", ("id", "type")),
        ("session", ("id", "parent")),
        ("device", ("id",)),
    ):
        facet = getattr(actor, name)
        if facet.assurance is not Assurance.ABSENT and facet.id is not None:
            found[name] = {k: getattr(facet, k) for k in keys if getattr(facet, k) is not None}
    if actor.project.assurance is not Assurance.ABSENT and actor.project.id is not None:
        found["project"] = actor.project.id
    return found


@dataclass(frozen=True)
class OutputSpill:
    """``cli_kit.spill.SpillTarget`` of the app: ``GRAPH_CLI_SPILL_BYTES``, else ``[output]
    spill_bytes``; files under ``[output] spill_dir``, else ``/tmp/graph_cli_<uid>``. The
    config is read only when an answer is large enough to need it."""

    services: AppServices

    def limit(self) -> int:
        value = self.services.env.get(SPILL_ENV)
        if value is not None and value.strip().isdigit():
            return int(value)
        return self.services.spill_config().spill_bytes

    def write(self, graph: str | None, command: str | None, content: bytes) -> str:
        from graph_cli.catalog.spill import SpillFiles, default_spill_dir  # large answers only

        configured = self.services.spill_config().spill_dir
        root = self.services.path(configured) if configured else default_spill_dir()
        return SpillFiles(root, SystemClock().now()).write(graph, command, content)
