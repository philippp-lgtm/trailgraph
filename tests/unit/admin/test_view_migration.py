"""The live view around a data migration (migration plan M10).

A tx with more revisions than a client queue holds is one ``reload`` event, a client that fell
behind gets one in place of what it missed, a graph whose pin moved (a release, or a migration
committed and not yet pinned) is opened again under the target model and its clients are told to
reload.
"""

import http.client
import json
import queue
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from support.admin import write_documents
from support.admin_context import admin_context
from support.fixtures import ONTOLOGIES
from support.stores import FaultSwitch

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.maintenance_cli import view_server
from graph_cli_admin.maintenance_cli.commands.view import page
from graph_cli_admin.maintenance_cli.view_server import Hub, ViewServer
from graph_cli_admin.services_admin import graphs, view
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.release import release
from graph_cli_admin.services_admin.view import ViewFeed, ViewGraphs, graph_json, open_view

INVENTORY = ONTOLOGIES / "inventory"
MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""


def item(local: str, name: str, condition: str) -> dict[str, Any]:
    return {
        "op": "create",
        "type": "inv:Item",
        "local": local,
        "attrs": {"name": name, "condition": condition},
        "edges": [
            {"rel": "inv:located_in", "target": "@room"},
            {"rel": "inv:in_category", "target": "@tools"},
        ],
    }


SEED: list[dict[str, Any]] = [
    {"op": "create", "type": "inv:Location", "local": "room", "attrs": {"name": "R", "code": "R1"}},
    {"op": "create", "type": "inv:Category", "local": "tools", "attrs": {"name": "Tools"}},
    item("hammer", "Hammer", "worn"),
    item("saw", "Saw", "good"),
]


