"""The activity signal over Unix datagram sockets (graph view plan, "Activity signal").

A viewer (``graph-admin view``) binds one socket in ``<state_root>/activity/``
and the app sends each read to every socket there. Fire and forget: no
viewer, a stale socket or a full buffer drops the signal, and every
``OSError`` is swallowed. The app never creates the directory (B12.4); the
viewer creates it with mode 0700.

A datagram is canonical JSON ``{"v": 2, "graph": <id>, "kind": "read",
"command": <command name>, "call": <random hex>, "actor": <label>,
"session": <session id or null>, "ids": [<entity ids>], "edges": [<edge
ids>]}``: who read (the label ``graph-admin view`` shows for writes and the
session that groups one agent's reads) and what, never labels, attributes,
arguments or query text. Actor and session are cut to ``MAX_TEXT``
characters. Datagrams stay below macOS ``net.local.dgram.maxdgram`` (2048
bytes); a read of many entities is split into datagrams with the same
``call``, entities and edges taking turns (so a cut read still draws a
walk, not only dots), and whatever does not fit into ``MAX_DATAGRAMS``
datagrams is dropped. ``decode`` still reads version 1
(``ids`` only) from senders of an earlier release.
"""

from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import CanonicalError, JsonValue, canonical_bytes, parse_json
from graph_cli.kernel.ports.activity import ReadSignal

ACTIVITY_DIR: Final = "activity"
SOCKET_SUFFIX: Final = ".sock"
PAYLOAD_VERSION: Final = 2
READ_VERSIONS: Final = frozenset({1, PAYLOAD_VERSION})
MAX_DATAGRAM_BYTES: Final = 1800
MAX_DATAGRAMS: Final = 8
MAX_TEXT: Final = 128
CALL_BYTES: Final = 4
RECEIVE_BYTES: Final = 4096
RECEIVE_BUFFER: Final = 256 * 1024  # macOS defaults to 4096 (net.local.dgram.recvspace)


def activity_dir(state_root: Path) -> Path:
    return state_root / ACTIVITY_DIR


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One received datagram; a version 1 datagram has ids only."""

    graph: str
    kind: str
    ids: tuple[str, ...]
    edges: tuple[str, ...] = ()
    command: str | None = None
    call: str | None = None
    actor: str | None = None
    session: str | None = None


def encode_reads(signal: ReadSignal, call: str | None = None) -> list[bytes]:
    """The datagrams for one read: unique ids in first-seen order, an entity and an edge
    in turn, split by size; every datagram carries the same ``call``."""
    header: dict[str, JsonValue] = {
        "v": PAYLOAD_VERSION,
        "graph": str(signal.graph),
        "kind": "read",
        "command": signal.command,
        "call": call if call is not None else secrets.token_hex(CALL_BYTES),
        "actor": signal.actor.label()[:MAX_TEXT],
        "session": _session(signal),
    }
    entities = [("ids", text) for text in dict.fromkeys(str(i) for i in signal.entities)]
    edges = [("edges", text) for text in dict.fromkeys(str(e) for e in signal.edges)]
    items = [item for pair in zip(entities, edges, strict=False) for item in pair]
    items += entities[len(edges) :] + edges[len(entities) :]
    datagrams: list[bytes] = []
    chunk: list[tuple[str, str]] = []
    for item in items:
        if chunk and len(_payload(header, [*chunk, item])) > MAX_DATAGRAM_BYTES:
            datagrams.append(_payload(header, chunk))
            if len(datagrams) == MAX_DATAGRAMS:
                return datagrams
            chunk = []
        chunk.append(item)
    if chunk:
        datagrams.append(_payload(header, chunk))
    return datagrams


def _session(signal: ReadSignal) -> str | None:
    key = signal.actor.session_key()
    return None if key is None else key[:MAX_TEXT]


def _payload(header: dict[str, JsonValue], chunk: list[tuple[str, str]]) -> bytes:
    ids: list[JsonValue] = [text for key, text in chunk if key == "ids"]
    edges: list[JsonValue] = [text for key, text in chunk if key == "edges"]
    return canonical_bytes({**header, "ids": ids, "edges": edges})


def decode(data: bytes) -> ActivityEvent | None:
    """One datagram, or ``None`` for anything that is not a version 1 or 2 payload."""
    try:
        document = parse_json(data)
    except CanonicalError:
        return None
    if not isinstance(document, dict):
        return None
    version = document.get("v")
    if isinstance(version, bool) or version not in READ_VERSIONS:
        return None
    graph, ids = document.get("graph"), _texts(document.get("ids"))
    if not isinstance(graph, str) or document.get("kind") != "read" or ids is None:
        return None
    if version == 1:
        return ActivityEvent(graph, "read", ids)
    edges = _texts(document.get("edges"))
    command, call, actor = document.get("command"), document.get("call"), document.get("actor")
    session = document.get("session")
    if edges is None or not isinstance(command, str) or not isinstance(call, str):
        return None
    if not isinstance(actor, str) or not (session is None or isinstance(session, str)):
        return None
    return ActivityEvent(graph, "read", ids, edges, command, call, actor, session)


def _texts(value: JsonValue | None) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(str(item) for item in value)


class SocketActivity:
    """The app side: sends reads to every viewer socket in ``directory``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def reads(self, signal: ReadSignal) -> None:
        if not signal.entities and not signal.edges:
            return
        try:
            targets = sorted(
                str(p) for p in self.directory.iterdir() if p.name.endswith(SOCKET_SUFFIX)
            )
        except OSError:
            return
        if not targets:
            return
        try:
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError:
            return
        with sender:
            sender.setblocking(False)
            datagrams = encode_reads(signal)
            for target in targets:
                for datagram in datagrams:
                    try:
                        sender.sendto(datagram, target)
                    except OSError:
                        break  # this viewer is gone or busy; try the next one


