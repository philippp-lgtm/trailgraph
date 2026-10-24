"""The remote client's transport to graph-server (contracts B14.10).

Stdlib ``http.client`` with ``ssl.create_default_context()``, imported only
in remote mode. A failed connect sends nothing: ``remote.unreachable``. A
connection lost after the request was sent is ``remote.outcome_unknown``
for a write (it may have been applied; the idempotency key makes running
it again safe) and one retry for a read. 401 is ``remote.unauthorized``;
an answer that is not the API's is ``remote.protocol``. The token goes into
the ``Authorization`` header only, never into an error.
"""

from __future__ import annotations

import http.client
import json
import shutil
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from graph_cli.catalog.endpoint import Endpoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord

ENVELOPE_SCHEMA: Final = "graph_cli.envelope/1"
VERSION_HEADER: Final = "Graph-Cli-Version"
CHUNK: Final = 1024 * 1024
APP_TOKEN_HINT: Final = (
    "set GRAPH_CLI_TOKEN or [remote] token_file, and ask the maintainer for a token"
)
COMMANDS_ROUTE: Final = "/v1/commands/"


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes

    def json(self) -> JsonValue:
        try:
            value: JsonValue = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise protocol(self.status, "the answer is not JSON") from None
        return value


@dataclass(frozen=True, slots=True)
class Write:
    """What ``remote.outcome_unknown`` names: the key a write was sent with and the
    command line that repeats it with that key."""

    idempotency_key: str | None
    argv: Sequence[str]


class RemoteClient:
    def __init__(
        self,
        endpoint: Endpoint,
        token: str,
        *,
        timeout_s: float,
        version: str,
        token_hint: str = APP_TOKEN_HINT,
    ) -> None:
        self.endpoint = endpoint
        self._token = token
        self.timeout_s = timeout_s
        self.version = version
        self.token_hint = token_hint  # where this program's token comes from

    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | BinaryIO | None = None,
        length: int | None = None,
        headers: Mapping[str, str] | None = None,
        write: Write | None = None,
        sink: BinaryIO | None = None,
    ) -> Answer:
        """One request; ``write`` makes a lost answer ``outcome_unknown`` instead of a retry.
        A ``sink`` receives the body of a 200 answer instead of ``Answer.body``."""
        attempts = 1 if write is not None else 2
        for attempt in range(attempts):
            connection = self._connection()
            try:
                try:
                    connection.connect()
                except OSError as exc:
                    if attempt + 1 < attempts:
                        continue
                    raise self._unreachable(exc) from None
                if hasattr(body, "seek"):
                    body.seek(0)  # type: ignore[union-attr]
                try:
                    connection.request(
                        method,
                        self.endpoint.path + path,
                        body=body,
                        headers=self._headers(headers, body, length),
                    )
                    response = connection.getresponse()
                    found = {k.lower(): v for k, v in response.getheaders()}
                    if sink is not None and response.status == 200:
                        shutil.copyfileobj(response, sink, CHUNK)
                        data = b""
                    else:
                        data = response.read()
                except (OSError, http.client.HTTPException) as exc:
                    if write is not None:
                        raise outcome_unknown(write) from None
                    if attempt + 1 < attempts:
                        continue
                    raise self._unreachable(exc) from None
            finally:
                connection.close()
            if response.status == 401:
                raise unauthorized(self.token_hint)
            return Answer(response.status, found, data)
        raise GraphCliError("internal.error", "no attempt was made")

    def version_warning(self, answer: Answer) -> WarningRecord | None:
        server = answer.headers.get(VERSION_HEADER.lower())
        if server is None or server == self.version:
            return None
        return WarningRecord(
            "remote.version_differs",
            f"the server runs {server}, this program {self.version}; commands and answers"
            " may differ",
            {"client": self.version, "server": server},
        )

    def _connection(self) -> http.client.HTTPConnection:
        endpoint = self.endpoint
        if endpoint.scheme == "https":
            return http.client.HTTPSConnection(
                endpoint.host,
                endpoint.port,
                timeout=self.timeout_s,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=self.timeout_s)

    def _headers(
        self,
        extra: Mapping[str, str] | None,
        body: bytes | BinaryIO | None,
        length: int | None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            VERSION_HEADER: self.version,
            "Accept": "application/json",
            **(extra or {}),
        }
        if isinstance(body, bytes):
            headers.setdefault("Content-Type", "application/json")
            headers["Content-Length"] = str(len(body))
        elif body is not None and length is not None:
            headers["Content-Length"] = str(length)
        return headers

    def _unreachable(self, exc: BaseException) -> GraphCliError:
        reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        return GraphCliError(
            "remote.unreachable",
            f"cannot reach the server at {self.endpoint.display}: {reason}",
            details={"endpoint": self.endpoint.display},
        )


