"""The WSGI application of ``graph-server`` (contracts B14).

Routes are generated from the ``graph`` declarations: every command the
server runs (``remote`` is ``server`` or ``hooked``) is ``POST
/v1/commands/<path>``; its JSON parameters become argv for the same click
program, run by ``cli_kit.runner.run`` with one ``AppServices`` per request
(the request's actor, pointer, ``GRAPH_CLI_GRAPH`` and file origins). Every
answer is the envelope; the HTTP status follows the error category. A
request without a valid bearer token reaches no service and is only logged.
A request that names another build of its program than the server runs is
refused before any route (``remote.client_outdated``) unless it also names
the server's compatibility id of that program (``catalog.compat``; with
``[server] client_builds = "exact"`` it is refused anyway, with ``"warn"``
never). The graph skills a client has
installed come in ``Graph-Cli-Skills`` and are judged when a command selects
a graph (``catalog.skill_claims``). ``/v1/dist/`` hands out the programs
the server was built from (``graph_cli_server.dist``): ``graph``'s wheel to
anyone, ``graph-admin``'s to an admin token, never refused for a build (they
are how a client catches up). ``/v1/enroll`` exchanges an enrollment code
for a token (``graph_cli_server.enroll``), also before any token and build
check. No body, token or query string is ever logged.

HTTP conventions (B14.22 G7-G10): a write command takes its idempotency key also as
the ``Idempotency-Key`` header; an error answer is RFC 9457 problem details when
``Accept`` asks for them (``graph_cli_server.problems``) and ``GET /v1/errors`` lists
the error codes; a wrong method is 405 with ``Allow``, a retryable 503 carries
``Retry-After``, and the file download an ``ETag`` it answers ``If-None-Match`` with.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import os
import shlex
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final, Protocol

import click

import graph_cli
from graph_cli import __version__
from graph_cli.app_cli.commands import all_specs
from graph_cli.app_cli.commands.meta import inventory
from graph_cli.app_cli.commands.skills import app_compat
from graph_cli.app_cli.main import program
from graph_cli.catalog.build import (
    BUILD_HEADER,
    build_header,
    parse_build_header,
    read_build,
    short,
)
from graph_cli.catalog.compat import COMPAT_HEADER
from graph_cli.catalog.config import load_config
from graph_cli.catalog.identity.request import request_actor
from graph_cli.catalog.pointer import PointerFile, request_pointer
from graph_cli.catalog.resolve import config_path, resolve_roots
from graph_cli.catalog.skill_claims import SkillClaim, parse_claims_header
from graph_cli.catalog.tokens import ADMIN_SCOPE, APP_SCOPE, TokenRecord, TokenStore
from graph_cli.catalog.wiring import PROGRAM, AppServices, RequestScope
from graph_cli.cli_kit.argv import inventory_args, to_argv
from graph_cli.cli_kit.envelope import ENVELOPE_SCHEMA, Result, envelope, render_json, render_text
from graph_cli.cli_kit.exit_codes import EXIT_OK, exit_code_for
from graph_cli.cli_kit.runner import Completed, Invocation, run
from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.changeset import IDEMPOTENCY_KEY_RE, FloatLiteral, float_numbers
from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.timing import Timings
from graph_cli_server.dist import DIST_ENV, Served, load
from graph_cli_server.http_status import status_for
from graph_cli_server.openapi import API_VERSION, openapi
from graph_cli_server.problems import (
    PROBLEM_TYPE,
    lookup,
    problem,
    registry,
    wants_problem,
)
from graph_cli_server.uploads import DIGEST_RE, Uploads, link_name

SERVER_PROGRAM: Final = "graph-server"
MAX_BODY_BYTES: Final = 16 * 1024 * 1024
CLIENT: Final = ClientInfo(SERVER_PROGRAM, __version__, Entrypoint.SERVER)
VERSION_HEADER: Final = "Graph-Cli-Version"
EXIT_HEADER: Final = "Graph-Cli-Exit-Code"
ACTOR_HEADER: Final = "HTTP_GRAPH_CLI_ACTOR"
KEY_HEADER: Final = "Idempotency-Key"  # draft-ietf-httpapi-idempotency-key-header, B14.22 G7
KEY_OPTION: Final = "--idempotency-key"
RETRY_AFTER: Final = "2"  # seconds; the request already waited the lock timeout
CORS_METHODS: Final = "GET, POST, PUT"
CORS_HEADERS: Final = (
    "Authorization, Content-Type, Graph-Cli-Version, Graph-Cli-Build, Graph-Cli-Compat,"
    f" Graph-Cli-Skills, Graph-Cli-Actor, {KEY_HEADER}, If-None-Match"
)
CORS_EXPOSED: Final = (
    f"{VERSION_HEADER}, {BUILD_HEADER}, {COMPAT_HEADER}, {EXIT_HEADER}, ETag, Retry-After, Allow,"
    " Graph-Cli-Content-Hash, Graph-Cli-Media-Type"
)
_BODY_KEYS: Final = frozenset({"args", "actor", "pointer", "graph_env"})
_SKILL_KEYS: Final = frozenset({"actor", "graph_env"})
_UPLOAD_KEYS: Final = frozenset({"upload", "name", "origin"})
_DIRECTORY_KEYS: Final = frozenset({"files", "name"})
MAX_DIRECTORY_FILES: Final = 1000  # files of one directory parameter (admin API)
STDIN: Final = "-"  # the CLI's stdin argument; the server has no stdin of the caller
# Plain string parameters whose "-" means stdin (file parameters take uploads only).
STDIN_ARGS: Final[Mapping[tuple[str, ...], tuple[str, ...]]] = {("query",): ("text",)}

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


class AdminRoutes(Protocol):
    """The admin API (``graph_cli_server.admin_api``): ``/v1/admin/<parts>`` for a token
    with the admin scope. Plugged in by ``main`` when ``[server] admin_api`` is on, so this
    module never imports the maintenance distribution."""

    def dispatch(
        self, environ: Mapping[str, Any], method: str, parts: list[str], token: TokenRecord
    ) -> Response: ...


class EnrollRoutes(Protocol):
    """``POST /v1/enroll`` (``graph_cli_server.enroll``): an enrollment code exchanged for a
    token, without a bearer token. Plugged in by ``main``, like the admin API."""

    def dispatch(self, environ: Mapping[str, Any]) -> Response: ...


@dataclass(frozen=True)
class Settings:
    """What the server reads once at start: the environment, roots and limits."""

    env: Mapping[str, str]
    cwd: Path
    data_root: Path
    state_root: Path
    cors_origins: frozenset[str]
    max_upload_bytes: int
    admin_api: bool = False
    # "exact": another build of a client is refused; "warn": let through; "compatible":
    # refused when its compatibility id differs too
    client_builds: str = "compatible"
    dist: bool = True  # [server] dist: hand out the programs of dist_dir
    dist_dir: Path | None = None  # GRAPH_CLI_DIST_DIR: the wheels the image was built from
    public_url: str | None = None  # [server] public_url: base of problem types, OpenAPI servers

    @classmethod
    def load(cls, env: Mapping[str, str], cwd: Path) -> Settings:
        config, _ = load_config(config_path(env))
        if config.mode == "remote":
            raise GraphCliError(
                "config.invalid",
                f"{config.path}: runtime.mode is 'remote'; the server owns the data and runs"
                " with mode 'local'",
                details={"file": str(config.path), "key": "runtime.mode"},
            )
        roots, _ = resolve_roots(env, config=config, cwd=cwd, data_root_flag=None, allow_flag=False)
        return cls(
            env=dict(env),
            cwd=cwd,
            data_root=roots.data_root,
            state_root=roots.state_root,
            cors_origins=frozenset(config.cors_origins),
            max_upload_bytes=config.max_file_bytes,
            admin_api=config.admin_api,
            client_builds=config.client_builds,
            dist=config.dist,
            dist_dir=Path(env[DIST_ENV]) if env.get(DIST_ENV) else None,
            public_url=config.public_url,
        )


@dataclass
class Response:
    status: int
    body: Iterable[bytes]
    headers: list[tuple[str, str]] = field(default_factory=list)
    content_type: str = "application/json"
    failed: Mapping[str, JsonValue] | None = None  # the envelope of an error answer


class MethodNotAllowed(GraphCliError):
    """A route called with another method than its own: 405 with ``Allow`` (B14.2)."""

    def __init__(self, method: str, allowed: str) -> None:
        super().__init__(
            "usage.invalid_argument",
            f"method {method} is not allowed here; use {allowed}",
            details={"argument": "method", "expected": allowed},
        )
        self.allowed = allowed


class ServerApp:
    def __init__(
        self,
        settings: Settings,
        *,
        log: IO[str],
        clock: Clock | None = None,
        admin: Callable[[ServerApp], AdminRoutes] | None = None,
        builds: Mapping[str, str | None] | None = None,
        compats: Mapping[str, str | None] | None = None,
        enroll: Callable[[ServerApp], EnrollRoutes] | None = None,
    ) -> None:
        self.settings = settings
        # Program to the build id the server runs (the admin API adds graph-admin's);
        # None: unknown, so nothing is compared. ``builds`` replaces them in tests.
        self.builds: dict[str, str | None] = {PROGRAM: read_build(graph_cli.__file__)}
        self.builds.update(builds or {})
        # Program to its compatibility id, computed for a known build (``compats`` in tests).
        self.compats: dict[str, str | None] = dict(compats or {})
        self.add_compat(PROGRAM, app_compat)
        self.log = log
        self.clock = clock or SystemClock()
        self.program: click.Group = program()
        self.specs: dict[str, CommandSpec] = {
            "/".join(spec.path): spec for spec in all_specs() if spec.remote != "client"
        }
        self.tokens = TokenStore(settings.data_root)
        self.uploads = Uploads(settings.state_root)
        self.admin = admin(self) if admin is not None else None
        self.enroll = enroll(self) if enroll is not None else None
        self.dist: Served | None = None
        self.dist_problems: list[str] = []  # what is not handed out, and why; main logs it
        if settings.dist and settings.dist_dir is not None:
            self.dist, self.dist_problems = load(settings.dist_dir, self.builds)

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        started = time.perf_counter()
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = _path(environ)
        name: list[str] = []
        try:
            response = self._dispatch(environ, method, path, name)
        except MethodNotAllowed as exc:
            response = self._error(None, exc, status=405)
            response.headers.append(("Allow", exc.allowed))
        except GraphCliError as exc:
            response = self._error(None, exc)
        except Exception:  # a request never takes the server down or leaks a trace
            response = self._error(None, GraphCliError("internal.error", "unexpected error"))
        vary: list[str] = []
        if response.failed is not None:
            response = self._failure(environ, response)
            vary.append("Accept")
        cors = self._cors(environ)
        if self.settings.cors_origins:  # an answer differs by Origin, also for no or another one
            vary.append("Origin")
        own_cache = any(key.lower() == "cache-control" for key, _ in response.headers)
        headers = [
            ("Content-Type", response.content_type),
            (VERSION_HEADER, __version__),
            *self._build_header(environ),
            *([] if own_cache else [("Cache-Control", "no-store")]),
            ("X-Content-Type-Options", "nosniff"),
            *response.headers,
            *cors,
            *([("Vary", ", ".join(vary))] if vary else []),
        ]
        reason = HTTPStatus(response.status).phrase
        start_response(f"{response.status} {reason}", headers)
        millis = int((time.perf_counter() - started) * 1000)
        stamp = self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        who = name[0] if name else "-"
        shown = _loggable(method), _loggable(path)
        self.log.write(
            f"{SERVER_PROGRAM} {stamp} {shown[0]} {shown[1]} {response.status} {millis}ms"
            f" token={who}\n"
        )
        self.log.flush()
        return response.body

    # ---- routing -------------------------------------------------------

    def _dispatch(
        self, environ: Mapping[str, Any], method: str, path: str, name: list[str]
    ) -> Response:
        parts = path.strip("/").split("/")
        if parts[:1] != ["v1"]:
            return self._unknown(path)
        if method == "OPTIONS":
            return self._preflight(environ)
        if parts == ["v1", "health"]:
            self._method(method, "GET")
            return _json(200, {"ok": True, "version": __version__, "api": API_VERSION})
        if parts[1:2] == ["errors"] and len(parts) <= 3:
            self._method(method, "GET")
            return self._errors(path, parts[2:])
        if parts[1:2] == ["dist"] and len(parts) == 3:
            self._method(method, "GET")
            return self._dist(environ, parts[2], name)
        if parts == ["v1", "enroll"]:
            if self.enroll is None:
                return self._unknown(path)
            self._method(method, "POST")
            return self.enroll.dispatch(environ)
        token = self._authenticate(environ)
        name.append(token.name)
        outdated = self._outdated(environ)
        if outdated is not None:
            return self._error(None, outdated)
        if parts == ["v1", "uploads"]:
            self._method(method, "PUT")
            return self._upload(environ, token)
        if parts[1:2] == ["admin"]:
            if self.admin is None:
                return self._admin_off(path)
            refused = _scope_refusal(token, ADMIN_SCOPE)
            if refused is not None:
                return self._error(None, refused, status=403)
            return self.admin.dispatch(environ, method, parts[2:], token)
        refused = _scope_refusal(token, APP_SCOPE)
        if refused is not None:
            return self._error(None, refused, status=403)
        if parts == ["v1", "inventory"]:
            self._method(method, "GET")
            return _json(200, self._inventory())
        if parts == ["v1", "openapi.json"]:
            self._method(method, "GET")
            document = openapi(
                inventory(all_specs(), output_schemas=True),
                version=__version__,
                public_url=self.settings.public_url,
                dist=self.dist is not None,
                enroll=self.enroll is not None,
            )
            return _json(200, document)
        if len(parts) > 2 and parts[1] == "commands":
            spec = self.specs.get("/".join(parts[2:]))
            if spec is None:
                return self._unknown(path)
            self._method(method, "POST")
            render = "text" if "render=text" in str(environ.get("QUERY_STRING", "")) else "json"
            return self._command(environ, spec, token, render)
        if len(parts) == 5 and parts[1] == "graphs" and parts[3] == "files":
            self._method(method, "GET")
            return self._file(environ, parts[2], parts[4], token)
        if len(parts) == 4 and parts[1] == "graphs" and parts[3] == "skill":
            self._method(method, "POST")
            return self._skill(environ, parts[2], token)
        return self._unknown(path)

    def _method(self, method: str, allowed: str) -> None:
        if method != allowed:
            raise MethodNotAllowed(method, allowed)

    def _unknown(self, path: str) -> Response:
        error = GraphCliError("usage.unknown_command", f"no such endpoint: {path}")
        return self._error(None, error, status=404)

    def _admin_off(self, path: str) -> Response:
        error = GraphCliError(
            "usage.unknown_command",
            f"no such endpoint: {path}; the admin API is off on this server"
            " ([server] admin_api = true turns it on)",
        )
        return self._error(None, error, status=404)

    def _authenticate(self, environ: Mapping[str, Any]) -> TokenRecord:
        header = str(environ.get("HTTP_AUTHORIZATION", ""))
        scheme, _, secret = header.partition(" ")
        found = None
        if scheme.lower() == "bearer" and secret.strip():
            found = self.tokens.lookup(secret.strip(), self.clock.now())
        if found is None:
            raise GraphCliError(
                "remote.unauthorized",
                "the server does not accept this token (missing, unknown, revoked or expired);"
                " set GRAPH_CLI_TOKEN or [remote] token_file, and ask the maintainer for a token",
            )
        return found

    def add_compat(self, program: str, compute: Callable[[], str]) -> None:
        """The compatibility id of ``program`` (B14.16), once, when its build is known."""
        if program not in self.compats:
            self.compats[program] = compute() if self.builds.get(program) is not None else None

    def _outdated(self, environ: Mapping[str, Any]) -> GraphCliError | None:
        """``remote.client_outdated`` for a client that names another build of its program
        than the server runs, and in ``compatible`` mode not the server's compatibility id
        of it either; nothing when either build is unknown or in ``warn`` mode."""
        named = parse_build_header(environ.get("HTTP_GRAPH_CLI_BUILD"))
        mode = self.settings.client_builds
        if named is None or mode == "warn":
            return None
        client, build = named
        ours = self.builds.get(client)
        if ours is None or ours == build:
            return None
        claimed = parse_build_header(environ.get("HTTP_GRAPH_CLI_COMPAT"))
        theirs = claimed[1] if claimed is not None and claimed[0] == client else None
        compat = self.compats.get(client)
        if mode == "compatible" and compat is not None and theirs == compat:
            return None
        details: dict[str, JsonValue] = {"program": client, "client": build, "server": ours}
        if compat is not None and theirs is not None:
            details |= {"client_compat": theirs, "server_compat": compat}
        return GraphCliError(
            "remote.client_outdated",
            f"this {client} is build {short(build)}, the server runs build {short(ours)};"
            f" run {client} self update, then the command again",
            details=details,
        )

    def _build_header(self, environ: Mapping[str, Any]) -> list[tuple[str, str]]:
        """The server's build and compatibility id of the program the request named
        (default ``graph``)."""
        named = parse_build_header(environ.get("HTTP_GRAPH_CLI_BUILD"))
        client = named[0] if named is not None and named[0] in self.builds else PROGRAM
        ours = self.builds.get(client)
        if ours is None:
            return []
        compat = self.compats.get(client)
        found = [(BUILD_HEADER, build_header(client, ours))]
        return found if compat is None else [*found, (COMPAT_HEADER, build_header(client, compat))]

    def _inventory(self) -> dict[str, JsonValue]:
        return inventory(all_specs())

    def _errors(self, path: str, code: list[str]) -> Response:
        """The error registry, or one code of it (B14.22 G9); no token, like health."""
        if not code:
            return _json(200, {"errors": registry()})
        found = lookup(code[0])
        return self._unknown(path) if found is None else _json(200, found)

    # ---- commands ------------------------------------------------------

    def _command(
        self, environ: Mapping[str, Any], spec: CommandSpec, token: TokenRecord, render: str
    ) -> Response:
        command = " ".join(spec.path)
        try:
            body = _object(_read_json(environ), "body", _BODY_KEYS)
        except GraphCliError as exc:
            return self._error(command, exc)
        claims = parse_claims_header(environ.get("HTTP_GRAPH_CLI_SKILLS"))
        key = environ.get("HTTP_IDEMPOTENCY_KEY")
        outcome = self.run_command(spec, body, token, claims, None if key is None else str(key))
        if spec.path == ("file", "path"):
            outcome = _without_server_path(outcome)
        return answer(outcome, render)

    def run_command(
        self,
        spec: CommandSpec,
        body: Mapping[str, JsonValue],
        token: TokenRecord,
        claims: tuple[SkillClaim, ...] = (),
        idempotency_key: str | None = None,
    ) -> Outcome:
        """Run one command for a request; every error becomes an envelope. ``claims`` are
        the graph skills the client has installed (B14.17), ``idempotency_key`` the value
        of the ``Idempotency-Key`` header."""
        command = " ".join(spec.path)
        given = body.get("args", {})
        staged: list[Staged] = []
        try:
            if not isinstance(given, Mapping):
                raise GraphCliError(
                    "usage.invalid_input", "args must be an object", details={"argument": "args"}
                )
            args = inventory_args(spec, given)
            if idempotency_key is not None:
                args = with_header_key(spec, args, idempotency_key)
            for name in STDIN_ARGS.get(spec.path, ()):
                if args.get(name) == STDIN:
                    raise GraphCliError(
                        "usage.invalid_argument",
                        f"{name}: - (stdin) is not available on the server; upload the input"
                        " and pass it as a file parameter",
                        details={"argument": name},
                    )
            scope = self._scope(body, token)
            with self.uploads.request_dir(token.name) as directory:
                argv = to_argv(spec, args, self.stager(token, directory, staged))
                origins = {s.link: s.origin for s in staged}
                done = self._run(argv, RequestScope(**scope, origins=origins, claims=claims))
        except GraphCliError as exc:
            failed = envelope(command, program=PROGRAM, error=exc)
            return Outcome(failed, None, *status_of(failed))
        finally:
            self.release(token, staged)
        document = restore_names(done.document, staged)
        return Outcome(document, done.text, *status_of(document))

    def release(self, token: TokenRecord, staged: list[Staged]) -> None:
        """Remove the uploads a command used (after it ran, whatever its outcome)."""
        for digest in sorted({d for item in staged for d in item.digests}):
            self.uploads.remove(token.name, digest)

    def _scope(self, body: Mapping[str, JsonValue], token: TokenRecord) -> dict[str, Any]:
        asserted = body.get("actor")
        if asserted is not None and not isinstance(asserted, Mapping):
            raise GraphCliError(
                "usage.invalid_input", "actor must be an object", details={"argument": "actor"}
            )
        actor, warnings = request_actor(asserted, token=token, client=CLIENT)
        graph_env = body.get("graph_env")
        if graph_env is not None and not isinstance(graph_env, str):
            raise GraphCliError(
                "usage.invalid_input",
                "graph_env must be a string",
                details={"argument": "graph_env"},
            )
        pointer: PointerFile | None = request_pointer(body.get("pointer"))
        return {
            "actor": actor,
            "warnings": warnings,
            "pointer": pointer,
            "graph_env": graph_env or None,
        }

    def _run(self, argv: list[str], scope: RequestScope) -> Completed:
        timings = Timings()
        services = AppServices(self.settings.env, self.settings.cwd, None, timings, request=scope)
        done: list[Completed] = []

        def after(completed: Completed) -> None:
            done.append(completed)
            services.record_access(
                completed.document,
                json_mode=True,
                stdout_bytes=completed.stdout_bytes,
                spilled_bytes=None,
            )

        out, err = io.StringIO(), io.StringIO()
        invocation = Invocation(PROGRAM, json_default=True)
        run(self.program, argv, invocation, services=services, out=out, err=err, after=after)
        if not done:
            raise GraphCliError("internal.error", "the command gave no answer")
        return done[0]

    def stager(
        self, token: TokenRecord, directory: Path, staged: list[Staged]
    ) -> Callable[[ParamSpec, JsonValue], str]:
        """Turns an upload reference into a path below the request ``directory``: a hard
        link for a file parameter, a tree of them for a directory parameter (admin API)."""

        def stage(param: ParamSpec, value: JsonValue) -> str:
            if param.type == "directory":
                return self._stage_directory(token, directory, staged, param, value)
            reference = _object(value, param.name, _UPLOAD_KEYS)
            digest, name = reference.get("upload"), reference.get("name")
            origin = reference.get("origin")
            if not isinstance(digest, str) or not DIGEST_RE.match(digest):
                raise _bad_upload(param.name, "upload must be sha256:<hex> from PUT /v1/uploads")
            if not isinstance(name, str) or not name:
                raise _bad_upload(param.name, "name must be the argument as given")
            if origin is not None and not (
                isinstance(origin, Mapping)
                and set(origin) == {"root", "path"}
                and all(isinstance(origin[k], str) for k in ("root", "path"))
            ):
                raise _bad_upload(param.name, "origin must be {root, path} or null")
            found = self.uploads.find(token.name, digest)
            if found is None:
                raise _bad_upload(param.name, "no such upload (expired or used); upload it again")
            slot = directory / str(len(staged))
            slot.mkdir()
            link = slot / link_name(name)
            os.link(found, link)
            staged.append(Staged(link, (digest,), name, dict(origin) if origin else None))
            return str(link)

        return stage

    def _stage_directory(
        self,
        token: TokenRecord,
        directory: Path,
        staged: list[Staged],
        param: ParamSpec,
        value: JsonValue,
    ) -> str:
        """``{files: {relative path: upload}, name}``: the directory rebuilt from hard links,
        named like the client's directory. No path may leave it."""
        reference = _object(value, param.name, _DIRECTORY_KEYS)
        files, name = reference.get("files"), reference.get("name")
        if not isinstance(name, str) or not name:
            raise _bad_upload(param.name, "name must be the argument as given")
        if not isinstance(files, Mapping):
            raise _bad_upload(param.name, "files must map relative paths to uploads")
        if len(files) > MAX_DIRECTORY_FILES:
            raise _bad_upload(param.name, f"a directory holds at most {MAX_DIRECTORY_FILES} files")
        sources: dict[PurePosixPath, Path] = {}
        for relative, digest in files.items():
            if not _is_relative(relative):
                raise _bad_upload(param.name, f"{relative!r} is not a relative path")
            if not isinstance(digest, str) or not DIGEST_RE.match(digest):
                raise _bad_upload(param.name, f"{relative}: upload must be sha256:<hex>")
            found = self.uploads.find(token.name, digest)
            if found is None:
                raise _bad_upload(param.name, f"{relative}: no such upload; upload it again")
            sources[PurePosixPath(relative)] = found
        slot = directory / str(len(staged))
        slot.mkdir()
        root = slot / link_name(name)
        root.mkdir()
        digests = tuple(sorted({str(d) for d in files.values()}))
        staged.append(Staged(root, digests, name, None))
        try:
            for posix, source in sorted(sources.items()):
                target = root.joinpath(*posix.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
        except OSError:
            raise _bad_upload(
                param.name, "files name a path twice (as a file and a directory)"
            ) from None
        return str(root)

    # ---- uploads, files, skills ----------------------------------------

    def _upload(self, environ: Mapping[str, Any], token: TokenRecord) -> Response:
        length = _length(environ)
        self.uploads.prune(self.clock.now().timestamp())
        digest, size = self.uploads.store(
            token.name, environ["wsgi.input"], length, self.settings.max_upload_bytes
        )
        return _json(200, {"upload": digest, "size": size})

    def _file(
        self, environ: Mapping[str, Any], graph: str, ref: str, token: TokenRecord
    ) -> Response:
        """``file path`` for the request's actor, then the stored bytes (B14.2)."""
        body: dict[str, JsonValue] = {"args": {"ref": ref, "graph": graph}}
        header = environ.get(ACTOR_HEADER)
        if header:
            body["actor"] = _actor_header(str(header))
        outcome = self.run_command(self.specs["file/path"], body, token)
        if outcome.document.get("ok") is not True:
            return _json(outcome.status, dict(outcome.document), exit_code=outcome.exit_code)
        data = outcome.document["data"]
        assert isinstance(data, Mapping)
        tag = f'"{data.get("hash")}"'
        # the browser may keep the bytes, but asks every time (the read is authorized)
        cached = [("ETag", tag), ("Cache-Control", "private, no-cache")]
        if _matches(environ.get("HTTP_IF_NONE_MATCH"), tag):
            return Response(304, [b""], headers=cached, content_type="application/octet-stream")
        stored = Path(str(data["path"]))
        handle = stored.open("rb")
        wrapper = environ.get("wsgi.file_wrapper")
        chunks: Iterable[bytes] = (
            wrapper(handle, 1024 * 1024) if wrapper is not None else _chunks(handle)
        )
        return Response(
            200,
            chunks,
            headers=[
                ("Content-Length", str(stored.stat().st_size)),
                ("Content-Disposition", "attachment"),
                ("Graph-Cli-Content-Hash", str(data.get("hash"))),
                ("Graph-Cli-Media-Type", str(data.get("media_type") or "")),
                (EXIT_HEADER, str(EXIT_OK)),
                *cached,
            ],
            content_type="application/octet-stream",
        )

    def _skill(self, environ: Mapping[str, Any], graph: str, token: TokenRecord) -> Response:
        """A graph's published skill, rendered as ``graph skills install`` would (B14.2)."""
        from graph_cli.app_cli.commands.skills import graph_skill

        warnings: list[WarningRecord] = []
        try:
            body = _object(_read_json(environ), "body", _SKILL_KEYS)
            scope = self._scope(body, token)
            services = AppServices(
                self.settings.env,
                self.settings.cwd,
                None,
                Timings(),
                request=RequestScope(**scope, origins={}),
            )
            ctx = services.context(warnings, "skills")
            entry = ctx.select(graph)
            skill = graph_skill(ctx, entry, warnings)
        except GraphCliError as exc:
            document = envelope("skills", program=PROGRAM, error=exc, warnings=warnings)
            status, exit_code = status_of(document)
            return _json(status, dict(document), exit_code=exit_code)
        data: dict[str, JsonValue] = {
            "graph": {"id": str(entry.id), "name": entry.name},
            "skill": {
                "name": skill.name,
                "kind": skill.kind,
                "files": {
                    path: base64.b64encode(content).decode()
                    for path, content in sorted(skill.files.items())
                },
                "source": dict(skill.marker.source),
            },
        }
        document = envelope("skills", program=PROGRAM, result=Result(data=data), warnings=warnings)
        return _json(200, dict(document))

    def _dist(self, environ: Mapping[str, Any], file: str, name: list[str]) -> Response:
        """The manifest or one of its files (B14.18); never refused for a build. The
        ``graph-admin`` wheel needs a token with the admin scope."""
        if self.dist is None:
            why = (
                "[server] dist = false"
                if not self.settings.dist
                else f"no wheels ({DIST_ENV} names none)"
            )
            error = GraphCliError(
                "usage.unknown_command",
                f"no such endpoint: /v1/dist/{file}; this server hands out no programs ({why})",
            )
            return self._error(None, error, status=404)
        if file == "manifest":
            return _json(200, self.dist.manifest.to_json())
        found = self.dist.files.get(file)
        if found is None:
            return self._unknown(f"/v1/dist/{file}")
        path, admin = found
        if admin:
            token = self._authenticate(environ)
            name.append(token.name)
            refused = _scope_refusal(token, ADMIN_SCOPE)
            if refused is not None:
                return self._error(None, refused, status=403)
        handle = path.open("rb")
        wrapper = environ.get("wsgi.file_wrapper")
        chunks: Iterable[bytes] = (
            wrapper(handle, 1024 * 1024) if wrapper is not None else _chunks(handle)
        )
        return Response(
            200,
            chunks,
            headers=[
                ("Content-Length", str(path.stat().st_size)),
                ("Content-Disposition", "attachment"),
            ],
            content_type="application/octet-stream",
        )

    # ---- errors and CORS -----------------------------------------------

    def _error(
        self, command: str | None, error: GraphCliError, status: int | None = None
    ) -> Response:
        document = envelope(command, program=PROGRAM, error=error)
        found, exit_code = status_of(document)
        headers = []
        if error.code == "remote.unauthorized":
            challenge = 'Bearer error="insufficient_scope"' if status == 403 else "Bearer"
            headers.append(("WWW-Authenticate", challenge))
        response = _json(status or found, dict(document), exit_code=exit_code)
        response.headers += headers
        return response

    def _failure(self, environ: Mapping[str, Any], response: Response) -> Response:
        """An error answer as the request takes it: ``Retry-After`` on a retryable 503,
        problem details when ``Accept`` asks for them (B14.22 G8, G10)."""
        document = response.failed
        assert document is not None
        error = document.get("error")
        assert isinstance(error, Mapping)
        if response.status == 503 and error.get("retryable") is True:
            response.headers.append(("Retry-After", RETRY_AFTER))
        if not wants_problem(environ.get("HTTP_ACCEPT")):
            return response
        _, exit_code = status_of(document)
        found = problem(document, response.status, exit_code, self.settings.public_url)
        body = render_json(found).encode()
        headers = [h for h in response.headers if h[0] != "Content-Length"]
        headers.append(("Content-Length", str(len(body))))
        return Response(response.status, [body], headers, PROBLEM_TYPE, document)

    def _cors(self, environ: Mapping[str, Any]) -> list[tuple[str, str]]:
        """The CORS headers of an allowed origin; ``Vary: Origin`` is added by the caller."""
        origin = environ.get("HTTP_ORIGIN")
        if not origin or origin not in self.settings.cors_origins:
            return []
        return [
            ("Access-Control-Allow-Origin", str(origin)),
            ("Access-Control-Expose-Headers", CORS_EXPOSED),
        ]

    def _preflight(self, environ: Mapping[str, Any]) -> Response:
        origin = environ.get("HTTP_ORIGIN")
        if not origin or origin not in self.settings.cors_origins:
            return Response(403, [b""], content_type="text/plain")
        return Response(
            204,
            [b""],
            headers=[
                ("Access-Control-Allow-Methods", CORS_METHODS),
                ("Access-Control-Allow-Headers", CORS_HEADERS),
                ("Access-Control-Max-Age", "600"),
            ],
            content_type="text/plain",
        )


@dataclass(frozen=True)
class Outcome:
    """A command's answer on the server: the envelope, the handler's text, status and exit."""

    document: Mapping[str, JsonValue]
    text: str | None
    status: int
    exit_code: int


@dataclass(frozen=True)
class Staged:
    """An upload reference made a path for one command: a file's hard link or a directory
    of them, the uploads it used and the client's argument it stands for."""

    link: Path
    digests: tuple[str, ...]
    name: str
    origin: dict[str, JsonValue] | None


def _scope_refusal(token: TokenRecord, scope: str) -> GraphCliError | None:
    """An app token reaches the app routes only, an admin token the admin routes only
    (403 with ``remote.unauthorized``: the token is valid, its scope is not this one)."""
    if token.scope == scope:
        return None
    reaches = "the admin API (/v1/admin/)" if token.scope == ADMIN_SCOPE else "the app routes"
    return GraphCliError(
        "remote.unauthorized",
        f"token {token.name} has the scope {token.scope} and reaches {reaches} only;"
        f" this endpoint needs a token with the scope {scope}",
        details={"scope": token.scope, "required": scope},
    )


def answer(outcome: Outcome, render: str) -> Response:
    """A command's HTTP answer: the envelope, or with ``render=text`` also its text (an
    error asked for as problem details is the problem only)."""
    if render == "text":
        stdout, stderr = render_text(outcome.document, text=outcome.text)
        payload: dict[str, JsonValue] = {
            "envelope": dict(outcome.document),
            "stdout": stdout,
            "stderr": stderr,
        }
        response = _json(outcome.status, payload, exit_code=outcome.exit_code)
        if outcome.document.get("ok") is False:
            response.failed = outcome.document
        return response
    return _json(outcome.status, dict(outcome.document), exit_code=outcome.exit_code)


def with_header_key(
    spec: CommandSpec, args: Mapping[str, JsonValue], header: str
) -> dict[str, JsonValue]:
    """``args`` with the key of an ``Idempotency-Key`` header (B14.22 G7): an RFC 9651
    String such as ``"<key>"``. A command without the option, or another key in
    ``args``, is ``usage.invalid_argument``."""
    text = header.strip(" \t")
    key = text[1:-1] if len(text) > 2 and text[0] == text[-1] == '"' else ""
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise _bad_key(
            f'{KEY_HEADER} must be a quoted key ("<key>") of 1 to 128 characters of'
            " A-Z a-z 0-9 . _ : -"
        )
    if not any(param.name == KEY_OPTION for param in spec.params):
        raise _bad_key(
            f"{' '.join(spec.path)} takes no idempotency key; send {KEY_HEADER} with a write"
        )
    given = args.get(KEY_OPTION)
    if given not in (None, False) and given != key:
        raise _bad_key(f"{KEY_HEADER} and args name different keys; send the key once")
    return {**args, KEY_OPTION: key}


def _bad_key(message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", message, details={"argument": KEY_HEADER})


def _matches(header: object, tag: str) -> bool:
    """Whether ``If-None-Match`` names ``tag`` (weak comparison, RFC 9110 13.1.2)."""
    if not isinstance(header, str):
        return False
    return any(item.strip() in ("*", tag, "W/" + tag) for item in header.split(","))


def _is_relative(value: str) -> bool:
    """A relative POSIX path that stays below its directory."""
    parts = value.split("/")
    return (
        0 < len(value.encode()) <= 1024
        and "\x00" not in value
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in parts)
    )


def status_of(document: Mapping[str, JsonValue]) -> tuple[int, int]:
    """``(HTTP status, exit code)`` of an envelope (B14.5)."""
    error = document.get("error")
    if not isinstance(error, Mapping):
        return 200, EXIT_OK
    category = ErrorCategory(str(error.get("category")))
    return status_for(str(error.get("code")), category), exit_code_for(category)


def _without_server_path(outcome: Outcome) -> Outcome:
    """``file path`` over the API: the stored copy's path on the server means nothing to a
    caller (``path: null``); the bytes are at ``GET /v1/graphs/{graph}/files/{ref}``."""
    data = outcome.document.get("data")
    if outcome.document.get("ok") is not True or not isinstance(data, Mapping):
        return outcome
    document = {**outcome.document, "data": {**data, "path": None}}
    return Outcome(document, None, outcome.status, outcome.exit_code)


def restore_names(
    document: Mapping[str, JsonValue], staged: list[Staged]
) -> Mapping[str, JsonValue]:
    """Hints and messages name the client's argument, not the staged link."""
    if not staged:
        return document

    def fix(text: str) -> str:
        for item in staged:
            text = text.replace(shlex.quote(str(item.link)), shlex.quote(item.name))
            text = text.replace(str(item.link), item.name)
        return text

    fixed = dict(document)
    hints = fixed.get("hints")
    if isinstance(hints, list):
        fixed["hints"] = [
            {**h, "command": fix(h["command"])}
            if isinstance(h, Mapping) and isinstance(h.get("command"), str)
            else h
            for h in hints
        ]
    error = fixed.get("error")
    if isinstance(error, Mapping):
        repaired = dict(error)
        for key in ("hint", "message"):
            if isinstance(repaired.get(key), str):
                repaired[key] = fix(str(repaired[key]))
        fixed["error"] = repaired
    return fixed


def _loggable(text: str) -> str:
    """One log field: no control characters or spaces (no forged lines), at most 200
    characters."""
    escaped = "".join(
        c if c.isprintable() and not c.isspace() else f"\\x{ord(c):02x}" for c in text
    )
    return escaped[:200]


def _json(
    status: int, document: Mapping[str, JsonValue], *, exit_code: int | None = None
) -> Response:
    body = render_json(document).encode()
    headers = [("Content-Length", str(len(body)))]
    if exit_code is not None:
        headers.append((EXIT_HEADER, str(exit_code)))
    failed = document if document.get("schema") == ENVELOPE_SCHEMA and not document["ok"] else None
    return Response(status, [body], headers=headers, failed=failed)


def _path(environ: Mapping[str, Any]) -> str:
    raw = str(environ.get("PATH_INFO", "") or "/")
    try:
        return raw.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return raw


def _length(environ: Mapping[str, Any]) -> int:
    """The declared body size; the upload store checks it against the limit."""
    text = str(environ.get("CONTENT_LENGTH") or "")
    if not text.isdigit():
        raise GraphCliError(
            "usage.invalid_input",
            "the request needs a Content-Length",
            details={"argument": "body"},
        )
    return int(text)


def _read_json(environ: Mapping[str, Any]) -> JsonValue:
    text = str(environ.get("CONTENT_LENGTH") or "0")
    length = int(text) if text.isdigit() else 0
    if length > MAX_BODY_BYTES:
        raise GraphCliError(
            "usage.invalid_input",
            f"the body is larger than {MAX_BODY_BYTES} bytes",
            details={"argument": "body", "max_bytes": MAX_BODY_BYTES},
        )
    data = environ["wsgi.input"].read(length) if length else b"{}"
    try:
        value = parse_json(data, floats=FloatLiteral)
    except CanonicalError as exc:
        raise GraphCliError(
            "usage.invalid_input",
            f"the body is not valid JSON: {exc}",
            details={"argument": "body"},
        ) from None
    # B14.22 G6: a number with a fraction reaches a command as written (``1.10``); elsewhere
    # it stays a number, which the readers refuse by its type as before
    if isinstance(value, Mapping):
        return {key: item if key == "args" else float_numbers(item) for key, item in value.items()}
    return float_numbers(value)  # type: ignore[no-any-return]


def _object(value: JsonValue, what: str, keys: frozenset[str]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise GraphCliError(
            "usage.invalid_input", f"{what} must be a JSON object", details={"argument": what}
        )
    unknown = sorted(set(value) - keys)
    if unknown:
        raise GraphCliError(
            "usage.invalid_input",
            f"{what} has unknown keys: {', '.join(unknown)}",
            details={"argument": what},
        )
    return value


def _actor_header(value: str) -> JsonValue:
    """Parsed as strictly as a body (a duplicate key refused)."""
    try:
        decoded = parse_json(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, CanonicalError):
        raise GraphCliError(
            "usage.invalid_input",
            "Graph-Cli-Actor must be base64url of a JSON object",
            details={"argument": "actor"},
        ) from None
    return decoded


def _bad_upload(name: str, message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", f"{name}: {message}", details={"argument": name})


def _chunks(handle: IO[bytes]) -> Iterable[bytes]:
    with contextlib.closing(handle):
        while chunk := handle.read(1024 * 1024):
            yield chunk
