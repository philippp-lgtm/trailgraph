"""The local HTTP server of ``graph-admin view`` (graph view plan, "Server").

Stdlib only. It binds ``127.0.0.1`` and answers nothing but GET requests
that carry the token (``?t=``, random per start unless given) and a
``Host`` of ``127.0.0.1:<port>`` or ``localhost:<port>`` (DNS rebinding).
Bound to another address (``--host``, in a container), the ``Host`` must
still name ``127.0.0.1`` or ``localhost``, with any port or none: a
published port may differ from the bound one. No CORS headers; every response is
``no-store`` with a strict content security policy and no referrer.

Routes: ``/health`` ``ok`` without token, for the probes of a container
platform; bound to another address also without the ``Host`` check (a
probe names the pod's address), on ``127.0.0.1`` under it, so a page of
another site cannot probe it by DNS rebinding; it touches no graph.
``/`` the page, ``/graphs`` the graphs the page can switch between
(``ViewGraphs.listing``). The routes of one graph take it as ``graph=``
(name or id; without it the graph of ``--graph``): ``/graph`` the whole
graph as JSON, ``/entity?id=`` one entity in full (attributes, edges,
history with the writing agents), ``/blob?id=`` the stored file of a file
entity (PDFs and images inline, anything else as a download), ``/model``
the ontology (``model_json``, also needs ``read.describe``), ``/functions``
the published functions in full (``functions_json``), ``/policies`` the
installed policies as they bear on that graph (``policies_json``, also
needs ``maint.audit_read`` on ``installation``, as the admin API's
installation reads do), ``/events``
Server-Sent Events of that graph only, ``/trail`` its recent steps of
activity (``ActivityTrail``). The page asks for every route by a relative
URL, so a proxy may serve it below a path prefix.
One feed thread polls the store of every open graph and the activity
socket, numbers each write and read as a step of that graph's trail and
hands it to a bounded queue per client of that graph: a slow client loses
events, the feed never waits; ``/trail`` gives a page the steps it missed.
A failing poll becomes an ``error`` event and the feed goes on. A client that falls
behind (its queue is full) and a write with too many revisions to send one by one (a data
migration) each become one ``reload`` event: the page loads the graph again, as it does
after a reconnect. A graph whose ontology pin moved is opened again on the next round and
its clients get a ``reload`` too. A graph
that clients watch is never closed (``MAX_OPEN``); a stream that lost the
race with closing its graph ends at once and the browser reconnects. A
stream is authorized when it connects; on every round the feed closes the
graphs whose read the policy revoked meanwhile, which ends their streams,
and the reconnect is refused. Any other exception in a route is
a 500 with a fixed message, and one line on stderr.
"""

from __future__ import annotations

import contextlib
import json
import queue
import secrets
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from graph_cli.catalog.activity import ActivityListener
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.services_admin.view import (
    BlobStream,
    GraphView,
    ViewGraphs,
    entity_json,
    functions_json,
    graph_json,
    model_json,
    open_blob,
    policies_json,
    read_event,
)

HOST: Final = "127.0.0.1"
POLL_S: Final = 0.5
HEARTBEAT_S: Final = 15.0
CLIENT_QUEUE: Final = 1000
SECURITY_HEADERS: Final = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self'; frame-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
)
# A stored file is shown inside the page only: it runs no script and loads nothing.
BLOB_POLICY: Final = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; frame-ancestors 'self'"
)
NOT_FOUND_CODES: Final = frozenset({"entity.not_found", "graph.not_found"})

Event = dict[str, JsonValue]


Client = queue.Queue[Event | None]


