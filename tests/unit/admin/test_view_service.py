"""The live graph view's data on seeded file graphs of both fixtures (graph view plan, step 3)."""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import TOY_ONTOLOGIES, compiled_model
from support.roles import load_roles
from support.services import ServiceEnv
from support.snapshots import revision as stored_revision

from graph_cli.catalog.activity import ActivityEvent
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli_admin.services_admin import view
from graph_cli_admin.services_admin.view import (
    ActivityTrail,
    ViewFeed,
    ViewGraphs,
    graph_json,
    label_of,
    open_view,
    read_event,
)


@pytest.fixture(params=TOY_ONTOLOGIES)
def svc(request: pytest.FixtureRequest, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv("files", str(request.param), tmp_path / "root")
    assert env.apply_seed().error is None
    return env


def source_of(svc: ServiceEnv) -> view.ViewSource:
    return open_view(admin_context(svc.root), graph_ref=svc.name)


def test_the_whole_graph_with_labels_and_links(svc: ServiceEnv) -> None:
    source = source_of(svc)
    data = graph_json(source)
    revisions = svc.all_revisions()
    nodes = data["nodes"]
    assert isinstance(nodes, list) and data["seq"] == svc.head_seq()
    assert sorted(n["id"] for n in nodes) == sorted(str(r.id) for r in revisions)  # type: ignore[index]
    assert {n["status"] for n in nodes} == {"active", "superseded", "retracted"}  # type: ignore[index]
    assert data["graph"] == {"id": str(svc.entry.id), "name": svc.name}
    assert data["types"] == sorted({r.type for r in revisions})
    known = {str(r.id) for r in revisions}
    links = data["links"]
    assert isinstance(links, list) and links
    assert all(link["source"] in known and link["target"] in known for link in links)  # type: ignore[index]
    by_id = {n["id"]: n for n in nodes}  # type: ignore[index]
    for revision in revisions:
        node = by_id[str(revision.id)]
        assert set(node) == {"id", "label", "type", "alias", "status"}  # no attributes
        labels = [
            revision.attrs[a]
            for a, role in source.model.label_attributes(revision.type)
            if role == "label" and revision.attrs.get(a)
        ]
        fallback = f"{revision.type.rpartition(':')[2]} {str(revision.id)[-8:]}"
        expected = labels[0] if labels else revision.alias or fallback
        assert node["label"] == expected


def test_the_feed_turns_commits_into_events(
    svc: ServiceEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles = load_roles(svc.ontology)
    source = source_of(svc)
    feed = ViewFeed(source, svc.head_seq())
    assert feed.poll() == []
    cls = roles.term("lifecycle_type")
    svc.create(cls)  # with the entities its required relations need
    events = feed.poll()
    (tx,) = svc.txs()[-1:]
    assert [e["node"]["id"] for e in events] == sorted(str(r.id) for r in tx.revisions)  # type: ignore[index, call-overload]
    assert {(e["kind"], e["seq"], e["actor"]) for e in events} == {("created", tx.seq, "tester")}
    assert {(e["command"], e["session"]) for e in events} == {
        (tx.request.command, tx.actor.session_key())
    }
    (created,) = [e for e in events if e["node"]["type"] == cls]  # type: ignore[index, call-overload]
    entity = created["node"]["id"]  # type: ignore[index, call-overload]
    revision = next(r for r in tx.revisions if str(r.id) == entity)
    assert len(created["links"]) == len(revision.edges)  # type: ignore[arg-type]
    attrs = svc.factory.attrs(cls)
    attr = next(n for n in attrs if isinstance(attrs[n], str))
    svc.apply({"op": "update", "id": entity, "set": {attr: "changed"}})
    assert [(e["kind"], e["node"]["id"]) for e in feed.poll()] == [("updated", entity)]  # type: ignore[index, call-overload]
    monkeypatch.setattr(view, "PAGE", 1)
    for local in ("a", "b", "c"):
        svc.create(cls, local)
    events = feed.poll()
    assert {e["kind"] for e in events} == {"created"} and len({e["seq"] for e in events}) == 3
    assert feed.seq == svc.head_seq() and feed.poll() == []


RECEIVED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def read(
    ids: tuple[str, ...], call: str | None = "c1", edges: tuple[str, ...] = ()
) -> dict[str, JsonValue]:
    return {
        "kind": "read",
        "ts": "2026-01-02T03:04:05.000Z",
        "command": "neighbors",
        "call": call,
        "actor": "example_agent",
        "session": "session-1",
        "ids": list(ids),
        "edges": list(edges),
    }


def test_reads_of_this_graph_only(svc: ServiceEnv) -> None:
    source = source_of(svc)
    ids = ("0192f3c0-0000-7000-8000-000000000001",)
    event = ActivityEvent(
        str(svc.entry.id), "read", ids, ("e1",), "neighbors", "c1", "example_agent", "session-1"
    )
    assert read_event(source, event, RECEIVED) == read(ids, edges=("e1",))
    old = ActivityEvent(str(svc.entry.id), "read", ids)  # version 1: ids only
    assert read_event(source, old, RECEIVED) == {
        **read(ids, call=None),
        "command": None,
        "actor": None,
        "session": None,
    }
    other = dataclasses.replace(event, graph="0192f3c0-0000-7000-8000-0000000000ff")
    assert read_event(source, other, RECEIVED) is None


def test_the_trail_merges_the_datagrams_of_one_read() -> None:
    trail = ActivityTrail()
    assert trail.add(read(("a", "b"), edges=("e1",)))["step"] == 1
    assert trail.add(read(("b", "c"), edges=("e2",)))["step"] == 1  # same call
    write: dict[str, JsonValue] = {
        "kind": "created",
        "seq": 7,
        "ts": "2026-01-02T03:04:06.000Z",
        "command": "apply",
        "actor": "example_agent",
        "session": "session-1",
        "node": {"id": "d"},
        "links": [],
    }
    assert trail.add(write)["step"] == 2
    assert trail.add(read(("a",), call=None))["step"] == 3  # version 1: always a new step
    steps = trail.to_json()["steps"]
    assert isinstance(steps, list)
    assert [(s["step"], s["kind"], s["ids"], s["edges"]) for s in steps] == [  # type: ignore[index, call-overload]
        (1, "read", ["a", "b", "c"], ["e1", "e2"]),
        (2, "created", ["d"], []),
        (3, "read", ["a"], []),
    ]
    assert steps[0] == {  # type: ignore[comparison-overlap]
        "step": 1,
        "kind": "read",
        "ts": "2026-01-02T03:04:05.000Z",
        "command": "neighbors",
        "actor": "example_agent",
        "session": "session-1",
        "ids": ["a", "b", "c"],
        "edges": ["e1", "e2"],
    }
    assert steps[1]["seq"] == 7  # type: ignore[index, call-overload]


def test_the_trail_keeps_the_last_steps() -> None:
    trail = ActivityTrail(limit=2)
    for n in range(1, 5):
        trail.add(read((str(n),), call=f"c{n}"))
    steps = trail.to_json()["steps"]
    assert [s["step"] for s in steps] == [3, 4]  # type: ignore[index, union-attr]
    assert trail.add(read(("x",), call="c1"))["step"] == 5  # c1 left the trail


def test_a_denied_view_is_audited(svc: ServiceEnv) -> None:
    deny_all = PolicySet(
        "sha256:" + "1" * 64,
        ({"schema": "graph_cli.policy/1", "id": "none", "rules": [], "obligations": []},),
    )

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return deny_all

    ctx = dataclasses.replace(admin_context(svc.root), policy=Policy())
    with pytest.raises(GraphCliError) as info:
        open_view(ctx, graph_ref=svc.name)
    assert info.value.code == "access.denied"
    assert svc.installation_events()[-1]["result"] == {"status": "denied", "code": "access.denied"}


def test_the_list_leaves_out_unreadable_graphs_without_an_audit_event(svc: ServiceEnv) -> None:
    deny_all = PolicySet(
        "sha256:" + "1" * 64,
        ({"schema": "graph_cli.policy/1", "id": "none", "rules": [], "obligations": []},),
    )

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return deny_all

    assert [g["name"] for g in ViewGraphs(admin_context(svc.root)).listing()["graphs"]] == [  # type: ignore[index, union-attr]
        svc.name
    ]
    before = len(svc.installation_events())
    ctx = dataclasses.replace(admin_context(svc.root), policy=Policy())
    assert ViewGraphs(ctx).listing() == {"default": None, "graphs": []}
    assert len(svc.installation_events()) == before


def test_a_graph_opens_once_and_again_when_its_manifest_changes(svc: ServiceEnv) -> None:
    graphs = ViewGraphs(admin_context(svc.root))
    opened = graphs.get(svc.name)
    assert graphs.get(str(svc.entry.id)) is opened and graphs.opened() == [opened]
    source = opened.source
    svc.set_entry(title="Another purpose")
    again = graphs.get(svc.name)
    assert again is opened and again.source is not source  # same feed and trail
    assert again.feed.source is again.source and again.source.entry.title == "Another purpose"
    assert graphs.find(str(svc.entry.id)) is opened and graphs.find("other") is None


def test_a_language_tagged_label_is_shown_in_the_graph_language() -> None:
    model = compiled_model("notes_and_sources", "1.2.0")
    entry = stored_revision(
        "g",
        "nas:GlossaryEntry",
        {"heading": [{"lang": "en", "value": "Control"}, {"lang": "de", "value": "Kontrolle"}]},
    )
    assert label_of(model, entry, "en") == "Control"
    assert label_of(model, entry) == "Kontrolle"  # no language: the smallest tag
