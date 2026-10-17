"""The live graph view's data on seeded file graphs of both fixtures (graph view plan, step 3)."""

import dataclasses
from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import TOY_ONTOLOGIES
from support.roles import load_roles
from support.services import ServiceEnv

from graph_cli.catalog.activity import ActivityEvent
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli_admin.services_admin import view
from graph_cli_admin.services_admin.view import ViewFeed, graph_json, open_view, read_event


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


def test_reads_of_this_graph_only(svc: ServiceEnv) -> None:
    source = source_of(svc)
    ids = ("0192f3c0-0000-7000-8000-000000000001",)
    assert read_event(source, ActivityEvent(str(svc.entry.id), "read", ids)) == {
        "kind": "read",
        "ids": list(ids),
    }
    assert (
        read_event(source, ActivityEvent("0192f3c0-0000-7000-8000-0000000000ff", "read", ids))
        is None
    )


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