class Hub:
    """Fans the events of a graph out to its connected clients; ``None`` tells a
    client to stop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, set[Client]] = {}

    def subscribe(self, graph: str) -> Client:
        client: Client = queue.Queue(maxsize=CLIENT_QUEUE)
        with self._lock:
            self._clients.setdefault(graph, set()).add(client)
        return client

    def unsubscribe(self, graph: str, client: Client) -> None:
        with self._lock:
            clients = self._clients.get(graph, set())
            clients.discard(client)
            if not clients:
                self._clients.pop(graph, None)

    def watched(self, graph: str) -> bool:
        with self._lock:
            return bool(self._clients.get(graph))

    def publish(self, graph: str, event: Event | None) -> None:
        with self._lock:
            clients = list(self._clients.get(graph, ()))
        for client in clients:
            try:
                client.put_nowait(event)
            except queue.Full:  # too slow: it loads the graph again instead of what it missed
                self._lagging(client, stop=event is None)

    @staticmethod
    def _lagging(client: Client, *, stop: bool) -> None:
        """Replace what a full queue holds by one ``reload`` event; a stop takes its place
        and a stop already queued stays (the stream must end)."""
        stopped = stop
        with contextlib.suppress(queue.Empty):
            while True:
                stopped = client.get_nowait() is None or stopped
        with contextlib.suppress(queue.Full):
            client.put_nowait(None if stopped else {"kind": "reload", "reason": "lagging"})

    def close(self, graph: str) -> None:
        self.publish(graph, None)

    def close_all(self) -> None:
        with self._lock:
            graphs = list(self._clients)
        for graph in graphs:
            self.close(graph)


class _HTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that went away is no error; anything else is one line, no traceback."""
        exc = sys.exception()
        if not isinstance(exc, ConnectionError):
            print(f"graph-admin view: {type(exc).__name__}: {exc}", file=sys.stderr)


class ViewServer:
    def __init__(
        self,
        graphs: ViewGraphs,
        *,
        page: bytes,
        port: int = 0,
        host: str = HOST,
        listener: ActivityListener | None = None,
        signal_reads: bool = False,
        poll_s: float = POLL_S,
        clock: Clock | None = None,
        token: str | None = None,
    ) -> None:
        self.graphs = graphs
        self.page = page
        self.listener = listener
        self.signal_reads = signal_reads
        self.poll_s = poll_s
        self.token = token if token is not None else secrets.token_urlsafe(24)
        self.hub = Hub()
        graphs.on_close = self.hub.close
        graphs.watched = self.hub.watched
        self.clock = clock if clock is not None else SystemClock()
        self._stop = threading.Event()
        self.host = host
        self._http = _HTTPServer((host, port), _handler(self))
        self._http.daemon_threads = True
        self._threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        return int(self._http.server_address[1])

    @property
    def address(self) -> str:
        return f"http://{HOST}:{self.port}/"

    @property
    def url(self) -> str:
        return f"{self.address}?t={self.token}"

    def start(self) -> None:
        for target in (self._http.serve_forever, self._run_feed):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def wait(self) -> None:
        """Block until ``stop``; short waits keep Ctrl+C responsive."""
        while not self._stop.wait(0.2):
            pass

    def stop(self) -> None:
        self._stop.set()
        self.hub.close_all()
        self._http.shutdown()
        self._http.server_close()
        for thread in self._threads:
            thread.join(timeout=2.0)

    @property
    def reads_shown(self) -> bool:
        return self.signal_reads and self.listener is not None

    def listing(self) -> dict[str, JsonValue]:
        data = self.graphs.listing()
        data["signal_reads"] = self.reads_shown
        return data

    def graph(self, view: GraphView) -> dict[str, JsonValue]:
        data = graph_json(view.source)
        data["signal_reads"] = self.reads_shown
        return data

    def model(self, view: GraphView) -> dict[str, JsonValue]:
        self.graphs.authorize(view.source.entry, "read.describe")
        return model_json(view.source)

    def policies(self, view: GraphView) -> dict[str, JsonValue]:
        self.graphs.authorize(view.source.entry, "maint.audit_read", "installation")
        return policies_json(self.graphs.ctx, view.source)

    def _run_feed(self) -> None:
        while not self._stop.is_set():
            self.graphs.close_revoked()
            for graph in self.graphs.refresh_models():
                self.hub.publish(graph, {"kind": "reload", "reason": "model"})
            for view in self.graphs.opened():
                graph = str(view.source.entry.id)
                try:
                    for event in view.feed.poll():
                        # a reload is no step of activity: the trail keeps what agents did
                        sent = event if event["kind"] == "reload" else view.trail.add(event)
                        self.hub.publish(graph, sent)
                except GraphCliError as exc:
                    error: Event = {"kind": "error", "code": exc.code, "message": exc.message}
                    self.hub.publish(graph, error)
            if self.listener is None:
                self._stop.wait(self.poll_s)
                continue
            try:
                received = self.listener.receive(self.poll_s)
            except OSError:
                self._stop.wait(self.poll_s)
                continue
            now = self.clock.now()
            for activity in received:
                shown = self.graphs.find(activity.graph)  # a graph not open has no viewer
                read = read_event(shown.source, activity, now) if shown is not None else None
                if shown is not None and read is not None:
                    self.hub.publish(activity.graph, shown.trail.add(read))


