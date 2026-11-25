"""C: type read limits end to end through the server in-process (type read limits plan V1-V11).

The roles ``reporting`` and ``auditor`` are installed with the real ``graph-admin``. ``reporting``
reads notes and web pages of ``example_graph`` and may write notes (``reads`` and ``writes`` on
one grant), ``auditor`` reads everything. Token A holds the first role, token B both. Requests go
through ``ServerApp`` without a socket.

A gets every read command (get, find, lookup, search, neighbors, query, run, history, changes,
``--as-of``, ``describe *``, the graph skill) without a trace of the other types: no id, alias,
label, count, relation, term or skill text of a hidden type, scanned in every answer. What A asks
about a hidden entity, type, function or action is answered as about one that does not exist. B,
with both roles, sees everything. A graph policy that denies ``read.get`` on one more type hides
it from both. A's writes keep what A cannot see and treat a hidden type as an unknown one.
``whoami`` says what was observed, the refusals are audited with the decision that hides the type
and ``audit verify`` stays clean.
"""

import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init, write_documents
from support.app import app, create_graph
from support.policies import write_policy
from support.read_limits import NOT_THERE
from support.server import Reply, call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: notes and web pages only"}
  auditor: {description: "Auditors: everything"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [nas:Note, nas:WebPage]
    writes:
      nas:Note: {create: true, update: true, link: true}
  - entrypoint: server
    principal: {role: auditor}
    graphs: [example_graph]
    level: read
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
GRAPH_POLICY = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: hide_pages, action: [read.get], type: [nas:WebPage]}
"""
NOTES_BY_KIND = """\
schema: graph_cli.function/1
name: notes_by_kind
summary: Notes of a given kind.
use_when: Which notes are questions; a list of notes by kind.
params:
  kind:
    type: enum
    enum: nas:NoteKind
    summary: The kind to look for.
query: |
  MATCH (n:Note)
  WHERE n.kind = $kind
  RETURN n.title AS note, n.kind AS kind
  ORDER BY note
returns:
  - {column: note, summary: Title of the note.}
  - {column: kind, summary: "The kind, as asked for."}
examples:
  - question: Which notes are questions?
    params: {kind: question}
featured: true
"""
NOTE_SOURCES = """\
schema: graph_cli.function/1
name: note_sources
summary: The sources every note cites.
use_when: Which sources a note cites; where the notes come from.
params: {}
query: |
  MATCH (n:Note)-[:cites]->(s:Source)
  RETURN n.title AS note, s.title AS source
  ORDER BY note, source
returns:
  - {column: note, summary: Title of the note.}
  - {column: source, summary: Title of the cited source.}
examples:
  - question: Where do the notes come from?
"""
NOTES_ON_TOPIC = """\
schema: graph_cli.function/1
name: notes_on_topic
summary: Notes about a topic.
use_when: What is written about a subject.
params:
  topic:
    type: ref
    class: nas:Topic
    summary: The topic to list.
query: |
  MATCH (n:Note)-[:about]->(t:Topic)
  WHERE t.id = $topic
  RETURN n.title AS note
  ORDER BY note
returns:
  - {column: note, summary: Title of the note.}
examples:
  - question: What do we know about the first subject?
"""
RENAME_NOTE = """\
schema: graph_cli.action/1
name: rename_note
summary: Give a note another title.
use_when: A note needs a better headline.
params:
  note:
    type: ref
    class: nas:Note
    summary: The note.
  title:
    type: string
    summary: The new title.
ops:
  - {op: update, id: $note, set: {title: $title}}
examples:
  - request: Call the note about the harvest "Harvest report".
"""
NOTE_ON_TOPIC = """\
schema: graph_cli.action/1
name: note_on_topic
summary: Write a note about a topic.
use_when: Something is worth recording about a subject.
params:
  topic:
    type: ref
    class: nas:Topic
    summary: The subject of the note.
  title:
    type: string
    summary: Headline of the note.
ops:
  - op: create
    type: nas:Note
    attrs: {title: $title, body: text, kind: observation}
    edges:
      - {rel: nas:about, target: $topic}
examples:
  - request: Record that the first subject came up again.
"""
SKILL = """\
---
name: graph-example-graph
description: "Notes and the pages they cite in the test graph example_graph. Needs graph-cli."
---

# Graph `example_graph`

## When to use

Use it for questions about notes and their sources.

