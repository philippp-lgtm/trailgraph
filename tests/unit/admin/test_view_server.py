"""The local server of graph-admin view (graph view plan, step 4)."""

import dataclasses
import http.client
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from support.activity import short_dir
from support.admin_context import admin_context
from support.clock import StepClock
from support.ids import SeededIdProvider
from support.roles import load_roles
from support.services import ServiceEnv

from graph_cli.catalog.activity import ActivityListener, SocketActivity
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.ports.activity import ReadSignal
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest
from graph_cli_admin.maintenance_cli.commands.view import page
from graph_cli_admin.maintenance_cli.view_server import Hub, ViewServer
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.view import ViewGraphs


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
            ViewGraphs(admin_context(svc.root), default=svc.name),
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
    # bound to 127.0.0.1 the Host names this port: no portless or other-port name
    assert get(server, f"/graph?t={token}", host="localhost").status == 403
    assert get(server, f"/graph?t={token}", host="127.0.0.1").status == 403
    assert get(server, f"/graph?t={token}", host=f"localhost:{server.port + 1}").status == 403
    # /health needs no token, but on 127.0.0.1 the Host check (DNS rebinding)
    assert get(server, "/health").status == 200
    assert get(server, "/health", host="evil.example:80").status == 403
    assert get(server, "/health", host="localhost").status == 403
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


def test_another_bind_address_keeps_token_and_host_checks(svc: ServiceEnv) -> None:
    """B14.13: bound for a container (``--host``), any port of 127.0.0.1 or localhost
    passes (a published port may differ), any other name does not."""
    running = ViewServer(
        ViewGraphs(admin_context(svc.root), default=svc.name), page=page(), host="0.0.0.0"
    )
    running.start()
    try:
        token = running.token
        assert get(running, f"/graph?t={token}", host="localhost:18080").status == 200
        assert get(running, f"/graph?t={token}", host="127.0.0.1:8080").status == 200
        assert get(running, f"/graph?t={token}", host="localhost").status == 200  # a proxy
        assert get(running, f"/graph?t={token}", host="evil.example:8080").status == 403
        assert get(running, "/graph?t=wrong", host="127.0.0.1:8080").status == 403
    finally:
        running.stop()


def test_health_needs_neither_token_nor_a_local_host(svc: ServiceEnv) -> None:
    """Bound to another address, a probe of a container platform names the pod's
    address and has no token; every other route still checks both."""
    token = "x" * 32
    running = ViewServer(
        ViewGraphs(admin_context(svc.root), default=svc.name),
        page=page(),
        host="0.0.0.0",
        token=token,
    )
    running.start()
    try:
        assert running.token == token and running.url.endswith(f"/?t={token}")
        health = get(running, "/health", host="10.42.0.7:8080")
        assert health.status == 200 and health.read() == b"ok\n"
        assert health.getheader("Content-Type") == "text/plain; charset=utf-8"
        assert health.getheader("Cache-Control") == "no-store"
        assert "default-src 'none'" in str(health.getheader("Content-Security-Policy"))
        assert get(running, "/health/", host="10.42.0.7:8080").status == 403
        assert get(running, f"/graph?t={token}", host="10.42.0.7:8080").status == 403
        assert get(running, "/graph?t=wrong", host="localhost:8080").status == 403
        assert get(running, f"/?t={token}", host="localhost:8080").status == 200
        # the page sends its own empty token; a proxy that adds the real one gets in
        assert get(running, f"/graph?t=&t={token}", host="localhost:8080").status == 200
        conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        conn.request("POST", "/health", body=b"{}")
        assert conn.getresponse().status == 405
    finally:
        running.stop()


def test_the_page_asks_for_every_route_by_a_relative_url() -> None:
    """Behind a proxy the page lives below a path prefix: no request may start at ``/``."""
    html = page().decode()
    assert re.findall(r"""(?:fetch|EventSource|url|api)\(\s*[`"'](/[^`"']*)""", html) == []
    for route in ('"graphs?t="', 'api("graph")', 'api("events")', '"entity"', '"blob"'):
        assert route in html, route


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
    revision = svc.txs()[-1].revisions[0].id
    signal = ReadSignal(svc.entry.id, "get", svc.actor, [revision])
    SocketActivity(server.listener.path.parent).reads(signal)
    (read,) = events(stream, 1)
    assert isinstance(read.pop("call"), str) and isinstance(read.pop("ts"), str)
    assert read == {
        "kind": "read",
        "command": "get",
        "actor": "tester",
        "session": svc.actor.session_key(),
        "ids": [entity],
        "edges": [],
        "step": len(created) + 1,
    }
    assert [e["step"] for e in created] == list(range(1, len(created) + 1))