class Scene:
    """A graph at 1.1.0 with two items, and the 2.0.0 directory that migrates them."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.source = tmp_path / "v2"
        shutil.copytree(INVENTORY / "2.0.0", self.source)
        (self.source / "migration.yaml").write_text(MIGRATION)
        self.ctx: AdminContext = admin_context(self.root)
        graphs.create(
            self.ctx,
            name="example_graph",
            source=INVENTORY / "1.1.0",
            module=None,
            title=None,
            language=None,
        )
        write_documents(self.root, "example_graph", [{"ops": SEED}])
        self.graph = str(self.ctx.catalog.resolve("example_graph").id)

    def release(self, faults: FaultSwitch | None = None) -> None:
        release(
            admin_context(self.root, faults),
            graph_ref="example_graph",
            source=self.source,
            module=None,
            reason="example reason",
        )

    def interrupt(self) -> None:
        """A crash between the migration tx and the pin switch."""
        faults = FaultSwitch()
        faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
        with pytest.raises(GraphCliError):
            self.release(faults)


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path)


def labels(data: dict[str, Any]) -> list[str]:
    return sorted(n["label"] for n in data["nodes"] if n["type"] == "inv:Item")


def test_a_tx_with_many_revisions_is_one_reload_event(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = open_view(scene.ctx, graph_ref="example_graph")
    feed = ViewFeed(source, source.read.head().seq)
    monkeypatch.setattr(view, "RELOAD_REVISIONS", 1)
    misc = {"op": "create", "type": "inv:Category", "attrs": {"name": "Misc"}}
    write_documents(scene.root, "example_graph", [{"ops": [misc]}], seed=14)
    assert [e["kind"] for e in feed.poll()] == ["created"]  # one revision: as before
    scene.release()  # rewrites the two items
    (event,) = feed.poll()
    assert event["kind"] == "reload" and event["reason"] == "large_tx"
    assert event["revisions"] == 2 and event["seq"] == source.read.head().seq
    assert set(event) == {"kind", "reason", "seq", "ts", "command", "actor", "revisions"}
    assert feed.seq == source.read.head().seq and feed.poll() == []


def test_the_default_threshold_sits_below_the_client_queue() -> None:
    assert 0 < view.RELOAD_REVISIONS < view_server.CLIENT_QUEUE


def test_a_client_that_falls_behind_gets_one_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(view_server, "CLIENT_QUEUE", 3)
    hub = Hub()
    client = hub.subscribe("g")
    for number in range(6):
        hub.publish("g", {"kind": "updated", "seq": number})
    found = []
    while True:
        try:
            found.append(client.get_nowait())
        except queue.Empty:
            break
    # the queue held three; the fourth replaced them by a reload, the rest followed it
    assert found == [
        {"kind": "reload", "reason": "lagging"},
        {"kind": "updated", "seq": 4},
        {"kind": "updated", "seq": 5},
    ]
    # a stop is never lost to a full queue
    for number in range(3):
        hub.publish("g", {"kind": "updated", "seq": number})
    hub.close("g")
    stream = []
    while True:
        try:
            stream.append(client.get_nowait())
        except queue.Empty:
            break
    assert stream[-1] is None


def test_the_view_shows_an_unpinned_migration_under_the_target_model(scene: Scene) -> None:
    """Between the migration tx and the pin switch the data has the new names: the view reads
    them under the target version, as the app does."""
    assert labels(graph_json(open_view(scene.ctx, graph_ref="example_graph"))) == [
        "Hammer",
        "Saw",
    ]
    scene.interrupt()
    assert scene.ctx.catalog.resolve("example_graph").pin.version == "1.1.0"  # the manifest
    source = open_view(scene.ctx, graph_ref="example_graph")
    assert source.entry.pin.version == "2.0.0" and source.entry.migration is not None
    assert labels(graph_json(source)) == ["Hammer", "Saw"]
    scene.release()  # finishes it
    done = open_view(scene.ctx, graph_ref="example_graph")
    assert done.entry.pin.version == "2.0.0" and done.entry.migration is None


def test_an_open_graph_follows_a_pin_that_moves(scene: Scene) -> None:
    graphs_open = ViewGraphs(scene.ctx, default="example_graph")
    opened = graphs_open.get("example_graph")
    assert graphs_open.refresh_models() == []
    before = opened.source
    assert before.entry.pin.version == "1.1.0"
    scene.release()
    assert graphs_open.refresh_models() == [scene.graph]  # the feed thread's round, no request
    assert opened.source is not before and opened.feed.source is opened.source
    assert opened.source.entry.pin.version == "2.0.0"
    assert graphs_open.refresh_models() == []
    assert graphs_open.get("example_graph") is opened


@pytest.fixture
def running(scene: Scene) -> Iterator[ViewServer]:
    server = ViewServer(ViewGraphs(scene.ctx, default="example_graph"), page=page(), poll_s=0.05)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def stream_events(response: http.client.HTTPResponse, until: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    while True:
        line = response.fp.readline()
        assert line, f"the stream ended after {found}"
        if line.startswith(b"data: "):
            found.append(json.loads(line[6:]))
            if until(found):
                return found


def test_a_migration_reaches_the_event_stream_as_reloads(
    scene: Scene, running: ViewServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(view, "RELOAD_REVISIONS", 1)
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=10)
    connection.putrequest("GET", f"/events?t={running.token}&graph=example_graph", skip_host=True)
    connection.putheader("Host", f"127.0.0.1:{running.port}")
    connection.endheaders()
    stream = connection.getresponse()
    assert stream.status == 200 and stream.fp.readline() == b": connected\n"
    scene.release()
    found = stream_events(
        stream,
        lambda seen: (
            {e.get("reason") for e in seen if e["kind"] == "reload"} >= {"model", "large_tx"}
        ),
    )
    assert {e["kind"] for e in found} == {"reload"}  # no event per revision, no trail step
    trail = running.graphs.get("example_graph").trail.to_json()
    assert trail["steps"] == []
    # the page that reloads gets the graph under the new model, and the seq the tx is in
    reloaded = running.graph(running.graphs.get("example_graph"))
    assert labels(reloaded) == ["Hammer", "Saw"]
    assert reloaded["seq"] >= max(e["seq"] for e in found if "seq" in e)
    connection.close()