## Is it available here?

<!-- graph_cli:block start -->

## Subject areas

<!-- graph_cli:block modules -->

## Functions

<!-- graph_cli:block functions -->

## Changes

<!-- graph_cli:block actions -->

## Names and ids

<!-- graph_cli:block names -->

## Model

<!-- graph_cli:block model -->
"""


def build() -> list[dict[str, Any]]:
    def make(
        local: str, type_: str, attrs: dict[str, Any], *edges: dict[str, str]
    ) -> dict[str, Any]:
        return {"op": "create", "type": type_, "local": local, "attrs": attrs, "edges": list(edges)}

    def note(local: str, title: str, *edges: dict[str, str]) -> dict[str, Any]:
        attrs = {"title": title, "body": "text", "kind": "summary"}
        return make(local, "nas:Note", attrs, *edges)

    def edge(rel: str, target: str) -> dict[str, str]:
        return {"rel": rel, "target": target}

    return [
        {
            "ops": [
                make(
                    "t1",
                    "nas:Topic",
                    {"label": "Alpha topic", "synonyms": ["ATP"], "misspellings": ["Alfa topik"]},
                ),
                make("t2", "nas:Topic", {"label": "Beta topic"}, edge("nas:broader", "@t1")),
                make("w", "nas:WebPage", {"title": "Harvest page", "url": "https://example.org/p"}),
                make("b", "nas:Book", {"title": "Zebra handbook", "publisher": "Quagga Press"}),
                note("n", "First note", edge("nas:about", "@t1"), edge("nas:cites", "@w")),
                note("m", "Second note", edge("nas:about", "@t2"), edge("nas:cites", "@b")),
            ]
        },
        # the second revision of the first note only adds an edge to a hidden topic
        {
            "reason": "one more subject",
            "ops": [{"op": "link", "id": "@n", "rel": "nas:about", "target": "@t2"}],
        },
        {
            "reason": "cite the book too",
            "ops": [{"op": "link", "id": "@n", "rel": "nas:cites", "target": "@b"}],
        },
    ]


GRAPH = {"--graph": "example_graph"}
CONCRETE = (
    "files:File",
    "files:FormatMetadata",
    "nas:Book",
    "nas:Note",
    "nas:Scan",
    "nas:Topic",
    "nas:WebPage",
    "provenance:Claim",
)
# What names a type or relation of the hidden ones. Prose of the ontology ("topics", "about")
# is the maintainer's text and may stay; the names themselves may not.
HIDDEN_TERMS = (
    "nas:Topic",
    "nas:Book",
    "nas:Scan",
    "files:File",
    "provenance:Claim",
    "nas:about",
    "nas:broader",
    "nas:asserts",
    "has_note",
    "narrower",
    "asserted_in",
    "Topic",
    "Book",
    "Scan",
    "Claim",
)
PAGE_TERMS = ("nas:WebPage", "WebPage", "Harvest page", "example.org/p")


class World:
    """The prepared installation: tokens, entity ids and aliases, what is hidden from whom."""

    def __init__(self, server: ServerApp, installation: dict[str, Any]) -> None:
        self.server = server
        self.root: Path = installation["root"]
        self.tokens: dict[str, str] = installation["tokens"]
        self.ids: dict[str, str] = installation["ids"]
        self.aliases: dict[str, str] = installation["aliases"]
        self.first_tx: int = installation["first_tx"]

    def ask(self, who: str, path: str, **args: Any) -> Reply:
        body = {"args": {**GRAPH, **args}}
        return call(self.server, "POST", f"/v1/commands/{path}", token=self.tokens[who], body=body)

    def ok(self, who: str, path: str, **args: Any) -> Reply:
        reply = self.ask(who, path, **args)
        assert reply.status == 200, (path, args, reply.json)
        return reply

    def data(self, who: str, path: str, **args: Any) -> Any:
        return self.ok(who, path, **args).json["data"]

    def names_of_topics_and_book(self) -> list[str]:
        """Everything that names one of the hidden entities: ids, aliases, labels, values."""
        found = [self.ids[local] for local in ("t1", "t2", "b")]
        found += [self.aliases[local] for local in ("t1", "t2") if self.aliases.get(local)]
        found += [
            "Alpha topic",
            "Beta topic",
            "ATP",
            "Alfa topik",
            "Zebra handbook",
            "Quagga Press",
        ]
        return found

    def traces(self, text: str, *, pages: bool = False) -> list[str]:
        """The hidden names that appear in ``text``."""
        names = [*self.names_of_topics_and_book(), *HIDDEN_TERMS]
        if pages:
            names += [self.ids["w"], *PAGE_TERMS]
        return [name for name in names if name in text]


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    first = write_documents(root, "example_graph", build())[0]
    # the functions, actions and skill are published under the built-in policy
    for kind, files in (
        (
            "functions",
            {
                "notes_by_kind": NOTES_BY_KIND,
                "note_sources": NOTE_SOURCES,
                "notes_on_topic": NOTES_ON_TOPIC,
            },
        ),
        ("actions", {"rename_note": RENAME_NOTE, "note_on_topic": NOTE_ON_TOPIC}),
    ):
        folder = tmp_path / kind
        folder.mkdir()
        for name, text in files.items():
            (folder / f"{name}.yaml").write_text(text)
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    skill = tmp_path / "skill" / "graph-example-graph"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL)
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(skill))
    assert published.code == 0, published.envelope
    # entity ids and aliases, read before any limit exists
    env = {"GRAPH_CLI_GRAPH": "example_graph"}
    found = app(run_cli, "find", "--limit", "100", env=env)
    titles = {
        "First note": "n",
        "Second note": "m",
        "Harvest page": "w",
        "Zebra handbook": "b",
        "Alpha topic": "t1",
        "Beta topic": "t2",
    }
    ids: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for item in found.data["items"]:
        attrs = item["attrs"]
        local = titles.get(attrs.get("title") or attrs.get("label"))
        if local is not None:
            ids[local] = item["id"]
            aliases[local] = item["alias"]
    assert sorted(ids) == sorted(titles.values())
    applied = admin(
        run_cli,
        *(
            "policy",
            "apply",
            str(write_policy(tmp_path / "policies", "installation.yaml", INSTALLATION)),
        ),
        *("--reason", "type read limits"),
    )
    assert applied.code == 0, applied.envelope
    # the reporting grant creates notes but reads no topics: policy check warns about it
    assert [(w["code"], w["details"]["relation"]) for w in applied.warnings] == [
        ("admin.policy_unusable_write", "nas:about")
    ], applied.envelope
    tokens = {
        "a": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reporting"),
        "b": new_token(
            *(run_cli, "app_b", "--project", "example_project"),
            *("--role", "reporting", "--role", "auditor"),
        ),
    }
    return {
        "root": root,
        "tokens": tokens,
        "ids": ids,
        "aliases": aliases,
        "first_tx": first,
        "graph_policy": write_policy(tmp_path / "policies", "graph.yaml", GRAPH_POLICY),
    }


@pytest.fixture
def world(installation: dict[str, Any], isolated_env: dict[str, str], tmp_path: Path) -> World:
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)
    return World(server, installation)


def same_but_for(hidden: Reply, missing: Reply, swap: tuple[str, str]) -> None:
    """Two replies are the same but for the name asked and the suggestions that follow it."""
    assert hidden.status == missing.status
    first, second = hidden.json, missing.json
    for body in (first, second):
        if isinstance(body.get("error"), dict):
            body["error"].get("details", {}).pop("did_you_mean", None)
    assert json.dumps(first).replace(*swap) == json.dumps(second)


def skill_text(world: World, who: str) -> tuple[str, dict[str, Any]]:
    reply = call(
        world.server,
        "POST",
        "/v1/graphs/example_graph/skill",
        token=world.tokens[who],
        body={},
    )
    assert reply.status == 200, reply.json
    skill = reply.json["data"]["skill"]
    text = b"".join(base64.b64decode(c) for c in skill["files"].values()).decode()
    return text, skill["source"]


def reads_of(world: World, who: str) -> dict[str, Any]:
    reply = call(
        world.server,
        "POST",
        "/v1/commands/whoami",
        token=world.tokens[who],
        body={"args": {}},
    )
    assert reply.status == 200, reply.json
    seen = reply.json["data"]
    (graph,) = seen["permissions"]["graphs"]
    assert isinstance(graph, dict)
    return graph


def reached(world: World, who: str) -> list[str]:
    """The concrete types the token reaches: ``describe type`` and ``find --type`` agree."""
    found = []
    for type_curie in CONCRETE:
        described = world.ask(who, "describe/type", type=type_curie)
        listed = world.ask(who, "find", **{"--type": [type_curie]})
        assert described.status == listed.status, (type_curie, described.json, listed.json)
        if described.status == 200:
            found.append(type_curie)
        else:
            assert described.error["code"] == listed.error["code"] == "term.unknown"
    return found


# The asked name comes back in the answer to these (the caller wrote it); the rest must not.
ECHOED = {
    "lookup_hidden_label": "Alpha topic",
    "lookup_alt_label": "ATP",
    "lookup_hidden_book": "Zebra handbook",
}


def leaks(world: World, name: str, reply: Reply, *, pages: bool = False) -> list[str]:
    """The hidden names in an answer, not counting the name the caller asked for."""
    text = reply.body.decode().replace(ECHOED.get(name, "\0"), "")
    return world.traces(text, pages=pages)


def events_of(root: Path, principal: str) -> list[dict[str, Any]]:
    """The audit events that one token's requests wrote."""
    found: list[dict[str, Any]] = []
    for segment in sorted(p for p in root.rglob("*.jsonl") if "audit" in p.parts):
        for line in segment.read_text().splitlines():
            event = json.loads(line)
            if event["actor"]["principal"]["id"] == principal:
                found.append(event)
    return found


