"""An installation served by graph-server: the policy grant, tokens, WSGI calls (B14)."""

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

from harness import CliRunner

from support.admin import admin
from support.policies import write_policy

# The app (tests prepare data with it) and the server may write; one token is bound
# to a project, "stranger" is granted nothing.
SERVER_POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: [app, server], graphs: "*", level: write}
  - {entrypoint: server, principal: {id: "token:bound"}, graphs: "*", level: write}
deny:
  - {id: no_strangers, principal_id: "token:stranger", action: ["*"]}
no_project: {"*": write}
"""


def grant_server(run_cli: CliRunner, tmp_path: Path, text: str = SERVER_POLICY) -> None:
    policy = write_policy(tmp_path / "policies", "installation.yaml", text)
    outcome = admin(run_cli, "policy", "apply", str(policy), "--reason", "serve the api")
    assert outcome.code == 0, outcome.envelope


def new_token(run_cli: CliRunner, name: str, *args: str) -> str:
    outcome = admin(run_cli, "token", "create", name, *args)
    assert outcome.code == 0, outcome.envelope
    token: str = outcome.data["token"]
    return token


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def error(self) -> dict[str, Any]:
        error = self.json["error"]
        assert isinstance(error, dict), self.json
        return error


def call(
    app: Any,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: Any = None,
    raw: bytes | None = None,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> Reply:
    """One request through the WSGI app, in-process."""
    data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        REQUEST_METHOD=method,
        PATH_INFO=path,
        QUERY_STRING=query,
        CONTENT_LENGTH=str(len(data)),
    )
    environ["wsgi.input"] = io.BytesIO(data)
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    started: dict[str, Any] = {}

    def start_response(status: str, response_headers: list[tuple[str, str]]) -> None:
        started["status"] = int(status.split()[0])
        started["headers"] = dict(response_headers)

    chunks = app(environ, start_response)
    content = b"".join(chunks)
    close = getattr(chunks, "close", None)
    if close is not None:
        close()
    return Reply(started["status"], started["headers"], content)


def actor_header(actor: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(actor).encode()).decode().rstrip("=")