class ActivityListener:
    """The viewer side: one bound socket in the activity directory.

    ``open`` creates the directory (0700), removes sockets no process
    listens on, and binds ``v<pid>-<random>.sock`` (0600) with a receive
    buffer large enough for the reads of a busy agent. ``close`` removes
    the socket file.
    """

    def __init__(self, sock: socket.socket, path: Path) -> None:
        self._sock = sock
        self.path = path

    @classmethod
    def open(cls, directory: Path) -> ActivityListener:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        remove_stale(directory)
        path = directory / f"v{os.getpid()}-{secrets.token_hex(3)}{SOCKET_SUFFIX}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER)
            sock.bind(str(path))
        except OSError:
            sock.close()
            raise
        try:
            os.chmod(path, 0o600)  # fails on some shared mounts (virtiofs: EINVAL)
        except OSError:
            sock.close()
            path.unlink(missing_ok=True)
            raise
        return cls(sock, path)

    def fileno(self) -> int:
        return self._sock.fileno()

    def receive(self, timeout_s: float) -> list[ActivityEvent]:
        """Every datagram waiting now, after waiting up to ``timeout_s`` for the first."""
        events: list[ActivityEvent] = []
        self._sock.settimeout(timeout_s)
        while True:
            try:
                data = self._sock.recv(RECEIVE_BYTES)
            except (TimeoutError, BlockingIOError):
                return events
            event = decode(data)
            if event is not None:
                events.append(event)
            self._sock.setblocking(False)

    def close(self) -> None:
        self._sock.close()
        self.path.unlink(missing_ok=True)


def remove_stale(directory: Path) -> list[Path]:
    """Remove the socket files in ``directory`` that no process is bound to."""
    removed: list[Path] = []
    try:
        candidates = sorted(p for p in directory.iterdir() if p.name.endswith(SOCKET_SUFFIX))
    except OSError:
        return removed
    for path in candidates:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        with probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink(missing_ok=True)
                removed.append(path)
            except OSError:
                continue
    return removed
