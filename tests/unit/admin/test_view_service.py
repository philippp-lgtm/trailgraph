"""The live graph view's data on seeded file graphs of both fixtures (graph view plan, step 3)."""

import dataclasses
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.fixtures import FIXTURES, TOY_ONTOLOGIES, compiled_model
from support.roles import load_roles
from support.services import ServiceEnv
from support.snapshots import revision as stored_revision
from support.txbuilder import actor

from graph_cli.catalog.activity import ActivityEvent
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.services.describe import (
    describe_module,
    describe_modules,
    describe_relations,
    describe_type,
)
from graph_cli_admin.services_admin import policy, tokens, view
from graph_cli_admin.services_admin.functions import publish
from graph_cli_admin.services_admin.view import (
    ActivityTrail,
    ViewFeed,
    ViewGraphs,
    functions_json,
    graph_json,
    label_of,
    model_json,
    open_view,
    policies_json,
    read_event,
)

FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
INSTALLATION_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps"}
grants:
  - {principal: {name: maintainer}, graphs: "*", level: write}
  - {entrypoint: server, principal: {project: example_project}, graphs: [GRAPH], level: read}
scopes:
  example_project: {GRAPH: read}
  other_project: {"*": none}
no_project: {"*": read}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""
GRAPH_POLICY = """\
schema: graph_cli.policy/1
id: graph
obligations:
  - {action: ["write.*"], max_batch: 200}
deny:
  - {id: no_history, action: ["read.history"]}
"""


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


def test_a_repeated_denial_is_audited_once_per_window(svc: ServiceEnv) -> None:
    deny_all = PolicySet(
        "sha256:" + "1" * 64,
        ({"schema": "graph_cli.policy/1", "id": "none", "rules": [], "obligations": []},),
    )

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return deny_all

    now = [100.0]
    ctx = dataclasses.replace(admin_context(svc.root), policy=Policy())
    graphs = ViewGraphs(ctx, monotonic=lambda: now[0])
    before = len(svc.installation_events())

    def refused() -> str:
        with pytest.raises(GraphCliError) as info:
            graphs.get(svc.name)
        return info.value.code

    assert [refused(), refused()] == ["access.denied", "access.denied"]
    assert len(svc.installation_events()) == before + 1  # a retrying page: one event
    now[0] += view.DENIAL_AUDIT_S
    assert refused() == "access.denied"
    assert len(svc.installation_events()) == before + 2
    assert graphs.opened() == []


def test_a_policy_applied_meanwhile_decides_the_next_request(
    svc: ServiceEnv, tmp_path: Path
) -> None:
    ctx = admin_context(svc.root)
    watched = dataclasses.replace(ctx, policy=FilePolicySource(svc.root, watch=True))
    graphs = ViewGraphs(watched)
    closed: list[str] = []
    graphs.on_close = closed.append
    graphs.get(svc.name)
    graphs.close_revoked()
    assert closed == [] and len(graphs.opened()) == 1
    authored = tmp_path / "installation.yaml"
    hidden = INSTALLATION_POLICY.replace('no_project: {"*": read}', 'no_project: {"*": none}')
    authored.write_text(hidden.replace("GRAPH", svc.name))
    policy.apply(ctx, file=authored, graph_ref=None, reason="test")
    assert graphs.listing()["graphs"] == []
    with pytest.raises(GraphCliError) as info:
        graphs.get(svc.name)
    assert info.value.code == "access.denied"
    graphs.close_revoked()  # the feed ends the open streams
    assert closed == [str(svc.entry.id)] and graphs.opened() == []


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


def policy_of(*actions: str) -> object:
    """A policy that lets the maintainer do ``actions`` on every graph and nothing else."""
    rules = [
        {
            "id": "only",
            "effect": "allow",
            "principal": {"entrypoint": "maint"},
            "action": list(actions),
            "resource": "*",
        }
    ]
    policy = PolicySet(
        "sha256:" + "2" * 64,
        ({"schema": "graph_cli.policy/1", "id": "only", "rules": rules, "obligations": []},),
    )

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return policy

    return Policy()


