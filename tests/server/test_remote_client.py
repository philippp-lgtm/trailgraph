"""S: the remote mode of ``graph`` (contracts B14.10, B14.13).

Usage errors need no server; a missing server, a refused token, an answer
that is not the API's and a lost write answer each have their code. Stub
servers play the failures a real server cannot be made to show.
"""

import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, init, parse
from support.app import create_graph
from support.serve import client_env, graph_server
from support.server import grant_server, new_token

pytestmark = pytest.mark.slow

ENVELOPE = {
    "schema": "graph_cli.envelope/1",
    "ok": True,
    "command": "whoami",
    "graph": None,
    "data": {"stub": True},
    "page": None,
    "hints": [],
    "warnings": [],
    "error": None,
}
Behaviour = Callable[[BaseHTTPRequestHandler, int], None]


def remote(run_cli: CliRunner, env: dict[str, str], *args: str) -> Outcome:
    return parse(run_cli("graph", *args, "--json", env=env))


@contextmanager
def stub(behaviour: Behaviour) -> Iterator[list[str]]:
    """A server whose every request is handled by ``behaviour(handler, number)``."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        request_body = b""

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.request_body = self.rfile.read(length)
            seen.append(self.path)
            behaviour(self, len(seen))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        seen.append(str(server.server_address[1]))
        yield seen
    finally:
        server.shutdown()
        server.server_close()


def port_of(seen: list[str]) -> int:
    return int(seen.pop(0))


def answer(handler: BaseHTTPRequestHandler, body: bytes, *, version: str = "0.1.0.dev0") -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Graph-Cli-Version", version)
    handler.end_headers()
    handler.wfile.write(body)


def hang_up(handler: BaseHTTPRequestHandler) -> None:
    handler.close_connection = True
    handler.connection.shutdown(2)


def test_usage_errors_need_no_server(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = client_env(isolated_env, tmp_path / "client", 9, "gct_x")
    usage = remote(run_cli, env, "create")
    assert (usage.code, usage.error["code"]) == (2, "usage.missing_argument")
    down = remote(run_cli, env, "whoami")
    assert (down.code, down.error["code"]) == (5, "remote.unreachable")
    assert down.error["details"] == {"endpoint": "http://127.0.0.1:9"}
    assert "gct_x" not in json.dumps(down.envelope)
    described = remote(run_cli, env, "describe", "commands")
    assert described.code == 0  # runs on the client
    no_token = client_env(isolated_env, tmp_path / "other", 9, None)
    refused = remote(run_cli, no_token, "whoami")
    assert (refused.code, refused.error["code"]) == (3, "remote.unauthorized")


def test_token_file_spill_and_maintenance_refusal(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    grant_server(run_cli, tmp_path)
    token = new_token(run_cli, "ci")
    token_file = tmp_path / "client" / "token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(token + "\n")
    os.chmod(token_file, 0o644)
    with graph_server(isolated_env, tmp_path) as server:
        env = client_env(
            isolated_env,
            tmp_path / "client",
            server.port,
            None,
            extra=f'[remote]\ntoken_file = "{token_file}"\n',
        )
        readable = remote(run_cli, env, "whoami")
        assert (readable.code, readable.error["code"]) == (5, "config.invalid")
        assert readable.error["details"]["key"] == "remote.token_file"
        os.chmod(token_file, 0o600)
        assert remote(run_cli, env, "whoami").data["actor"]["principal"]["id"] == "token:ci"

        spilled = remote(
            run_cli,
            {**env, "GRAPH_CLI_SPILL_BYTES": "300"},
            "describe",
            "types",
            "--graph",
            "example_graph",
        )
        assert spilled.code == 0 and spilled.data is None
        (warning,) = [w for w in spilled.envelope["warnings"] if w["code"] == "output.spilled"]
        assert json.loads(Path(warning["details"]["path"]).read_text())["data"]["types"]

        admin = parse(run_cli("graph-admin", "init", "--json", env=env))
        assert (admin.code, admin.error["code"]) == (1, "admin.remote_mode")
        assert "graph-admin" not in str(admin.error["hint"])
        # the app token is not graph-admin's: the admin API needs an admin token
        listed = parse(run_cli("graph-admin", "graph", "list", "--json", env=env))
        assert (listed.code, listed.error["code"]) == (3, "remote.unauthorized")
        assert "GRAPH_CLI_ADMIN_TOKEN" in listed.error["message"]


@pytest.mark.unchecked_output("the stub server answers whoami with a stub")
def test_another_server_version_warns(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    with stub(lambda h, n: answer(h, json.dumps(ENVELOPE).encode(), version="9.9.9")) as seen:
        env = client_env(isolated_env, tmp_path / "client", port_of(seen), "gct_x")
        outcome = remote(run_cli, env, "whoami")
    assert outcome.code == 0 and outcome.data == {"stub": True}
    assert outcome.warning_codes == ["remote.version_differs"]
    assert outcome.envelope["warnings"][0]["details"]["server"] == "9.9.9"


def test_an_answer_that_is_not_the_apis(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    with stub(lambda h, n: answer(h, b"<html>bad gateway</html>")) as seen:
        env = client_env(isolated_env, tmp_path / "client", port_of(seen), "gct_x")
        outcome = remote(run_cli, env, "whoami")
    assert (outcome.code, outcome.error["code"]) == (5, "remote.protocol")
    assert outcome.error["details"] == {"status": 200}


def test_a_lost_write_answer_names_its_key(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    with stub(lambda h, n: hang_up(h)) as seen:
        env = client_env(isolated_env, tmp_path / "client", port_of(seen), "gct_x")
        outcome = remote(run_cli, env, "create", "inv:Location", "--set", "name=Shelf")
    assert seen == ["/v1/commands/create"]  # a write is never retried
    assert (outcome.code, outcome.error["code"]) == (5, "remote.outcome_unknown")
    key = outcome.error["details"]["idempotency_key"]
    assert key.startswith("r-") and len(key) == 34
    assert outcome.error["hint"] == (
        f"graph create inv:Location --set name=Shelf --idempotency-key {key}"
    )
    with stub(lambda h, n: hang_up(h)) as seen:
        env = client_env(isolated_env, tmp_path / "client", port_of(seen), "gct_x")
        given = remote(run_cli, env, "create", "inv:Location", "--idempotency-key", "k-1")
    assert given.error["details"]["idempotency_key"] == "k-1"


def test_the_client_sends_canonical_names(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    bodies: list[Any] = []
    keys: list[str | None] = []

    def record(handler: BaseHTTPRequestHandler, number: int) -> None:
        bodies.append(json.loads(handler.request_body))  # type: ignore[attr-defined]
        keys.append(handler.headers.get("Idempotency-Key"))
        hang_up(handler)

    with stub(record) as seen:
        env = client_env(isolated_env, tmp_path / "client", port_of(seen), "gct_x")
        args = ("inv:Location", "--set", "name=Shelf", "--graph", "example_graph")
        outcome = remote(run_cli, env, "create", *args, "--idempotency-key", "k-1")
        malformed = remote(run_cli, env, "create", *args, "--idempotency-key", "k 1")
    assert bodies[0]["args"] == {  # B14.22 G5; the hint stays the command line
        "type": "inv:Location",
        "set": ["name=Shelf"],
        "graph": "example_graph",
    }
    assert keys[0] == '"k-1"'  # B14.22 G7: the key as an RFC 9651 String
    assert outcome.error["hint"] == f"graph create {' '.join(args)} --idempotency-key k-1"
    # a malformed key stays in args, so the server refuses it as the CLI does
    assert keys[1] is None and bodies[1]["args"]["idempotency_key"] == "k 1"
    assert malformed.error["code"] == "remote.outcome_unknown"


@pytest.mark.unchecked_output("the stub server answers whoami with a stub")
def test_a_read_is_retried_once(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    def flaky(handler: BaseHTTPRequestHandler, number: int) -> None:
        if number == 1:
            hang_up(handler)
        else:
            answer(handler, json.dumps(ENVELOPE).encode())

    with stub(flaky) as seen:
        env = client_env(isolated_env, tmp_path / "client", port_of(seen), "gct_x")
        outcome = remote(run_cli, env, "whoami")
    assert outcome.code == 0 and len(seen) == 2


def test_plain_http_beyond_loopback_warns_when_allowed(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = client_env(isolated_env, tmp_path / "client", 9, "gct_x")
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(
        'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "http://192.0.2.1:9"\n'
        "[remote]\nallow_plain_http = true\ntimeout_s = 1\n"
    )
    outcome = remote(run_cli, env, "whoami")
    assert outcome.error["code"] == "remote.unreachable"
    assert "remote.plain_http" in outcome.warning_codes
    config.write_text(
        'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "http://192.0.2.1:9"\n'
    )
    refused = remote(run_cli, env, "whoami")
    assert (refused.code, refused.error["code"]) == (5, "config.invalid")


def test_the_client_names_its_build_and_compares_the_servers() -> None:
    """Plan origin_lifecycle E2: a warning when the server runs another build, unless the
    answer is an error (it says why); without a build id only the versions are compared."""
    from graph_cli.catalog.endpoint import parse_endpoint
    from graph_cli.catalog.remote import RemoteClient
    from graph_cli.kernel.errors import WarningRecord

    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    sent: list[str | None] = []
    replies: list[tuple[int, str | None]] = []

    def behaviour(handler: BaseHTTPRequestHandler, number: int) -> None:
        sent.append(handler.headers.get("Graph-Cli-Build"))
        status, build = replies[number - 1]
        body = json.dumps(ENVELOPE).encode()
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Graph-Cli-Version", "0.1.0.dev0")
        if build is not None:
            handler.send_header("Graph-Cli-Build", build)
        handler.end_headers()
        handler.wfile.write(body)

    with stub(behaviour) as seen:
        endpoint = parse_endpoint(f"http://127.0.0.1:{port_of(seen)}")
        assert not isinstance(endpoint, str)

        def client(build: str | None, warnings: list[WarningRecord]) -> RemoteClient:
            return RemoteClient(
                endpoint,
                "gct_x",
                timeout_s=5,
                version="0.1.0.dev0",
                program="graph",
                build=build,
                warnings=warnings,
            )

        warnings: list[WarningRecord] = []
        replies[:] = [(200, f"graph {a}"), (200, f"graph {b}"), (200, f"graph {b}")]
        replies += [(422, f"graph {b}"), (200, f"graph-admin {b}")]
        for _ in range(5):
            client(a, warnings).call("POST", "/v1/commands/whoami", body=b"{}")
        assert sent == [f"graph {a}"] * 5
        assert [w.code for w in warnings] == ["remote.version_differs"]  # noted once
        assert warnings[0].details["server_build"] == b
        assert "build bbbbbbbbbbbb of graph, this is build aaaaaaaaaaaa" in warnings[0].message
        alone: list[WarningRecord] = []
        replies.append((200, f"graph {b}"))
        client(None, alone).call("POST", "/v1/commands/whoami", body=b"{}")
    assert sent[-1] is None and alone == []  # no build to claim, the versions are equal


def test_the_client_names_its_compatibility_and_says_whether_the_servers_is_the_same() -> None:
    """Plan client_compatibility C4: the warning says whether only the build differs."""
    from graph_cli.catalog.endpoint import parse_endpoint
    from graph_cli.catalog.remote import Answer, RemoteClient
    from graph_cli.kernel.errors import WarningRecord

    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    c, d = "sha256:" + "c" * 64, "sha256:" + "d" * 64
    endpoint = parse_endpoint("http://127.0.0.1:1")
    assert not isinstance(endpoint, str)
    client = RemoteClient(
        endpoint, "gct_x", timeout_s=5, version="0.1.0.dev0", program="graph", build=a, compat=c
    )
    headers = client._headers(None, None, None)
    assert headers["Graph-Cli-Build"] == f"graph {a}"
    assert headers["Graph-Cli-Compat"] == f"graph {c}"

    def warning(compat: str | None) -> WarningRecord | None:
        found = {"graph-cli-version": "0.1.0.dev0", "graph-cli-build": f"graph {b}"}
        if compat is not None:
            found["graph-cli-compat"] = compat
        return client.version_warning(Answer(200, found, b"{}"))

    same = warning(f"graph {c}")
    assert same is not None and same.details["compatible"] is True
    assert "its commands and skills are the same; graph self update installs" in same.message
    for other in (f"graph {d}", f"graph-admin {c}", None):
        found = warning(other)
        assert found is not None and found.details["compatible"] is False, other
        assert found.message.endswith("commands and answers may differ")
    without = RemoteClient(
        endpoint, "gct_x", timeout_s=5, version="0.1.0.dev0", program="graph", build=None
    )
    assert "Graph-Cli-Compat" not in without._headers(None, None, None)