def trail(server: ViewServer, count: int, graph: str = "") -> list[dict[str, Any]]:
    """The trail (of ``graph``, else the default) once it has ``count`` steps (or after 5 s)."""
    deadline = time.monotonic() + 5
    path = f"/trail?graph={graph}&t={server.token}"
    while True:
        steps: list[dict[str, Any]] = json.loads(get(server, path).read())["steps"]
        if len(steps) >= count or time.monotonic() > deadline:
            return steps
        time.sleep(0.05)


def test_the_trail_has_the_steps_the_page_missed(server: ViewServer, svc: ServiceEnv) -> None:
    assert get(server, "/trail?t=wrong").status == 403
    assert json.loads(get(server, f"/trail?t={server.token}").read()) == {
        "limit": 500,
        "steps": [],
    }
    svc.create(load_roles(svc.ontology).term("lifecycle_type"))
    written = [str(r.id) for r in svc.txs()[-1].revisions]
    trail(server, len(written))  # the feed saw the commit
    assert server.listener is not None
    signal = ReadSignal(svc.entry.id, "find", svc.actor, [svc.txs()[-1].revisions[0].id])
    SocketActivity(server.listener.path.parent).reads(signal)
    steps = trail(server, len(written) + 1)
    assert [s["kind"] for s in steps] == ["created"] * len(written) + ["read"]
    assert sorted(i for s in steps[:-1] for i in s["ids"]) == sorted(written)
    assert (steps[-1]["command"], steps[-1]["ids"]) == ("find", [written[0]])


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
        ViewGraphs(admin_context(env.root), default=env.name), page=page(), poll_s=0.05
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


@pytest.fixture
def two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ServiceEnv, ServiceEnv]:
    """Two graphs of different ontologies in one data root."""
    first = ServiceEnv("files", "notes_and_sources", tmp_path / "root")
    assert first.apply_seed().error is None

    def other_ids(root: Path) -> AdminContext:  # another graph id than the first one's
        return dataclasses.replace(admin_context(root), ids=SeededIdProvider(StepClock(), 77))

    monkeypatch.setattr("support.services.admin_context", other_ids)
    second = ServiceEnv("files", "inventory", tmp_path / "root", name="second_graph")
    assert second.apply_seed().error is None
    return first, second


def answer(server: ViewServer, path: str) -> tuple[int, Any]:
    response = get(server, path)
    return response.status, json.loads(response.read())


def test_the_graphs_to_switch_between(two: tuple[ServiceEnv, ServiceEnv]) -> None:
    first, second = two
    running = ViewServer(ViewGraphs(admin_context(first.root)), page=page(), poll_s=0.05)
    running.start()
    try:
        token = running.token
        assert get(running, "/graphs").status == 403
        status, listing = answer(running, f"/graphs?t={token}")
        assert status == 200
        assert listing == {
            "default": None,
            "graphs": [
                {
                    "id": str(env.entry.id),
                    "name": env.name,
                    "title": None,
                    "language": None,
                    "state": "active",
                }
                for env in (first, second)
            ],
            "signal_reads": False,
        }
        assert running.graphs.opened() == []  # listed, not opened
        # without --graph a route of one graph needs ?graph=
        status, error = answer(running, f"/graph?t={token}")
        assert status == 400 and error["code"] == "usage.invalid_argument"
        status, error = answer(running, f"/graph?graph=nothing&t={token}")
        assert status == 404 and error["code"] == "graph.not_found"
        for env in (first, second):
            for ref in (env.name, str(env.entry.id)):
                status, data = answer(running, f"/graph?graph={ref}&t={token}")
                assert status == 200 and data["graph"]["id"] == str(env.entry.id)
                assert len(data["nodes"]) == len(env.all_revisions())
        assert len(running.graphs.opened()) == 2
    finally:
        running.stop()


def test_the_graph_of_graph_option_is_the_default(two: tuple[ServiceEnv, ServiceEnv]) -> None:
    first, second = two
    running = ViewServer(
        ViewGraphs(admin_context(first.root), default=second.name), page=page(), poll_s=0.05
    )
    running.start()
    try:
        token = running.token
        assert answer(running, f"/graphs?t={token}")[1]["default"] == second.name
        assert answer(running, f"/graph?t={token}")[1]["graph"]["name"] == second.name
        named = answer(running, f"/graph?graph={first.name}&t={token}")[1]
        assert named["graph"]["name"] == first.name
    finally:
        running.stop()


