"""The local server of graph-admin view (graph view plan, step 4)."""

import http.client
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from support.activity import short_dir
from support.admin_context import admin_context
from support.roles import load_roles
from support.services import ServiceEnv

from graph_cli.catalog.activity import ActivityListener, SocketActivity
from graph_cli_admin.maintenance_cli.commands.view import page
from graph_cli_admin.maintenance_cli.view_server import ViewServer
from graph_cli_admin.services_admin.view import open_view


@pytest.fixture
def svc(tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root")
    assert env.apply_seed().error is None
    return env


@pytest.fixture
def server(svc: ServiceEnv) -> Iterator[ViewServer]:
    with short_dir() as directory:
        listener = ActivityListener.open(directory)
        running = ViewServer(
            open_view(admin_context(svc.root), graph_ref=svc.name),
            page=page(),
            listener=listener,
            signal_reads=True,
            poll_s=0.05,
        )
        running.start()
        try:
            yield running
        finally:
            running.stop()
            listener.close()


def get(server: ViewServer, path: str, *, host: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("GET", path, skip_host=True)
    conn.putheader("Host", host or f"127.0.0.1:{server.port}")
    conn.endheaders()
    return conn.getresponse()


def events(response: http.client.HTTPResponse, count: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    while len(found) < count:
        line = response.fp.readline()
        assert line, "the stream ended"
        if line.startswith(b"data: "):
            found.append(json.loads(line[6:]))
    return found


def test_only_token_and_local_host_get_in(server: ViewServer) -> None:
    token = server.token
    assert get(server, "/").status == 403
    assert get(server, "/graph?t=wrong").status == 403
    assert get(server, f"/graph?t={token}", host="evil.example:80").status == 403
    assert get(server, f"/graph?t={token}", host=f"localhost:{server.port}").status == 200
    assert get(server, f"/nothing?t={token}").status == 404
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("POST", f"/graph?t={token}", body=b"{}")
    assert conn.getresponse().status == 405
    response = get(server, f"/?t={token}")
    assert response.status == 200 and response.read() == page()
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert "default-src 'none'" in str(response.getheader("Content-Security-Policy"))
    assert response.getheader("Access-Control-Allow-Origin") is None


def test_graph_json(server: ViewServer, svc: ServiceEnv) -> None:
    response = get(server, f"/graph?t={server.token}")
    data = json.loads(response.read())
    assert response.getheader("Content-Type") == "application/json"
    assert data["seq"] == svc.head_seq() and data["signal_reads"] is True
    assert len(data["nodes"]) == len(svc.all_revisions())


def test_writes_and_reads_reach_the_event_stream(server: ViewServer, svc: ServiceEnv) -> None:
    stream = get(server, f"/events?t={server.token}")
    assert stream.status == 200 and stream.getheader("Content-Type") == "text/event-stream"
    assert stream.fp.readline() == b": connected\n"
    svc.create(load_roles(svc.ontology).term("lifecycle_type"))
    created = events(stream, len(svc.txs()[-1].revisions))
    assert {e["kind"] for e in created} == {"created"}
    entity = created[0]["node"]["id"]
    assert server.listener is not None
    SocketActivity(server.listener.path.parent).reads(svc.entry.id, [svc.txs()[-1].revisions[0].id])
    (read,) = events(stream, 1)
    assert read == {"kind": "read", "ids": [entity]}