def test_the_model_is_what_describe_prints(svc: ServiceEnv) -> None:
    source = source_of(svc)
    entry, model = source.entry, source.model
    data = model_json(source)
    assert data["graph"] == {"id": str(entry.id), "name": svc.name, "language": entry.language}
    assert data["modules"] == describe_modules(entry, model)["modules"]  # no functions published
    assert data["module"] == {
        m.id: describe_module(entry, model, m.id)["module"] for m in model.modules
    }
    assert data["type"] == {c: describe_type(entry, model, c)["type"] for c in model.classes}
    assert data["relations"] == describe_relations(entry, model)["relations"]
    active = Counter(r.type for r in svc.all_revisions() if r.record_status == "active")
    assert data["entities"] == dict(sorted(active.items()))
    assert functions_json(source)["functions"] == []


def test_the_functions_in_full_only_with_read_function(tmp_path: Path) -> None:
    env = ServiceEnv("files", "inventory", tmp_path / "root")
    assert env.apply_seed().error is None
    ctx = admin_context(env.root)
    publish(ctx, graph_ref=env.name, source=FUNCTIONS, reason=None, dry_run=False)
    source = source_of(env)
    shown = functions_json(source)["functions"]
    assert isinstance(shown, list)
    assert sorted(f["name"] for f in shown) == sorted(p.stem for p in FUNCTIONS.glob("*.yaml"))  # type: ignore[index]
    assert all(f["query"] and f["hash"] and "use_when" in f for f in shown)  # type: ignore[index, operator]
    modules = model_json(source)["modules"]
    assert sum(m["functions"] for m in modules) == len(shown)  # type: ignore[index, misc]
    before = len(env.installation_events())
    hidden = dataclasses.replace(ctx, policy=policy_of("read.get", "read.describe"))
    source = open_view(hidden, graph_ref=env.name)
    assert functions_json(source)["functions"] == []
    assert all("functions" not in m for m in model_json(source)["modules"])  # type: ignore[operator]
    assert len(env.installation_events()) == before  # left out, not refused


def test_the_model_needs_read_describe(svc: ServiceEnv) -> None:
    now = [100.0]
    ctx = dataclasses.replace(admin_context(svc.root), policy=policy_of("read.get"))
    graphs = ViewGraphs(ctx, monotonic=lambda: now[0])
    entry = graphs.get(svc.name).source.entry
    before = len(svc.installation_events())

    def refused() -> str:
        with pytest.raises(GraphCliError) as info:
            graphs.authorize(entry, "read.describe")
        return info.value.code

    assert [refused(), refused()] == ["access.denied", "access.denied"]
    assert len(svc.installation_events()) == before + 1
    graphs.authorize(entry)  # read.get has its own window
    now[0] += view.DENIAL_AUDIT_S
    assert refused() == "access.denied"
    assert len(svc.installation_events()) == before + 2