def test_each_stream_has_the_events_of_its_graph_only(two: tuple[ServiceEnv, ServiceEnv]) -> None:
    first, second = two
    with short_dir() as directory:
        listener = ActivityListener.open(directory)
        running = ViewServer(
            ViewGraphs(admin_context(first.root)),
            page=page(),
            listener=listener,
            signal_reads=True,
            poll_s=0.05,
        )
        running.start()
        try:
            token = running.token
            stream = get(running, f"/events?graph={second.name}&t={token}")
            assert stream.fp.readline() == b": connected\n"
            assert get(running, f"/graph?graph={first.name}&t={token}").status == 200
            activity = SocketActivity(directory)
            for env in (first, second):
                env.create(load_roles(env.ontology).term("lifecycle_type"))
                read = env.txs()[-1].revisions[0].id
                activity.reads(ReadSignal(env.entry.id, "get", env.actor, [read]))
            written = second.txs()[-1].revisions
            # the feed polls the stores and the socket in turn: a read may come first
            got = events(stream, len(written) + 1)
            (read,) = [e for e in got if e["kind"] == "read"]
            types = {e["node"]["type"] for e in got if e["kind"] == "created"}
            assert len(got) - 1 == len(written) and read["ids"] == [str(written[0].id)]
            assert types <= set(second.model.classes)
            assert not types <= set(first.model.classes)
            for env in (first, second):
                count = len(env.txs()[-1].revisions) + 1
                kinds = [s["kind"] for s in trail(running, count, env.name)]
                assert sorted(kinds) == ["created"] * (count - 1) + ["read"]
        finally:
            running.stop()
            listener.close()


def opened(server: ViewServer) -> list[str]:
    return [v.source.entry.name for v in server.graphs.opened()]


def test_only_a_graph_nobody_watches_is_closed(two: tuple[ServiceEnv, ServiceEnv]) -> None:
    first, second = two
    running = ViewServer(ViewGraphs(admin_context(first.root), limit=1), page=page(), poll_s=0.05)
    running.start()
    try:
        token = running.token
        assert get(running, f"/graph?graph={first.name}&t={token}").status == 200
        assert get(running, f"/graph?graph={second.name}&t={token}").status == 200
        assert opened(running) == [second.name]  # nobody watched the first one
        stream = get(running, f"/events?graph={first.name}&t={token}")
        assert stream.fp.readline() == b": connected\n"
        assert get(running, f"/graph?graph={second.name}&t={token}").status == 200
        # both stay open, above the limit, rather than closing a watched graph
        assert sorted(opened(running)) == sorted([first.name, second.name])
        first.create(load_roles(first.ontology).term("lifecycle_type"))
        written = first.txs()[-1].revisions
        assert [e["kind"] for e in events(stream, len(written))] == ["created"] * len(written)
    finally:
        running.stop()


def test_the_default_graph_stays_open(two: tuple[ServiceEnv, ServiceEnv]) -> None:
    first, second = two
    running = ViewServer(
        ViewGraphs(admin_context(first.root), default=first.name, limit=1), page=page()
    )
    running.start()
    try:
        assert get(running, f"/graph?graph={second.name}&t={running.token}").status == 200
        assert opened(running) == [first.name, second.name]
    finally:
        running.stop()


def test_a_broken_graph_is_a_500_without_details(
    server: ViewServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(source: object) -> None:
        raise FileNotFoundError("/data/root/graphs/x/data/head.json")

    monkeypatch.setattr("graph_cli_admin.maintenance_cli.view_server.graph_json", broken)
    response = get(server, f"/graph?t={server.token}")
    body = response.read()
    assert response.status == 500
    assert json.loads(body) == {"code": "internal.error", "message": "the view could not answer"}
    assert get(server, f"/trail?t={server.token}").status == 200  # the server goes on
    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        "graph-admin view: /graph: FileNotFoundError: /data/root/graphs/x/data/head.json"
    ]


def test_the_stop_reaches_a_client_with_a_full_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graph_cli_admin.maintenance_cli.view_server.CLIENT_QUEUE", 2)
    hub = Hub()
    client = hub.subscribe("g")
    for n in range(3):
        hub.publish("g", {"n": n})  # the third is lost: too slow
    assert hub.watched("g") and not hub.watched("other")
    hub.close("g")
    assert [client.get_nowait(), client.get_nowait()] == [{"n": 1}, None]
