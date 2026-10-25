"""The activity signal over Unix datagram sockets (graph view plan, step 1)."""

import dataclasses
import json
import socket
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path
from uuid import UUID

import pytest
from support.activity import short_dir
from support.services import app_actor

from graph_cli.catalog.activity import (
    MAX_DATAGRAM_BYTES,
    MAX_DATAGRAMS,
    MAX_TEXT,
    ActivityEvent,
    ActivityListener,
    SocketActivity,
    decode,
    encode_reads,
    remove_stale,
)
from graph_cli.kernel.actor import Assurance, Facet
from graph_cli.kernel.ids import EdgeId, EntityId, GraphId
from graph_cli.kernel.ports.activity import NULL_ACTIVITY, ReadSignal

GRAPH = GraphId(UUID("0192f3c0-0000-7000-8000-000000000001"))
AGENT = dataclasses.replace(
    app_actor(),
    agent=Facet(Assurance.ASSERTED, name="example_agent"),
    session=Facet(Assurance.ASSERTED, id="session-1"),
)


def entity(n: int) -> EntityId:
    return EntityId(UUID(f"0192f3c0-0000-7000-8000-{n:012x}"))


def edge(n: int) -> EdgeId:
    return EdgeId(UUID(f"0192f3c0-0000-7000-9000-{n:012x}"))


def signal(
    entities: Sequence[EntityId], edges: Sequence[EdgeId] = (), command: str = "neighbors"
) -> ReadSignal:
    return ReadSignal(GRAPH, command, AGENT, entities, edges)


@pytest.fixture
def directory() -> Iterator[Path]:
    with short_dir() as path:
        yield path / "activity"


def test_payload_round_trips() -> None:
    [datagram] = encode_reads(signal([entity(2), entity(1), entity(2)], [edge(1)]), "c0ffee00")
    assert json.loads(datagram) == {
        "actor": "example_agent",
        "call": "c0ffee00",
        "command": "neighbors",
        "edges": [str(edge(1))],
        "graph": str(GRAPH),
        "ids": [str(entity(2)), str(entity(1))],
        "kind": "read",
        "session": "session-1",
        "v": 2,
    }
    assert decode(datagram) == ActivityEvent(
        str(GRAPH),
        "read",
        (str(entity(2)), str(entity(1))),
        (str(edge(1)),),
        "neighbors",
        "c0ffee00",
        "example_agent",
        "session-1",
    )


def test_without_agent_or_session_the_principal_names_the_reader() -> None:
    [datagram] = encode_reads(ReadSignal(GRAPH, "get", app_actor(), [entity(1)]))
    document = json.loads(datagram)
    assert (document["actor"], document["session"], document["edges"]) == ("tester", None, [])
    assert len(document["call"]) == 8


def test_long_actor_texts_are_cut() -> None:
    long = dataclasses.replace(
        AGENT,
        agent=Facet(Assurance.VERIFIED, name="a" * 500),
        session=Facet(Assurance.VERIFIED, id="s" * 500),
    )
    [datagram] = encode_reads(ReadSignal(GRAPH, "get", long, [entity(1)]))
    document = json.loads(datagram)
    assert (len(document["actor"]), len(document["session"])) == (MAX_TEXT, MAX_TEXT)


def test_version_1_datagrams_are_still_read() -> None:
    data = json.dumps({"v": 1, "graph": "g", "kind": "read", "ids": ["a"]}).encode()
    assert decode(data) == ActivityEvent("g", "read", ("a",))


def test_large_reads_are_split_and_capped() -> None:
    ids = [entity(n) for n in range(1, 1001)]
    datagrams = encode_reads(signal(ids, [edge(1)]))
    assert len(datagrams) == MAX_DATAGRAMS
    assert all(len(d) <= MAX_DATAGRAM_BYTES for d in datagrams)
    documents = [json.loads(d) for d in datagrams]
    sent = [i for d in documents for i in d["ids"]]
    assert sent == [str(i) for i in ids[: len(sent)]]
    assert 250 < len(sent) < 1000
    assert len({d["call"] for d in documents}) == 1
    assert documents[0]["edges"] == [str(edge(1))]  # in turn with the entities: it fits


def test_a_cut_read_keeps_entities_and_edges_in_turn() -> None:
    ids = [entity(n) for n in range(1, 1001)]
    edges = [edge(n) for n in range(1, 1001)]
    documents = [json.loads(d) for d in encode_reads(signal(ids, edges))]
    sent = [i for d in documents for i in d["ids"]]
    walked = [e for d in documents for e in d["edges"]]
    assert sent == [str(i) for i in ids[: len(sent)]]
    assert walked == [str(e) for e in edges[: len(walked)]]
    assert abs(len(sent) - len(walked)) <= 1 and len(sent) > 100


def test_edges_follow_the_entities_in_the_same_call() -> None:
    ids = [entity(n) for n in range(1, 61)]
    edges = [edge(n) for n in range(1, 61)]
    documents = [json.loads(d) for d in encode_reads(signal(ids, edges))]
    assert 1 < len(documents) <= MAX_DATAGRAMS
    assert [i for d in documents for i in d["ids"]] == [str(i) for i in ids]
    assert [e for d in documents for e in d["edges"]] == [str(e) for e in edges]
    assert len({d["call"] for d in documents}) == 1


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"v": 3, "graph": "g", "kind": "read", "ids": []}',
        b'{"v": true, "graph": "g", "kind": "read", "ids": []}',
        b'{"v": 2, "graph": "g", "kind": "read", "ids": []}',
        b'{"v": 2, "graph": "g", "kind": "read", "command": "get", "call": "c", "actor": "a",'
        b' "session": 1, "ids": [], "edges": []}',
        b'{"v": 2, "graph": "g", "kind": "read", "command": "get", "call": "c", "actor": "a",'
        b' "session": null, "ids": [], "edges": [2]}',
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
        SocketActivity(directory).reads(signal(ids))
        events = listener.receive(1.0)
        assert len(events) > 1
        assert [i for e in events for i in e.ids] == [str(i) for i in ids]
        assert listener.receive(0.01) == []
    finally:
        listener.close()
    assert not listener.path.exists()


def test_sending_never_fails(directory: Path) -> None:
    activity = SocketActivity(directory)
    activity.reads(signal([entity(1)]))  # no directory
    directory.mkdir()
    activity.reads(signal([entity(1)]))  # no socket
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    stale.bind(str(directory / "v1-dead.sock"))
    stale.close()  # the file stays, nobody listens
    (directory / "v2-file.sock").write_text("not a socket")
    listener = ActivityListener.open(directory)
    try:
        activity.reads(signal([entity(1)]))
        activity.reads(signal([]))
        assert [e.ids for e in listener.receive(1.0)] == [(str(entity(1)),)]
    finally:
        listener.close()
    NULL_ACTIVITY.reads(signal([entity(1)]))


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


def test_a_socket_that_cannot_be_protected_is_not_left_behind(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: object, mode: int) -> None:
        if str(path).endswith(".sock"):
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr("graph_cli.catalog.activity.os.chmod", refuse)
    with pytest.raises(OSError):
        ActivityListener.open(directory)
    assert list(directory.iterdir()) == []