def test_the_policies_as_they_bear_on_the_graph(svc: ServiceEnv, tmp_path: Path) -> None:
    ctx = admin_context(svc.root)
    data = policies_json(ctx, source_of(svc))
    builtin, own = data["layers"]  # type: ignore[misc]
    assert builtin["layer"] == "installation" and builtin["installed"] is False  # type: ignore[index]
    assert own == {  # no graph policy
        "layer": "graph",
        "installed": False,
        "hash": None,
        "rules": [],
        "obligations": [],
        "types": {},
    }
    assert data["callers"] == [  # only the scope for no project: the built-in policy has none
        {
            "project": None,
            "principal": ctx.actor.principal.id,
            "entrypoint": "app",
            "level": "write",
            "withheld": [],
            "maintains": False,
        }
    ]
    for name, text in (("installation", INSTALLATION_POLICY), ("graph", GRAPH_POLICY)):
        authored = tmp_path / f"{name}.yaml"
        authored.write_text(text.replace("GRAPH", svc.name))
        graph_ref = svc.name if name == "graph" else None
        policy.apply(ctx, file=authored, graph_ref=graph_ref, reason="test")
    tokens.create(ctx, name="ci", project="example_project", expires=None, reason=None)
    installed = dataclasses.replace(ctx, policy=FilePolicySource(svc.root))  # sees the roles
    tokens.create(installed, name="svc", project=None, expires=None, reason=None, roles=["reader"])
    data = policies_json(ctx, source_of(svc))
    assert data["graph"]["name"] == svc.name  # type: ignore[index, call-overload]
    assert data["graphs"] == {str(svc.entry.id): svc.name}
    installation, own = data["layers"]  # type: ignore[misc]
    assert installation["installed"] is True and own["installed"] is True  # type: ignore[index]
    kinds = {r["id"]: (r["kind"], r["level"]) for r in installation["rules"]}  # type: ignore[index]
    assert kinds == {
        "grant.1": ("grant", "write"),
        "grant.2": ("grant", "read"),
        "maint.any": ("maintainer", None),
        f"scope.example_project.{svc.name}": ("scope", None),
        # scope.example_project.* is lifted for the graphs the scope names: left out
        "scope.other_project.*": ("scope", None),
        "no_project.*": ("scope", None),
        "deny.no_explore_writes": ("deny", None),
    }
    assert [r["id"] for r in own["rules"]] == ["deny.no_history"]  # type: ignore[index]
    assert own["obligations"] == [{"action": ["write.*"], "max_batch": 200}]  # type: ignore[index]
    access = {(c.get("token"), c["project"]): c for c in data["callers"]}  # type: ignore[union-attr, index]
    assert data["projects"] == ["example_project", "other_project"]
    # the graph withholds the history from everyone: below the read level, still readable
    assert access[None, "example_project"]["withheld"] == ["read.history"]
    assert access[None, None]["withheld"] == ["read.history"]
    assert access[None, "other_project"]["withheld"] == []  # nothing to withhold from
    assert {c["level"] for c in access.values()} == {"none"}
    assert (
        access["svc", None]["roles"] == ["reader"]
        and access["ci", "example_project"]["roles"] == []
    )
    ci = access["ci", "example_project"]
    assert ci["entrypoint"] == "server" and ci["principal"] == "token:ci" and not ci["expired"]
    assert ci["withheld"] == ["read.history"] and ci["maintains"] is False


def test_the_policies_need_audit_read_on_the_installation(svc: ServiceEnv) -> None:
    ctx = dataclasses.replace(admin_context(svc.root), policy=policy_of("read.get"))
    graphs = ViewGraphs(ctx)
    entry = graphs.get(svc.name).source.entry
    before = len(svc.installation_events())
    with pytest.raises(GraphCliError) as info:
        graphs.authorize(entry, "maint.audit_read", "installation")
    assert info.value.code == "access.denied"
    assert len(svc.installation_events()) == before + 1


def test_an_actor_shows_its_roles_next_to_the_principal() -> None:
    plain = actor("token:ci")
    assert "roles" not in view._actor_json(plain)
    held = dataclasses.replace(plain, roles=("importer", "reader"))
    shown = view._actor_json(held)
    assert shown["roles"] == ["importer", "reader"]
    principal = shown["principal"]
    assert isinstance(principal, dict) and principal["name"] == plain.principal.name


WRITES_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers"}
grants:
  - entrypoint: app
    graphs: [GRAPH]
    level: read
    writes:
      nas:Note: {create: [title, body, kind], link: [nas:about]}
      nas:Topic: {update: [description]}
  - entrypoint: server
    principal: {role: importer}
    graphs: [GRAPH]
    level: read
    writes:
      nas:Topic: {create: true}
