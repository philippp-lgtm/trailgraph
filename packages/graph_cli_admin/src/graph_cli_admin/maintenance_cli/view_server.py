"""The local HTTP server of ``graph-admin view`` (graph view plan, "Server").

Stdlib only. It binds ``127.0.0.1`` and answers nothing but GET requests
that carry the token (``?t=``) and a ``Host`` of ``127.0.0.1:<port>`` or
``localhost:<port>`` (DNS rebinding). No CORS headers; every response is
``no-store`` with a strict content security policy and no referrer.

Routes: ``/`` the page, ``/graph`` the whole graph as JSON, ``/events``
Server-Sent Events. One feed thread polls the store and the activity
socket and hands each event to a bounded queue per client: a slow client
loses events, the feed never waits. A failing poll becomes an ``error``
event and the feed goes on.
"""

from __future__ import annotations

import contextlib
import json
import queue
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from graph_cli.catalog.activity import ActivityListener
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli_admin.services_admin.view import ViewFeed, ViewSource, graph_json, read_event

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
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
)

Event = dict[str, JsonValue]


class Hub:
    """Fans events out to the connected clients; ``None`` tells a client to stop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[queue.Queue[Event | None]] = set()

    def subscribe(self) -> queue.Queue[Event | None]:
        client: queue.Queue[Event | None] = queue.Queue(maxsize=CLIENT_QUEUE)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue[Event | None]) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, event: Event | None) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            with contextlib.suppress(queue.Full):  # too slow: it reloads on reconnect
                client.put_nowait(event)


class ViewServer:
    def __init__(
        self,
        source: ViewSource,
        *,
        page: bytes,
        port: int = 0,
        listener: ActivityListener | None = None,
        signal_reads: bool = False,
        poll_s: float = POLL_S,
    ) -> None:
        self.source = source
        self.page = page
        self.listener = listener
        self.signal_reads = signal_reads
        self.poll_s = poll_s
        self.token = secrets.token_urlsafe(24)
        self.hub = Hub()
        self.feed = ViewFeed(source, source.read.head().seq)
        self._stop = threading.Event()
        self._http = ThreadingHTTPServer((HOST, port), _handler(self))
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
        self.hub.publish(None)
        self._http.shutdown()
        self._http.server_close()
        for thread in self._threads:
            thread.join(timeout=2.0)

    def graph(self) -> dict[str, JsonValue]:
        data = graph_json(self.source)
        data["signal_reads"] = self.signal_reads and self.listener is not None
        return data

    def _run_feed(self) -> None:
        while not self._stop.is_set():
            try:
                for event in self.feed.poll():
                    self.hub.publish(event)
            except GraphCliError as exc:
                self.hub.publish({"kind": "error", "code": exc.code, "message": exc.message})
            if self.listener is None:
                self._stop.wait(self.poll_s)
                continue
            try:
                received = self.listener.receive(self.poll_s)
            except OSError:
                self._stop.wait(self.poll_s)
                continue
            for activity in received:
                read = read_event(self.source, activity)
                if read is not None:
                    self.hub.publish(read)


def _handler(server: ViewServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "graph-admin-view"
        sys_version = ""

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if not self._allowed(url.query):
                self._send(403, b"forbidden\n", "text/plain; charset=utf-8")
                return
            routes: dict[str, Callable[[], None]] = {
                "/": lambda: self._send(200, server.page, "text/html; charset=utf-8"),
                "/graph": lambda: self._send(
                    200, json.dumps(server.graph()).encode(), "application/json"
                ),
                "/events": self._events,
            }
            route = routes.get(url.path)
            if route is None:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
            else:
                route()

        def do_POST(self) -> None:
            self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _allowed(self, query: str) -> bool:
            host = self.headers.get("Host", "")
            if host not in (f"{HOST}:{server.port}", f"localhost:{server.port}"):
                return False
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

        def _events(self) -> None:
            client = server.hub.subscribe()
            try:
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
                server.hub.unsubscribe(client)

        def log_message(self, format: str, *args: Any) -> None:
            return  # no access log: stderr belongs to the envelope

    return Handler
