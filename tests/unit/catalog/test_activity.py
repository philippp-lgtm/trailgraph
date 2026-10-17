"""The activity signal over Unix datagram sockets (graph view plan, step 1)."""

import json
import socket
import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from support.activity import short_dir

from graph_cli.catalog.activity import (
    MAX_DATAGRAM_BYTES,
    MAX_DATAGRAMS,
    ActivityListener,
    SocketActivity,
    decode,
    encode_reads,
    remove_stale,
)
from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.ports.activity import NULL_ACTIVITY

GRAPH = GraphId(UUID("0192f3c0-0000-7000-8000-000000000001"))


def entity(n: int) -> EntityId:
    return EntityId(UUID(f"0192f3c0-0000-7000-8000-{n:012x}"))


@pytest.fixture
def directory() -> Iterator[Path]:
    with short_dir() as path:
        yield path / "activity"


def test_payload_round_trips() -> None:
    [datagram] = encode_reads(GRAPH, [entity(2), entity(1), entity(2)])
    assert json.loads(datagram) == {
        "graph": str(GRAPH),
        "ids": [str(entity(2)), str(entity(1))],
        "kind": "read",
        "v": 1,
    }
    event = decode(datagram)
    assert event is not None
    assert (event.graph, event.kind, event.ids) == (
        str(GRAPH),
        "read",
        (str(entity(2)), str(entity(1))),
    )


def test_large_reads_are_split_and_capped() -> None:
    ids = [entity(n) for n in range(1, 1001)]
    datagrams = encode_reads(GRAPH, ids)
    assert len(datagrams) == MAX_DATAGRAMS
    assert all(len(d) <= MAX_DATAGRAM_BYTES for d in datagrams)
    sent = [i for d in datagrams for i in json.loads(d)["ids"]]
    assert sent == [str(i) for i in ids[: len(sent)]]
    assert 300 < len(sent) < 1000


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"v": 2, "graph": "g", "kind": "read", "ids": []}',
        b'{"v": 1, "graph": "g", "kind": "write", "ids": []}',
        b'{"v": 1, "graph": "g", "kind": "read", "ids": [1]}',
        b'{"v": 1, "graph": 3, "kind": "read", "ids": []}',
    ],
)
def test_malformed_datagrams_are_ignored(data: bytes) -> None:
    assert decode(data) is None


def test_listener_receives_every_chunk(directory: Path) -> None:
    listener = ActivityListener.open(directory)
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(listener.path.stat().st_mode) == 0o600
        ids = [entity(n) for n in range(1, 101)]
        SocketActivity(directory).reads(GRAPH, ids)
        events = listener.receive(1.0)
        assert len(events) > 1
        assert [i for e in events for i in e.ids] == [str(i) for i in ids]
        assert listener.receive(0.01) == []
    finally:
        listener.close()
    assert not listener.path.exists()


def test_sending_never_fails(directory: Path) -> None:
    activity = SocketActivity(directory)
    activity.reads(GRAPH, [entity(1)])  # no directory
    directory.mkdir()
    activity.reads(GRAPH, [entity(1)])  # no socket
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    stale.bind(str(directory / "v1-dead.sock"))
    stale.close()  # the file stays, nobody listens
    (directory / "v2-file.sock").write_text("not a socket")
    listener = ActivityListener.open(directory)
    try:
        activity.reads(GRAPH, [entity(1)])
        activity.reads(GRAPH, [])
        assert [e.ids for e in listener.receive(1.0)] == [(str(entity(1)),)]
    finally:
        listener.close()
    NULL_ACTIVITY.reads(GRAPH, [entity(1)])


def test_stale_sockets_are_removed(directory: Path) -> None:
    directory.mkdir()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    stale.bind(str(directory / "v1-dead.sock"))
    stale.close()
    live = ActivityListener.open(directory)  # removes the stale one
    try:
        assert not (directory / "v1-dead.sock").exists()
        assert remove_stale(directory) == []
        assert live.path.exists()
    finally:
        live.close()