no_project: {"*": write}
"""


def test_the_policies_show_the_write_grants_and_what_each_caller_may_write(
    tmp_path: Path,
) -> None:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root")
    ctx = admin_context(env.root)
    authored = tmp_path / "installation.yaml"
    authored.write_text(WRITES_POLICY.replace("GRAPH", env.name))
    policy.apply(ctx, file=authored, graph_ref=None, reason="test")
    installed = dataclasses.replace(ctx, policy=FilePolicySource(env.root))  # sees the roles
    tokens.create(
        installed, name="svc", project=None, expires=None, reason=None, roles=["importer"]
    )
    tokens.create(installed, name="plain", project=None, expires=None, reason=None)
    data = policies_json(ctx, source_of_env(env))
    installation, _ = data["layers"]  # type: ignore[misc]
    rules = {r["id"]: r for r in installation["rules"]}  # type: ignore[index]
    graph = f"graph:{env.entry.id}"
    # the rules of the write verbs are grants, and show the resources they name
    assert {i: r["kind"] for i, r in rules.items() if i.startswith("grant.")} == {
        "grant.1": "grant",
        "grant.1.create": "grant",
        "grant.1.update": "grant",
        "grant.1.link": "grant",
        "grant.1.edge": "grant",
        "grant.2": "grant",
        "grant.2.create": "grant",
    }
    assert rules["grant.1.link"]["resource"] == [  # type: ignore[index]
        f"{graph}/type:nas:Note",
        f"{graph}/type:nas:Note/rel:nas:about",
    ]
    assert rules["grant.1.create"]["level"] is None  # a verb, not a level
    assert rules["grant.2.create"]["resource"] == [  # type: ignore[index]
        f"{graph}/type:nas:Topic",
        f"{graph}/type:nas:Topic/attr:*",
    ]
    assert data["graphs"] == {str(env.entry.id): env.name}  # the view names the graph
    access = {(c.get("token"), c["project"]): c for c in data["callers"]}  # type: ignore[union-attr, index]
    assert access[None, None]["level"] == "read"
    assert access[None, None]["writes"] == {
        "nas:Note": {"create": ["body", "kind", "title"], "link": ["nas:about"]},
        "nas:Topic": {"update": ["description"]},
    }
    assert access["svc", None]["writes"] == {"nas:Topic": {"create": True}}
    assert "writes" not in access["plain", None]  # no list: the answer is as before


READS_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting"}
  auditor: {description: "Auditors"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [GRAPH]
    level: read
    reads: [nas:Note, nas:WebPage]
  - entrypoint: server
    principal: {role: auditor}
    graphs: [GRAPH]
    level: read
  - entrypoint: app
    graphs: [GRAPH]
    level: read
no_project: {"*": write}
"""
READS_GRAPH_POLICY = """\
schema: graph_cli.policy/1
id: graph
types:
  nas:WebPage: none
"""


def test_the_policies_show_the_types_each_caller_sees(tmp_path: Path) -> None:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root")
    ctx = admin_context(env.root)
    authored = tmp_path / "installation.yaml"
    authored.write_text(READS_POLICY.replace("GRAPH", env.name))
    policy.apply(ctx, file=authored, graph_ref=None, reason="test")
    installed = dataclasses.replace(ctx, policy=FilePolicySource(env.root))  # sees the roles
    for name, roles in (("narrow", ["reporting"]), ("wide", ["reporting", "auditor"])):
        tokens.create(installed, name=name, project=None, expires=None, reason=None, roles=roles)
    rules = {r["id"]: r for r in policies_json(ctx, source_of_env(env))["layers"][0]["rules"]}  # type: ignore[index]
    # the compiled read rules are grants, as the write ones are
    assert rules["grant.1.reads"]["kind"] == "grant"
    assert rules["grant.1.reads"]["action"] == ["read.get"]

    def callers() -> dict[str | None, dict[str, JsonValue]]:
        data = policies_json(ctx, source_of_env(env))
        return {c.get("token"): c for c in data["callers"]}  # type: ignore[union-attr, index, misc]

    access = callers()
    assert access["narrow"]["reads"] == ["nas:Note", "nas:WebPage"]
    assert "reads" not in access["wide"] and "reads" not in access[None]
    # a graph layer that takes a type away limits every caller, and the limit of it is a type limit
    graph_policy = tmp_path / "graph.yaml"
    graph_policy.write_text(READS_GRAPH_POLICY)
    policy.apply(ctx, file=graph_policy, graph_ref=env.name, reason="test")
    access = callers()
    assert access["narrow"]["reads"] == ["nas:Note"]
    assert "nas:WebPage" not in access["wide"]["reads"]  # type: ignore[operator]
    assert "nas:Note" in access["wide"]["reads"]  # type: ignore[operator]
    assert access[None]["reads"] == access["wide"]["reads"]
    data = policies_json(ctx, source_of_env(env))
    own = data["layers"][1]["rules"]  # type: ignore[index]
    assert {r["id"]: r["kind"] for r in own}["type.nas:WebPage"] == "type_limit"  # type: ignore[index]


