"""The WSGI application of ``graph-server`` (contracts B14).

Routes are generated from the ``graph`` declarations: every command the
server runs (``remote`` is ``server`` or ``hooked``) is ``POST
/v1/commands/<path>``; its JSON parameters become argv for the same click
program, run by ``cli_kit.runner.run`` with one ``AppServices`` per request
(the request's actor, pointer, ``GRAPH_CLI_GRAPH`` and file origins). Every
answer is the envelope; the HTTP status follows the error category. A
request without a valid bearer token reaches no service and is only logged.
No body, token or query string is ever logged.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import json
import os
import shlex
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import IO, Any, Final

import click

from graph_cli import __version__
from graph_cli.app_cli.commands import all_specs
from graph_cli.app_cli.commands.meta import inventory
from graph_cli.app_cli.main import program
from graph_cli.catalog.config import load_config
from graph_cli.catalog.identity.request import request_actor
from graph_cli.catalog.pointer import PointerFile, request_pointer
from graph_cli.catalog.resolve import config_path, resolve_roots
from graph_cli.catalog.tokens import TokenRecord, TokenStore
from graph_cli.catalog.wiring import PROGRAM, AppServices, RequestScope
from graph_cli.cli_kit.argv import to_argv
from graph_cli.cli_kit.envelope import Result, envelope, render_json, render_text
from graph_cli.cli_kit.exit_codes import EXIT_OK, exit_code_for
from graph_cli.cli_kit.runner import Completed, Invocation, run
from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.timing import Timings
from graph_cli_server.openapi import API_VERSION, openapi
from graph_cli_server.uploads import DIGEST_RE, Uploads, link_name

SERVER_PROGRAM: Final = "graph-server"
MAX_BODY_BYTES: Final = 16 * 1024 * 1024
CLIENT: Final = ClientInfo(SERVER_PROGRAM, __version__, Entrypoint.SERVER)
VERSION_HEADER: Final = "Graph-Cli-Version"
EXIT_HEADER: Final = "Graph-Cli-Exit-Code"
ACTOR_HEADER: Final = "HTTP_GRAPH_CLI_ACTOR"
STATUS_BY_CATEGORY: Final[Mapping[ErrorCategory, int]] = {
    ErrorCategory.USAGE: 400,
    ErrorCategory.DENIED: 403,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.CONFLICT: 409,
    ErrorCategory.REFUSED: 422,
    ErrorCategory.INVALID: 422,
    ErrorCategory.UNAVAILABLE: 503,
    ErrorCategory.INTERNAL: 500,
}
CORS_METHODS: Final = "GET, POST, PUT"
CORS_HEADERS: Final = "Authorization, Content-Type, Graph-Cli-Version, Graph-Cli-Actor"
_BODY_KEYS: Final = frozenset({"args", "actor", "pointer", "graph_env"})
_SKILL_KEYS: Final = frozenset({"actor", "graph_env"})
_UPLOAD_KEYS: Final = frozenset({"upload", "name", "origin"})
STDIN: Final = "-"  # the CLI's stdin argument; the server has no stdin of the caller
# Plain string parameters whose "-" means stdin (file parameters take uploads only).
STDIN_ARGS: Final[Mapping[tuple[str, ...], tuple[str, ...]]] = {("query",): ("text",)}

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


@dataclass(frozen=True)
class Settings:
    """What the server reads once at start: the environment, roots and limits."""

    env: Mapping[str, str]
    cwd: Path
    data_root: Path
    state_root: Path
    cors_origins: frozenset[str]
    max_upload_bytes: int

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
        )


@dataclass
class Response:
    status: int
    body: Iterable[bytes]
    headers: list[tuple[str, str]] = field(default_factory=list)
    content_type: str = "application/json"


class ServerApp:
    def __init__(self, settings: Settings, *, log: IO[str], clock: Clock | None = None) -> None:
        self.settings = settings
        self.log = log
        self.clock = clock or SystemClock()
        self.program: click.Group = program()
        self.specs: dict[str, CommandSpec] = {
            "/".join(spec.path): spec for spec in all_specs() if spec.remote != "client"
        }
        self.tokens = TokenStore(settings.data_root)
        self.uploads = Uploads(settings.state_root)

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        started = time.perf_counter()
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = _path(environ)
        name: list[str] = []
        try:
            response = self._dispatch(environ, method, path, name)
        except GraphCliError as exc:
            response = self._error(None, exc)
        except Exception:  # a request never takes the server down or leaks a trace
            response = self._error(None, GraphCliError("internal.error", "unexpected error"))
        headers = [
            ("Content-Type", response.content_type),
            (VERSION_HEADER, __version__),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            *response.headers,
            *self._cors(environ),
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
        token = self._authenticate(environ)
        name.append(token.name)
        if parts == ["v1", "inventory"]:
            self._method(method, "GET")
            return _json(200, self._inventory())
        if parts == ["v1", "openapi.json"]:
            self._method(method, "GET")
            return _json(200, openapi(self._inventory(), version=__version__))
        if parts == ["v1", "uploads"]:
            self._method(method, "PUT")
            return self._upload(environ, token)
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
            raise GraphCliError(
                "usage.invalid_argument",
                f"method {method} is not allowed here; use {allowed}",
                details={"argument": "method", "expected": allowed},
            )

    def _unknown(self, path: str) -> Response:
        error = GraphCliError("usage.unknown_command", f"no such endpoint: {path}")
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

    def _inventory(self) -> dict[str, JsonValue]:
        return inventory(all_specs())

    # ---- commands ------------------------------------------------------

    def _command(
        self, environ: Mapping[str, Any], spec: CommandSpec, token: TokenRecord, render: str
    ) -> Response:
        command = " ".join(spec.path)
        try:
            body = _object(_read_json(environ), "body", _BODY_KEYS)
        except GraphCliError as exc:
            return self._error(command, exc)
        outcome = self.run_command(spec, body, token)
        if spec.path == ("file", "path"):
            outcome = _without_server_path(outcome)
        if render == "text":
            stdout, stderr = render_text(outcome.document, text=outcome.text)
            payload: dict[str, JsonValue] = {
                "envelope": dict(outcome.document),
                "stdout": stdout,
                "stderr": stderr,
            }
            return _json(outcome.status, payload, exit_code=outcome.exit_code)
        return _json(outcome.status, dict(outcome.document), exit_code=outcome.exit_code)

    def run_command(
        self, spec: CommandSpec, body: Mapping[str, JsonValue], token: TokenRecord
    ) -> Outcome:
        """Run one command for a request; every error becomes an envelope."""
        command = " ".join(spec.path)
        args = body.get("args", {})
        staged: list[_Staged] = []
        try:
            if not isinstance(args, Mapping):
                raise GraphCliError(
                    "usage.invalid_input", "args must be an object", details={"argument": "args"}
                )
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
                argv = to_argv(spec, args, self._stager(token, directory, staged))
                origins = {s.link: s.origin for s in staged}
                done = self._run(argv, RequestScope(**scope, origins=origins))
        except GraphCliError as exc:
            failed = envelope(command, program=PROGRAM, error=exc)
            return Outcome(failed, None, *_status_of(failed))
        finally:
            for item in staged:
                self.uploads.remove(token.name, item.digest)
        document = _restore_names(done.document, staged)
        return Outcome(document, done.text, *_status_of(document))

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

    def _stager(
        self, token: TokenRecord, directory: Path, staged: list[_Staged]
    ) -> Callable[[ParamSpec, JsonValue], str]:
        def stage(param: ParamSpec, value: JsonValue) -> str:
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
            staged.append(_Staged(link, digest, name, dict(origin) if origin else None))
            return str(link)

        return stage

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
        body: dict[str, JsonValue] = {"args": {"ref": ref, "--graph": graph}}
        header = environ.get(ACTOR_HEADER)
        if header:
            body["actor"] = _actor_header(str(header))
        outcome = self.run_command(self.specs["file/path"], body, token)
        if outcome.document.get("ok") is not True:
            return _json(outcome.status, dict(outcome.document), exit_code=outcome.exit_code)
        data = outcome.document["data"]
        assert isinstance(data, Mapping)
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
            status, exit_code = _status_of(document)
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

    # ---- errors and CORS -----------------------------------------------

    def _error(
        self, command: str | None, error: GraphCliError, status: int | None = None
    ) -> Response:
        document = envelope(command, program=PROGRAM, error=error)
        found, exit_code = _status_of(document)
        headers = [("WWW-Authenticate", "Bearer")] if error.code == "remote.unauthorized" else []
        response = _json(status or found, dict(document), exit_code=exit_code)
        response.headers += headers
        return response

    def _cors(self, environ: Mapping[str, Any]) -> list[tuple[str, str]]:
        origin = environ.get("HTTP_ORIGIN")
        if not origin or origin not in self.settings.cors_origins:
            return []
        return [
            ("Access-Control-Allow-Origin", str(origin)),
            ("Vary", "Origin"),
            ("Access-Control-Expose-Headers", f"{VERSION_HEADER}, {EXIT_HEADER}"),
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
class _Staged:
    link: Path
    digest: str
    name: str
    origin: dict[str, JsonValue] | None


def _status_of(document: Mapping[str, JsonValue]) -> tuple[int, int]:
    """``(HTTP status, exit code)`` of an envelope (B14.5)."""
    error = document.get("error")
    if not isinstance(error, Mapping):
        return 200, EXIT_OK
    category = ErrorCategory(str(error.get("category")))
    status = STATUS_BY_CATEGORY[category]
    if error.get("code") == "remote.unauthorized":
        status = 401
    return status, exit_code_for(category)


def _without_server_path(outcome: Outcome) -> Outcome:
    """``file path`` over the API: the stored copy's path on the server means nothing to a
    caller (``path: null``); the bytes are at ``GET /v1/graphs/{graph}/files/{ref}``."""
    data = outcome.document.get("data")
    if outcome.document.get("ok") is not True or not isinstance(data, Mapping):
        return outcome
    document = {**outcome.document, "data": {**data, "path": None}}
    return Outcome(document, None, outcome.status, outcome.exit_code)


def _restore_names(
    document: Mapping[str, JsonValue], staged: list[_Staged]
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
    return Response(status, [body], headers=headers)


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
        value: JsonValue = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphCliError(
            "usage.invalid_input", f"the body is not JSON: {exc}", details={"argument": "body"}
        ) from None
    return value


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
    try:
        decoded: JsonValue = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
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