def rows(reply: Reply) -> list[Any]:
    return list(reply.json["data"]["items"])


def read_everything(world: World, who: str) -> dict[str, Reply]:
    """Every read command once, about entities the token may see, and the schema in full."""
    n, m, w = (world.ids[local] for local in ("n", "m", "w"))
    alias = world.aliases["n"]
    tx = f"tx:{world.first_tx}"
    cypher = "MATCH (n:Note)-[r]->(x) RETURN n.title AS note, type(r) AS rel, x.title AS target"
    asked: dict[str, tuple[str, dict[str, Any]]] = {
        "get": ("get", {"ref": n}),
        "get_alias": ("get", {"ref": alias}),
        "get_full": ("get", {"ref": n, "--full": True}),
        "get_rev": ("get", {"ref": n, "--rev": 1}),
        "get_affordances": ("get", {"ref": n, "--affordances": True}),
        "get_page": ("get", {"ref": w}),
        "get_as_of": ("get", {"ref": n, "--as-of": tx}),
        "find": ("find", {"--limit": 100}),
        "find_include_inactive": ("find", {"--limit": 100, "--include-inactive": True}),
        "find_type": ("find", {"--type": ["nas:Note"]}),
        "find_abstract": ("find", {"--type": ["nas:Source"]}),
        "find_rel": ("find", {"--type": ["nas:Note"], "--rel": ["nas:cites"]}),
        "find_rel_to": ("find", {"--rel": [f"nas:cites={w}"]}),
        "find_in_rel": ("find", {"--in-rel": ["nas:cites"]}),
        "find_as_of": ("find", {"--limit": 100, "--as-of": tx}),
        "lookup": ("lookup", {"text": ["First note"]}),
        "lookup_hidden_label": ("lookup", {"text": ["Alpha topic"]}),
        "lookup_alt_label": ("lookup", {"text": ["ATP"]}),
        "lookup_hidden_book": ("lookup", {"text": ["Zebra handbook"]}),
        "lookup_for": ("lookup", {"text": ["Harvest"], "--for": "nas:cites"}),
        "search": ("search", {"text": "note"}),
        "search_hidden": ("search", {"text": "alpha"}),
        "search_book": ("search", {"text": "zebra"}),
        "neighbors": ("neighbors", {"ref": n}),
        "neighbors_deep": ("neighbors", {"ref": n, "--depth": 3, "--direction": "both"}),
        "neighbors_full": ("neighbors", {"ref": m, "--depth": 2, "--full": True}),
        "history": ("history", {"ref": n}),
        "history_m": ("history", {"ref": m}),
        "changes": ("changes", {"--since": "tx:0", "--limit": 500}),
        "changes_page": ("changes", {"--since": "tx:0", "--limit": 2}),
        "changes_from": ("changes", {"--since": f"tx:{world.first_tx}"}),
        "query_count": ("query", {"text": "MATCH (x) RETURN count(x) AS c"}),
        "query_edges": ("query", {"text": cypher}),
        "query_nodes": ("query", {"text": "MATCH (x:Note) RETURN x"}),
        "query_paths": ("query", {"text": "MATCH (a:Note)-[*1..3]-(b) RETURN count(b) AS c"}),
        "query_explain": ("query", {"text": cypher, "--explain": True}),
        "query_as_of": ("query", {"text": "MATCH (x) RETURN count(x) AS c", "--as-of": tx}),
        "run": ("run", {"name": "notes_by_kind", "--param": ["kind=summary"]}),
        "run_sources": ("run", {"name": "note_sources"}),
        "describe_types": ("describe/types", {}),
        "describe_type": ("describe/type", {"type": "nas:Note"}),
        "describe_fields": ("describe/fields", {"type": "nas:Note"}),
        "describe_lifecycle": ("describe/lifecycle", {"type": "nas:Note"}),
        "describe_relations": ("describe/relations", {}),
        "describe_modules": ("describe/modules", {}),
        "describe_module": ("describe/module", {"module": "notes_and_sources"}),
        "describe_schema": ("describe/schema", {}),
        "describe_schema_compact": ("describe/schema", {"--compact": True}),
        "describe_search": ("describe/search", {"text": "note"}),
        "describe_search_hidden": ("describe/search", {"text": "topic"}),
        "describe_functions": ("describe/functions", {}),
        "describe_function": ("describe/function", {"name": "notes_by_kind"}),
        "describe_actions": ("describe/actions", {}),
        "describe_action": ("describe/action", {"name": "rename_note"}),
    }
    return {name: world.ask(who, path, **args) for name, (path, args) in asked.items()}