def source_of_env(env: ServiceEnv) -> view.ViewSource:
    return open_view(admin_context(env.root), graph_ref=env.name)


MARKINGS_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "People team"}
  reporting: {description: "Reporting"}
  payroll: {description: "Payroll"}
grants:
  - entrypoint: server
    principal: {role: hr}
    graphs: [GRAPH]
    level: read
    markings: [stf:personal]
  - {entrypoint: server, principal: {role: reporting}, graphs: [GRAPH], level: read}
  - entrypoint: server
    principal: {role: payroll}
    graphs: [GRAPH]
    level: read
    markings: [stf:personal, stf:pay]
  - {entrypoint: app, graphs: [GRAPH], level: read}
no_project: {"*": write}
"""


def test_the_policies_show_the_markings_each_caller_holds(tmp_path: Path) -> None:
    env = ServiceEnv("files", "staff", tmp_path / "root")
    ctx = admin_context(env.root)
    authored = tmp_path / "installation.yaml"
    authored.write_text(MARKINGS_POLICY.replace("GRAPH", env.name))
    policy.apply(ctx, file=authored, graph_ref=None, reason="test")
    installed = dataclasses.replace(ctx, policy=FilePolicySource(env.root))  # sees the roles
    for name, roles in (
        ("hr", ["hr"]),
        ("reporting", ["reporting"]),
        ("payroll", ["payroll"]),
        ("both", ["hr", "reporting"]),
    ):
        tokens.create(installed, name=name, project=None, expires=None, reason=None, roles=roles)
    for name, project in (("ops", None), ("padmin", "example_project")):
        tokens.create(
            installed, name=name, project=project, expires=None, reason=None, scope="admin"
        )
    data = policies_json(ctx, source_of_env(env))
    rules = {r["id"]: r for r in data["layers"][0]["rules"]}  # type: ignore[index]
    graph = str(env.entry.id)
    # the compiled marking rules are grants, and the list of rules that decide on the graph
    # includes them (their resources are markings of the graph, not the graph itself)
    assert {i: r["kind"] for i, r in rules.items() if i.endswith(".markings")} == {
        "grant.1.markings": "grant",
        "grant.3.markings": "grant",
    }
    assert rules["grant.1.markings"]["action"] == ["read.get"]
    assert rules["grant.1.markings"]["resource"] == [f"marking:{graph}/stf:personal"]
    assert rules["grant.3.markings"]["resource"] == [
        f"marking:{graph}/stf:pay",
        f"marking:{graph}/stf:personal",
    ]
    access = {c.get("token"): c for c in data["callers"]}  # type: ignore[union-attr, index, misc]
    assert access["hr"]["markings"] == ["stf:personal"]
    assert access["both"]["markings"] == ["stf:personal"]
    assert access["reporting"]["markings"] == []
    assert access[None]["markings"] == []  # the app caller of this principal holds none
    assert "markings" not in access["payroll"]  # holds everything the model uses
    assert "markings" not in access["ops"]  # the installation admin maintains: holds every one
    assert access["padmin"]["markings"] == []  # a project admin reads no data: no marking
    # a marking a caller lacks is named nowhere in its entry
    assert "stf:pay" not in json.dumps(access["hr"])
    # a graph that uses no marking: no caller gets the key
    plain = ServiceEnv("files", "notes_and_sources", tmp_path / "plain_root")
    plain_ctx = admin_context(plain.root)
    callers = policies_json(plain_ctx, source_of_env(plain))["callers"]
    assert all("markings" not in c for c in callers)  # type: ignore[union-attr, operator]