def _handler(server: ViewServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "graph-admin-view"
        sys_version = ""
        started = False  # the status line of this request's answer is sent

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/health" and (server.host != HOST or self._local_host()):
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            if not self._local_host() or not self._token(url.query):
                self._send(403, b"forbidden\n", "text/plain; charset=utf-8")
                return
            params = parse_qs(url.query)
            entity = params.get("id", [""])[0]

            def view() -> GraphView:
                return server.graphs.get(params.get("graph", [""])[0])

            routes: dict[str, Callable[[], None]] = {
                "/": lambda: self._send(200, server.page, "text/html; charset=utf-8"),
                "/graphs": lambda: self._json(server.listing()),
                "/graph": lambda: self._json(server.graph(view())),
                "/entity": lambda: self._json(entity_json(view().source, entity)),
                "/blob": lambda: self._blob(view(), entity),
                "/model": lambda: self._json(server.model(view())),
                "/functions": lambda: self._json(functions_json(view().source)),
                "/policies": lambda: self._json(server.policies(view())),
                "/events": lambda: self._events(view()),
                "/trail": lambda: self._json(view().trail.to_json()),
            }
            route = routes.get(url.path)
            if route is None:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            self.started = False
            try:
                route()
            except ConnectionError:  # the client went away: nothing to answer, nothing to report
                self.close_connection = True
            except GraphCliError as exc:
                if self.started:  # the answer is under way: only the connection can end
                    self.close_connection = True
                    return
                status = (
                    404 if exc.code in NOT_FOUND_CODES else 403 if "denied" in exc.code else 400
                )
                body = json.dumps({"code": exc.code, "message": exc.message}).encode()
                self._send(status, body, "application/json")
            except Exception as exc:  # a broken graph: no traceback, no path to the browser
                print(f"graph-admin view: {url.path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                if self.started:  # the answer is under way: only the connection can end
                    self.close_connection = True
                    return
                body = json.dumps(
                    {"code": "internal.error", "message": "the view could not answer"}
                ).encode()
                self._send(500, body, "application/json")

        def send_response(self, code: int, message: str | None = None) -> None:
            self.started = True
            super().send_response(code, message)

        def _json(self, data: dict[str, JsonValue]) -> None:
            self._send(200, json.dumps(data).encode(), "application/json")

        def _blob(self, view: GraphView, entity: str) -> None:
            blob = open_blob(view.source, entity)
            try:
                self._send_blob(blob)
            finally:
                blob.chunks.close()

        def _send_blob(self, blob: BlobStream) -> None:
            self.send_response(200)
            self.send_header("Content-Type", blob.media_type)
            self.send_header("Content-Length", str(blob.size))
            disposition = "inline" if blob.inline else "attachment"
            name = blob.file_name.replace('"', "").replace("\\", "")
            name = "".join(c if c.isprintable() and c.isascii() else "_" for c in name)
            self.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
            for header, value in SECURITY_HEADERS:
                if header != "Content-Security-Policy":
                    self.send_header(header, value)
            self.send_header("Content-Security-Policy", BLOB_POLICY)
            self.end_headers()
            try:
                for chunk in blob.chunks:
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_POST(self) -> None:
            self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _local_host(self) -> bool:
            host = self.headers.get("Host", "")
            if server.host == HOST:
                return host in (f"{HOST}:{server.port}", f"localhost:{server.port}")
            return (host.rpartition(":")[0] if ":" in host else host) in (HOST, "localhost")

        def _token(self, query: str) -> bool:
            given = parse_qs(query).get("t", [""])[0]
            return secrets.compare_digest(given.encode(), server.token.encode())

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _events(self, view: GraphView) -> None:
            graph = str(view.source.entry.id)
            client = server.hub.subscribe(graph)
            try:
                if server.graphs.find(graph) is not view:  # closed before it was watched
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                for name, value in SECURITY_HEADERS:
                    self.send_header(name, value)
                self.end_headers()
                self.close_connection = True
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = client.get(timeout=HEARTBEAT_S)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                    else:
                        if event is None:
                            return
                        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                server.hub.unsubscribe(graph, client)

        def log_message(self, format: str, *args: Any) -> None:
            return  # no access log: stderr belongs to the envelope

    return Handler