@pytest.mark.doc_claim("security_model.read_limits", "security_model.type_limits_complete")
def test_type_read_limits_end_to_end(
    run_cli: CliRunner, world: World, installation: dict[str, Any]
) -> None:
    ids, aliases = world.ids, world.aliases
    n, m, w, t1, b = (ids[local] for local in ("n", "m", "w", "t1", "b"))

    # 1. A reads every command without a trace of a hidden type, entity or relation
    seen_by_a = read_everything(world, "a")
    for name, reply in seen_by_a.items():
        assert reply.status == 200, (name, reply.json)
        assert leaks(world, name, reply) == [], name
    text, source = skill_text(world, "a")
    assert world.traces(text) == []
    assert source["visible"].startswith("sha256:")
    # what is left: the visible entities with their totals, and the edges to visible ones only
    found = seen_by_a["find"].json
    assert sorted(i["type"] for i in found["data"]["items"]) == [
        "nas:Note",
        "nas:Note",
        "nas:WebPage",
    ]
    assert found["page"]["total"] == 3
    note = seen_by_a["get"].json["data"]["entity"]
    assert [(e["rel"], e["target"]) for e in note["edges"]] == [("nas:cites", w)]
    assert seen_by_a["get_alias"].json["data"] == seen_by_a["get"].json["data"]
    assert seen_by_a["get_rev"].json["data"]["entity"]["edges"] != []
    assert seen_by_a["get_page"].json["data"]["entity"]["type"] == "nas:WebPage"
    assert world.data("a", "get", ref=m)["entity"]["edges"] == []
    assert rows(seen_by_a["query_count"]) == [{"c": 3}]
    assert rows(seen_by_a["query_edges"]) == [
        {"note": "First note", "rel": "cites", "target": "Harvest page"}
    ]
    assert rows(seen_by_a["run_sources"]) == [{"note": "First note", "source": "Harvest page"}]
    assert rows(seen_by_a["lookup_hidden_label"]) == rows(seen_by_a["lookup_hidden_book"])
    for name in ("lookup_hidden_label", "lookup_alt_label", "lookup_hidden_book"):
        assert seen_by_a[name].json["page"]["total"] == 0, name
    assert seen_by_a["lookup"].json["page"]["total"] == 1
    assert seen_by_a["search_hidden"].json["page"]["total"] == 0
    assert seen_by_a["search_book"].json["page"]["total"] == 0
    # the second revision of the first note only added a hidden edge: it shows no change
    nothing = {"attrs": [], "edges": {"added": [], "removed": [], "changed": []}, "record": []}
    history = seen_by_a["history"].json["data"]["items"]
    assert [h["changed"] for h in history[:2]] == [nothing, nothing]
    # the feed lists the same revisions: the visible entities of the first tx, then the two
    # revisions that only added hidden edges, with nothing changed and no hidden target
    feed = seen_by_a["changes"].json["data"]
    assert (feed["head"], feed["next"], feed["more"]) == (3, "tx:3", False)
    assert feed["view"] is not None  # A reads under a limit: its consumers rebuild when it moves
    assert [e["tx_seq"] for e in feed["items"]] == [1, 2, 3]
    assert sorted(i["type"] for i in feed["items"][0]["entities"]) == [
        "nas:Note",
        "nas:Note",
        "nas:WebPage",
    ]
    assert [i["changed"] for e in feed["items"][1:] for i in e["entities"]] == [nothing, nothing]
    assert [e["reason"] for e in feed["items"][1:]] == ["one more subject", "cite the book too"]
    first_note = next(i for i in feed["items"][0]["entities"] if i["id"] == n)
    assert first_note["targets"] == [w]
    page = seen_by_a["changes_page"].json["data"]  # two items of a tx of three: it is split
    assert (page["more"], page["next"].startswith("tx:1/")) == (True, True)
    assert page["items"][0].get("partial") is True and len(page["items"][0]["entities"]) == 2
    after_first = seen_by_a["changes_from"].json["data"]  # what follows the first tx
    assert after_first["items"] == feed["items"][1:]
    assert after_first["since_tx_id"] == feed["items"][0]["tx_id"]
    types = [t["curie"] for t in seen_by_a["describe_types"].json["data"]["types"]]
    assert types == ["nas:Note", "nas:Source", "nas:WebPage"]
    assert [a["name"] for a in seen_by_a["describe_actions"].json["data"]["actions"]] == [
        "rename_note"
    ]
    assert [f["name"] for f in seen_by_a["describe_functions"].json["data"]["functions"]] == [
        "note_sources",
        "notes_by_kind",
    ]

    # 2. B holds both roles and sees everything
    seen_by_b = read_everything(world, "b")
    for name, reply in seen_by_b.items():
        assert reply.status == 200, (name, reply.json)
    assert seen_by_b["find"].json["page"]["total"] == 6
    whole = seen_by_b["get"].json["data"]["entity"]
    assert sorted(e["rel"] for e in whole["edges"]) == [
        "nas:about",
        "nas:about",
        "nas:cites",
        "nas:cites",
    ]
    assert rows(seen_by_b["query_count"]) == [{"c": 6}]
    assert sorted(t["curie"] for t in seen_by_b["describe_types"].json["data"]["types"]) == sorted(
        [*CONCRETE, "files:Metadata", "files:MetadataPart", "nas:Source"]
    )
    assert world.traces(seen_by_b["describe_schema"].body.decode()) != []
    assert rows(seen_by_b["lookup_hidden_label"]) != []
    assert seen_by_b["search_book"].json["page"]["total"] == 1
    assert "nas:about" in seen_by_b["history"].body.decode()
    feed_b = seen_by_b["changes"].json["data"]
    assert feed_b["view"] is None and [e["tx_seq"] for e in feed_b["items"]] == [1, 2, 3]
    assert len(feed_b["items"][0]["entities"]) == 6  # the topics and the book too
    assert feed_b["items"][1]["entities"][0]["changed"]["edges"]["added"] == ["nas:about"]
    assert [i["id"] for i in feed_b["items"][1]["entities"][0:1]] == [n]
    assert feed_b["items"][1]["entities"][0]["targets"] == [ids["t2"]]
    assert "nas:Topic" in seen_by_b["changes"].body.decode()
    text_b, source_b = skill_text(world, "b")
    assert "nas:Topic" in text_b and "visible" not in source_b  # no limit, no digest
    assert [f["name"] for f in seen_by_b["describe_actions"].json["data"]["actions"]] == [
        "note_on_topic",
        "rename_note",
    ]
    assert len(rows(seen_by_b["run_sources"])) == 3

    # 3. what A asks about a hidden entity, type, function or action is answered as about one
    #    that does not exist
    for local in ("t1", "b"):
        entity, alias = ids[local], aliases.get(local)
        for path, extra in (
            ("get", {}),
            ("get", {"--rev": 1}),
            ("history", {}),
            ("neighbors", {}),
        ):
            hidden = world.ask("a", path, ref=entity, **extra)
            missing = world.ask("a", path, ref=NOT_THERE, **extra)
            assert hidden.status == 404 and hidden.error["code"] == "entity.not_found", path
            same_but_for(hidden, missing, (entity, NOT_THERE))
        suffix = entity.replace("-", "")[-10:]
        hidden = world.ask("a", "get", ref=suffix)
        same_but_for(hidden, world.ask("a", "get", ref="0123456789"), (suffix, "0123456789"))
        if alias:
            hidden = world.ask("a", "get", ref=alias)
            same_but_for(
                hidden, world.ask("a", "get", ref=alias[:-1] + "9"), (alias, alias[:-1] + "9")
            )
    ref_to_hidden = world.ask("a", "find", **{"--rel": [f"nas:cites={b}"]})
    ref_to_nothing = world.ask("a", "find", **{"--rel": [f"nas:cites={NOT_THERE}"]})
    same_but_for(ref_to_hidden, ref_to_nothing, (b, NOT_THERE))
    # a hidden type, relation, function or action is an unknown term, function or action
    for path, make, hidden_name, missing_name in (
        ("describe/type", lambda x: {"type": x}, "nas:Topic", "nas:Gone"),
        ("describe/fields", lambda x: {"type": x}, "nas:Book", "nas:Gone"),
        ("describe/lifecycle", lambda x: {"type": x}, "nas:Scan", "nas:Gone"),
        ("find", lambda x: {"--type": [x]}, "nas:Topic", "nas:Gone"),
        ("find", lambda x: {"--rel": [x]}, "nas:about", "nas:gone"),
        ("query", lambda x: {"text": f"MATCH (x:{x}) RETURN x"}, "Topic", "Gone"),
        ("query", lambda x: {"text": f"MATCH (x)-[:{x}]->() RETURN x"}, "about", "gone"),
        ("describe/function", lambda x: {"name": x}, "notes_on_topic", "gone"),
        ("run", lambda x: {"name": x}, "notes_on_topic", "gone"),
        ("describe/action", lambda x: {"name": x}, "note_on_topic", "gone"),
        ("do", lambda x: {"name": x}, "note_on_topic", "gone"),
    ):
        hidden = world.ask("a", path, **make(hidden_name))
        missing = world.ask("a", path, **make(missing_name))
        assert hidden.status in (400, 404), (path, hidden.json)
        same_but_for(hidden, missing, (hidden_name, missing_name))
    # the same questions are answered for B
    assert world.ask("b", "describe/type", type="nas:Topic").status == 200
    assert world.ask("b", "get", ref=t1).status == 200
    assert (
        world.ask("b", "run", name="notes_on_topic", **{"--param": [f"topic={t1}"]}).status == 200
    )

    # 4. whoami says what A and B observed
    graph_a = reads_of(world, "a")
    assert graph_a["reads"] == ["nas:Note", "nas:WebPage"] == reached(world, "a")
    assert graph_a["writes"] == {"nas:Note": {"create": True, "update": True, "link": True}}
    assert world.traces(json.dumps(graph_a)) == []
    graph_b = reads_of(world, "b")
    assert "reads" not in graph_b and reached(world, "b") == sorted(CONCRETE)
    explained = admin(
        run_cli,
        *("policy", "explain", "--graph", "example_graph", "--project", "example_project"),
        *("--role", "reporting"),
    )
    assert explained.code == 0, explained.envelope
    assert explained.data["reads"] == ["nas:Note", "nas:WebPage"]

    # 5. A's writes: a hidden type is an unknown type, an update keeps what A cannot see
    hidden_create = world.ask("a", "create", type="nas:Topic", **{"--set": ["label=Gamma topic"]})
    unknown_create = world.ask("a", "create", type="nas:Gone", **{"--set": ["label=Gamma topic"]})
    assert hidden_create.status == unknown_create.status == 403
    assert hidden_create.error["code"] == "access.denied"
    assert hidden_create.error["details"]["rule"] == "default.deny"
    assert json.dumps(hidden_create.json).replace("nas:Topic", "nas:Gone") == json.dumps(
        unknown_create.json
    )
    renamed = world.ok("a", "update", ref=n, **{"--set": ["title=First note, edited"]})
    assert world.traces(renamed.body.decode()) == []
    whole = world.data("b", "get", ref=n)["entity"]
    assert whole["attrs"]["title"] == "First note, edited"
    assert sorted(e["rel"] for e in whole["edges"]) == [
        "nas:about",
        "nas:about",
        "nas:cites",
        "nas:cites",
    ]
    # a link from A to a hidden entity is a link to a missing one
    link_hidden = world.ask("a", "link", ref=n, rel="nas:cites", target=b)
    link_missing = world.ask("a", "link", ref=n, rel="nas:cites", target=NOT_THERE)
    assert link_hidden.status == link_missing.status
    same_but_for(link_hidden, link_missing, (b, NOT_THERE))
    # an action that names a hidden type is not found; one that does not runs for A
    done = world.ok(
        "a", "do", name="rename_note", **{"--param": [f"note={m}", "title=Second, edited"]}
    )
    assert world.traces(done.body.decode()) == []
    created = world.ok(
        "b", "do", name="note_on_topic", **{"--param": [f"topic={t1}", "title=Third"]}
    )
    third = created.json["data"]["entities"][0]["id"]
    assert world.data("a", "get", ref=third)["entity"]["edges"] == []
    assert world.ask("a", "find", **{"--limit": 100}).json["page"]["total"] == 4

    # 6. a graph policy that denies read.get on web pages hides them from both tokens
    applied = admin(
        run_cli,
        *("policy", "apply", str(installation["graph_policy"])),
        *("--graph", "example_graph", "--reason", "hide the pages"),
    )
    assert applied.code == 0, applied.envelope
    for who in ("a", "b"):
        gone = world.ask(who, "get", ref=w)
        assert gone.error["code"] == "entity.not_found"
        same_but_for(gone, world.ask(who, "get", ref=NOT_THERE), (w, NOT_THERE))
    limited_a = read_everything(world, "a")
    unknown = {}
    for name, reply in limited_a.items():
        if reply.status != 200:
            unknown[name] = reply.error["code"]
        if name != "get_page":  # the id it asks for comes back in its error
            assert leaks(world, name, reply, pages=True) == [], name
    # what A cannot name any more: the page, the abstract source type and the relation to it
    assert unknown == {
        "get_page": "entity.not_found",
        "find_abstract": "term.unknown",
        "find_rel": "term.unknown",
        "find_rel_to": "term.unknown",
        "find_in_rel": "term.unknown",
        "lookup_for": "term.unknown",
        "run_sources": "function.not_found",
    }
    assert reads_of(world, "a")["reads"] == ["nas:Note"] == reached(world, "a")
    expected_b = sorted(t for t in CONCRETE if t != "nas:WebPage")
    assert reads_of(world, "b")["reads"] == expected_b == reached(world, "b")
    after_b = read_everything(world, "b")
    assert after_b["find"].json["page"]["total"] == 6  # topics, book and notes, no page
    assert [t for t in (w, *PAGE_TERMS) if t in after_b["find"].body.decode()] == []
    for who, reply in (("a", limited_a["changes"]), ("b", after_b["changes"])):
        items = [i for e in reply.json["data"]["items"] for i in e["entities"]]
        assert w not in [i["id"] for i in items] and "nas:WebPage" not in str(items), who
        assert [t for t in (w, *PAGE_TERMS) if t in reply.body.decode()] == [], who
    assert limited_a["changes"].json["data"]["view"] != seen_by_a["changes"].json["data"]["view"]
    # the function over sources needs a source type A can see: it is hidden, B's runs on the book
    assert [f["name"] for f in limited_a["describe_functions"].json["data"]["functions"]] == [
        "notes_by_kind"
    ]
    assert world.ask("a", "run", name="note_sources").error["code"] == "function.not_found"
    assert sorted(r["source"] for r in rows(world.ok("b", "run", name="note_sources"))) == [
        "Zebra handbook",
        "Zebra handbook",
    ]
    text_after, source_after = skill_text(world, "a")
    assert world.traces(text_after, pages=True) == []
    assert source_after["visible"] != source["visible"]  # a policy change makes the claim stale
    assert skill_text(world, "b")[1]["visible"].startswith("sha256:")

    # 7. the audit trail verifies and records the decision that hides a type
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0 and verified.data["findings"] == []
    denied = [e for e in events_of(world.root, "token:app_a") if e["result"]["status"] == "denied"]
    assert denied, "the refused create of a hidden type is audited"
    hiding = {
        (d["action"], d["effect"])
        for e in denied
        for d in e["authz"]["decisions"]
        if d["resource"].endswith("/type:nas:Topic")
    }
    assert ("read.get", "deny") in hiding
