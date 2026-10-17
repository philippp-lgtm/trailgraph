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
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest
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


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def file_server(tmp_path: Path) -> Iterator[tuple[ViewServer, str, ServiceEnv]]:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root", version="1.1.0")
    assert env.apply_seed().error is None
    document = tmp_path / "report.pdf"
    document.write_bytes(PDF)
    added = env.make_files().add(
        FileAddRequest(
            actor=env.actor,
            graph=env.name,
            path=document,
            request=RequestMeta("file add", hash_value({"path": document.name})),
        )
    )
    running = ViewServer(
        open_view(admin_context(env.root), graph_ref=env.name), page=page(), poll_s=0.05
    )
    running.start()
    try:
        yield running, str(added.entity["id"]), env
    finally:
        running.stop()


def test_entity_details_with_attributes_edges_and_agents(
    server: ViewServer, svc: ServiceEnv
) -> None:
    revision = next(r for r in svc.all_revisions() if r.edges and r.record_status == "active")
    response = get(server, f"/entity?id={revision.id}&t={server.token}")
    data = json.loads(response.read())
    assert response.status == 200
    assert data["id"] == str(revision.id) and data["attrs"] == dict(revision.attrs)
    assert [e["id"] for e in data["outgoing"]] == [str(e.edge_id) for e in revision.edges]
    assert all(e["other"]["label"] for e in data["outgoing"])
    assert data["file"] is None
    assert data["history"][0]["rev"] == revision.rev
    assert data["history"][0]["actor"]["label"] == "tester"  # the principal, no agent named
    assert data["history"][0]["command"]
    target = revision.edges[0].target
    other = json.loads(get(server, f"/entity?id={target}&t={server.token}").read())
    assert str(revision.id) in {e["other"]["id"] for e in other["incoming"]}


def test_entity_errors(server: ViewServer) -> None:
    missing = get(server, f"/entity?id=0192f3c0-0000-7000-8000-0000000000ff&t={server.token}")
    assert missing.status == 404 and json.loads(missing.read())["code"] == "entity.not_found"
    garbage = get(server, f"/entity?id=nope&t={server.token}")
    assert garbage.status == 400 and json.loads(garbage.read())["code"] == "usage.invalid_argument"


def test_a_stored_pdf_is_served_inline(file_server: tuple[ViewServer, str, ServiceEnv]) -> None:
    server, entity, _ = file_server
    details = json.loads(get(server, f"/entity?id={entity}&t={server.token}").read())
    assert details["file"] == {
        "media_type": "application/pdf",
        "size": len(PDF),
        "stored": True,
        "inline": True,
    }
    response = get(server, f"/blob?id={entity}&t={server.token}")
    assert response.status == 200 and response.read() == PDF
    assert response.getheader("Content-Type") == "application/pdf"
    assert str(response.getheader("Content-Disposition")).startswith("inline;")
    policy = str(response.getheader("Content-Security-Policy"))
    assert "default-src 'none'" in policy and "script-src" not in policy
    assert get(server, f"/blob?id={entity}").status == 403  # no token


def test_a_blob_only_for_files(server: ViewServer, svc: ServiceEnv) -> None:
    response = get(server, f"/blob?id={svc.all_revisions()[0].id}&t={server.token}")
    assert response.status == 400
    assert json.loads(response.read())["code"] == "usage.invalid_argument"