def envelope_of(answer: Answer) -> Mapping[str, JsonValue]:
    """The envelope of a command answer, else ``remote.protocol``."""
    value = answer.json()
    if not isinstance(value, Mapping) or value.get("schema") != ENVELOPE_SCHEMA:
        raise protocol(answer.status, "the answer is not a graph_cli envelope")
    return value


def unauthorized(hint: str = APP_TOKEN_HINT) -> GraphCliError:
    return GraphCliError(
        "remote.unauthorized",
        f"the server does not accept the token (missing, unknown, revoked or expired); {hint}",
    )


def read_token(given: str | None, file: Path | None, *, config: Path, key: str, hint: str) -> str:
    """The token of the environment variable, else of the token file, which nobody else
    may read; ``remote.unauthorized`` without one. ``key`` names the file's config key."""
    if given and given.strip():
        return given.strip()
    if file is None:
        raise unauthorized(hint)
    try:
        if file.stat().st_mode & 0o077:
            raise GraphCliError(
                "config.invalid",
                f"{config}: {key} {file} may be read by others; chmod 600 {file}",
                details={"file": str(config), "key": key},
            )
        token = file.read_text().strip()
    except OSError as exc:
        raise GraphCliError(
            "config.invalid",
            f"{config}: {key} cannot be read: {exc.strerror or exc}",
            details={"file": str(config), "key": key},
        ) from exc
    if not token:
        raise unauthorized(hint)
    return token


def protocol(status: int, why: str) -> GraphCliError:
    return GraphCliError(
        "remote.protocol",
        f"the server's answer (HTTP {status}) cannot be used: {why}",
        details={"status": status},
    )


def outcome_unknown(write: Write) -> GraphCliError:
    key = write.idempotency_key
    if key is None:
        message = (
            "the connection was lost after the write was sent; it may have been applied."
            " Check the graph before running it again"
        )
    else:
        message = (
            "the connection was lost after the write was sent; it may have been applied."
            f" Running it again with --idempotency-key {key} is safe"
        )
    return GraphCliError(
        "remote.outcome_unknown",
        message,
        details={"idempotency_key": key, "argv": list(write.argv) if key is not None else None},
    )


@dataclass(frozen=True)
class RemoteSession:
    """One command's link to the server: the client and what every request carries
    (the asserted facets, the pointer, ``GRAPH_CLI_GRAPH``); built by the wiring."""

    client: RemoteClient
    actor: Mapping[str, JsonValue]
    pointer: Mapping[str, JsonValue] | None
    graph_env: str | None
    warnings: list[WarningRecord]  # the running command's warnings
    commands: str = COMMANDS_ROUTE  # the route of the commands; graph-admin's are /v1/admin/

    def command(
        self,
        path: Sequence[str],
        args: Mapping[str, JsonValue],
        *,
        text: bool,
        write: Write | None,
    ) -> tuple[Mapping[str, JsonValue], str | None, str | None]:
        """``(envelope, stdout, stderr)`` of a command run by the server; the text only
        with ``text`` and when the server rendered it (a refusal before the command, such
        as a token of the other scope, comes as a bare envelope)."""
        body = self.body(args=dict(args))
        route = self.commands + "/".join(path) + ("?render=text" if text else "")
        answer = self.client.call("POST", route, body=body, write=write)
        self._note_version(answer)
        if not text:
            return envelope_of(answer), None, None
        value = answer.json()
        if isinstance(value, Mapping) and value.get("schema") == ENVELOPE_SCHEMA:
            return value, None, None  # refused before any command ran: rendered here
        if not isinstance(value, Mapping) or not isinstance(value.get("envelope"), Mapping):
            raise protocol(answer.status, "the answer is not a rendered envelope")
        document = value["envelope"]
        stdout, stderr = value.get("stdout"), value.get("stderr")
        if (
            not isinstance(document, Mapping)
            or document.get("schema") != ENVELOPE_SCHEMA
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
        ):
            raise protocol(answer.status, "the answer is not a rendered envelope")
        return document, stdout, stderr

    def body(self, **parts: JsonValue) -> bytes:
        document: dict[str, JsonValue] = {"actor": dict(self.actor), **parts}
        if self.pointer is not None and "args" in parts:
            document["pointer"] = dict(self.pointer)
        if self.graph_env is not None:
            document["graph_env"] = self.graph_env
        return json.dumps(document, ensure_ascii=False).encode()

    def _note_version(self, answer: Answer) -> None:
        warning = self.client.version_warning(answer)
        if warning is not None and warning not in self.warnings:
            self.warnings.append(warning)
