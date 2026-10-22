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
With ``[audit] access_log``, a command that built the context is recorded
in the access log after its envelope is written (``record_access``), with
the command's ``Timings``: building the context is the ``context`` phase,
the services add theirs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from graph_cli import __version__
from graph_cli.access_control.effective import project_warning
from graph_cli.catalog.access_log import AccessLogFile, access_record
from graph_cli.catalog.activity import SocketActivity, activity_dir
from graph_cli.catalog.config import Config, load_config
from graph_cli.catalog.file_catalog import FileCatalog, StorageSettings
from graph_cli.catalog.file_roots import FileRoots
from graph_cli.catalog.identity import resolve_actor, system_inputs
from graph_cli.catalog.oplog import OpLogFile
from graph_cli.catalog.pointer import GRAPH_ENV, PointerFile, find_pointer, select_graph
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.catalog.resolve import Roots, config_path, resolve_roots
from graph_cli.kernel.actor import ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ActivitySink
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import EnvInfo, RequestMeta
from graph_cli.kernel.timing import Timings
from graph_cli.services.files import FileService
from graph_cli.services.read import ReadService
from graph_cli.services.write import WriteConfig, WriteService
from graph_cli.storage.files.audit_log import FileAuditLog

PROGRAM: Final = "graph"
CLAUDE_CONFIG_ENV: Final = "CLAUDE_CONFIG_DIR"  # relocates the user's ~/.claude (D15 step 0)


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
class AppContext:
    roots: Roots
    config: Config
    pointer: PointerFile | None
    actor: ActorContext
    catalog: FileCatalog
    reader: ReadService
    writer: WriteService
    files: FileService
    graph_env: str | None
    command: str = ""  # the running command, for audited selection refusals

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


@dataclass
class AppServices:
    """Handed to the commands; builds the context on first use, once."""

    env: Mapping[str, str]
    cwd: Path
    executable: str | None = None  # the running program's own path (sys.argv[0]), F6
    timings: Timings = field(default_factory=Timings)
    _context: AppContext | None = field(default=None, init=False, repr=False)
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

    def record_access(
        self, document: Mapping[str, JsonValue], *, json_mode: bool, stdout_bytes: int
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
            timing=self.timings.to_json(),
        )
        AccessLogFile(context.roots.state_root).append(record)

    def _build(self, command: str) -> tuple[AppContext, tuple[WarningRecord, ...]]:
        config, config_warnings = load_config(config_path(self.env))
        roots, root_warnings = resolve_roots(
            self.env, config=config, cwd=self.cwd, data_root_flag=None, allow_flag=False
        )
        pointer, pointer_warnings = find_pointer(self.cwd)
        actor, actor_warnings = self._actor(config, pointer)
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
        writer = WriteService(
            catalog=catalog,
            audit=audit,
            policy=policy,
            clock=clock,
            ids=ids,
            env=env,
            config=WriteConfig(lock_timeout_s=config.lock_timeout_s),
            timings=self.timings,
        )
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
            writer=writer,
            files=FileService(
                catalog=catalog,
                writer=writer,
                reader=reader,
                origins=FileRoots.from_config(config.file_roots, self.env),
                max_bytes=config.max_file_bytes,
                activity=activity,
            ),
            graph_env=self.env.get(GRAPH_ENV) or None,
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
