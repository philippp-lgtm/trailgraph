"""The activity signal over Unix datagram sockets (graph view plan, "Activity signal").

A viewer (``graph-admin view``) binds one socket in ``<state_root>/activity/``
and the app sends each read to every socket there. Fire and forget: no
viewer, a stale socket or a full buffer drops the signal, and every
``OSError`` is swallowed. The app never creates the directory (B12.4); the
viewer creates it with mode 0700.

A datagram is canonical JSON ``{"v": 1, "graph": <id>, "kind": "read",
"ids": [<entity ids>]}``: ids only, never labels, attributes, query text or
the actor. Datagrams stay below macOS ``net.local.dgram.maxdgram`` (2048
bytes); a read of many entities is split, and ids beyond
``MAX_DATAGRAMS`` datagrams are dropped.
"""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import CanonicalError, JsonValue, canonical_bytes, parse_json
from graph_cli.kernel.ids import EntityId, GraphId

ACTIVITY_DIR: Final = "activity"
SOCKET_SUFFIX: Final = ".sock"
PAYLOAD_VERSION: Final = 1
MAX_DATAGRAM_BYTES: Final = 1800
MAX_DATAGRAMS: Final = 8
RECEIVE_BYTES: Final = 4096
RECEIVE_BUFFER: Final = 256 * 1024  # macOS defaults to 4096 (net.local.dgram.recvspace)


def activity_dir(state_root: Path) -> Path:
    return state_root / ACTIVITY_DIR


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    graph: str
    kind: str
    ids: tuple[str, ...]


def encode_reads(graph: GraphId, ids: Sequence[EntityId]) -> list[bytes]:
    """The datagrams for one read: unique ids in first-seen order, split by size."""
    texts = list(dict.fromkeys(str(i) for i in ids))
    datagrams: list[bytes] = []
    chunk: list[str] = []
    for text in texts:
        if chunk and len(_payload(graph, [*chunk, text])) > MAX_DATAGRAM_BYTES:
            datagrams.append(_payload(graph, chunk))
            if len(datagrams) == MAX_DATAGRAMS:
                return datagrams
            chunk = []
        chunk.append(text)
    if chunk:
        datagrams.append(_payload(graph, chunk))
    return datagrams


def _payload(graph: GraphId, ids: list[str]) -> bytes:
    ids_json: list[JsonValue] = list(ids)
    return canonical_bytes(
        {"v": PAYLOAD_VERSION, "graph": str(graph), "kind": "read", "ids": ids_json}
    )


def decode(data: bytes) -> ActivityEvent | None:
    """One datagram, or ``None`` for anything that is not a version 1 payload."""
    try:
        document = parse_json(data)
    except CanonicalError:
        return None
    if not isinstance(document, dict) or document.get("v") != PAYLOAD_VERSION:
        return None
    graph, kind, ids = document.get("graph"), document.get("kind"), document.get("ids")
    if not isinstance(graph, str) or kind != "read" or not isinstance(ids, list):
        return None
    if not all(isinstance(i, str) for i in ids):
        return None
    return ActivityEvent(graph, kind, tuple(str(i) for i in ids))


class SocketActivity:
    """The app side: sends reads to every viewer socket in ``directory``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def reads(self, graph: GraphId, ids: Sequence[EntityId]) -> None:
        if not ids:
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
            datagrams = encode_reads(graph, ids)
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
            os.chmod(path, 0o600)
        except OSError:
            sock.close()
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
