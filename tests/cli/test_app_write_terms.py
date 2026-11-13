"""S: writes under a write allow list (write allow lists plan W4, W7), through ``graph apply``
and ``graph file add`` against a toy graph.

The installation policy grants the local caller a read level plus ``writes`` (locally a policy is
a guardrail: the app caller matches a grant without a principal). Every op row of W4 is
allowed, then refused with the fine resource in ``details.resource``; a dry-run refuses the
same way; an unlink by edge id takes its relation from the stored edge.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app, create_graph
from support.policies import write_policy

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}

POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      nas:Note:
        create: [title, body, kind]
        update: [body, kind]
        link: [nas:about]
        unlink: [nas:about]
        transition: [status]
      nas:Topic: {create: true, update: [description]}
      nas:Scan: {create: [title, content, media_type, file_name]}
no_project: {"*": write}
"""
VERBS = POLICY.replace(
    "        transition: [status]\n",
    "        transition: [status]\n        supersede: true\n        retract: true\n"
    "        alias: true\n",
)
FULL = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
GRAPH_DENY = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_status_moves, action: [write.transition], type: [nas:Note], attribute: [status]}
  - {id: no_title, action: [write.update], type: [nas:Note], attribute: [title]}
"""
SETUP = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t1", "attrs": {"label": "One"}},
        {"op": "create", "type": "nas:Topic", "local": "t2", "attrs": {"label": "Two"}},
        {"op": "create", "type": "nas:Topic", "local": "t3", "attrs": {"label": "Three"}},
        {
            "op": "create",
            "type": "nas:WebPage",
            "local": "w",
            "attrs": {"title": "Page", "url": "https://example.org/p"},
        },
        {
            "op": "create",
            "type": "nas:Note",
            "local": "n",
            "attrs": {"title": "A", "body": "b", "kind": "summary"},
            "edges": [
                {"rel": "nas:about", "target": "@t1"},
                {"rel": "nas:about", "target": "@t2"},
                {"rel": "nas:cites", "target": "@w"},
            ],
        },
    ]
}


@dataclass
class World:
    root: Path
    graph_id: str
    ids: dict[str, str]
    edges: dict[str, str]  # rel + "_" + target local -> edge id of the note n
    work: Path

    def resource(self, below: str) -> str:
        return f"graph:{self.graph_id}/{below}"


def install(run_cli: CliRunner, world: World, text: str, *args: str) -> None:
    path = write_policy(
        world.work, f"policy_{hashlib.sha256(text.encode()).hexdigest()[:12]}.yaml", text
    )
    applied = admin(run_cli, "policy", "apply", str(path), *args, "--reason", "test policy")
    assert applied.code == 0, applied.envelope


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    root = init(run_cli, isolated_env)
    graph_id = create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    # The data is written before any policy is installed: the built-in one lets the app write
    done = app(run_cli, "apply", "-", env=ENV, input=json.dumps(SETUP))
    assert done.code == 0, done.envelope
    by_local = {e["local"]: e["id"] for e in done.data["results"][0]["result"]["entities"]}
    note = app(run_cli, "get", by_local["n"], "--full", env=ENV).data["entity"]
    targets = {by_local[k]: k for k in ("t1", "t2", "w")}
    edges = {f"{e['rel']}_{targets[e['target']]}": e["edge_id"] for e in note["edges"]}
    work = tmp_path / "policies"
    found = World(root, graph_id, by_local, edges, work)
    install(run_cli, found, POLICY)
    return found


def write(run_cli: CliRunner, *ops: dict[str, Any], dry_run: bool = False, **frame: Any) -> Outcome:
    args = ["apply", "-", *(["--dry-run"] if dry_run else [])]
    return app(run_cli, *args, env=ENV, input=json.dumps({"ops": list(ops), **frame}))


def refused(outcome: Outcome, action: str, resource: str) -> None:
    assert outcome.code != 0 and outcome.error["code"] == "access.denied", outcome.envelope
    details = outcome.error["details"]
    assert (details["action"], details["resource"], details["rule"]) == (
        action,
        resource,
        "default.deny",
    )


def seq_of(run_cli: CliRunner) -> int | None:
    seq = app(run_cli, "find", "--type", "nas:Topic", "--limit", "1", env=ENV).envelope["graph"]
    return seq["seq"]  # type: ignore[no-any-return]


# ---- what the list allows ----------------------------------------------------------------


def test_every_listed_write_goes_through(run_cli: CliRunner, world: World) -> None:
    ids = world.ids
    steps: list[tuple[str, list[dict[str, Any]]]] = [
        (
            "create with listed attributes and an inline edge on a linkable relation",
            [
                {
                    "op": "create",
                    "type": "nas:Note",
                    "attrs": {"title": "B", "body": "x", "kind": "question"},
                    "edges": [{"rel": "nas:about", "target": ids["t3"]}],
                }
            ],
        ),
        (
            "create: true names every attribute of the type",
            [{"op": "create", "type": "nas:Topic", "attrs": {"label": "Four", "description": "d"}}],
        ),
        (
            "update of listed attributes, as values and as text",
            [
                {"op": "update", "id": ids["n"], "set": {"kind": "question"}},
                {"op": "update", "id": ids["n"], "set_text": {"body": "new"}},
                {"op": "update", "id": ids["t3"], "set": {"description": "d"}},
            ],
        ),
        (
            "link of a listed relation",
            [{"op": "link", "id": ids["n"], "rel": "nas:about", "target": ids["t3"]}],
        ),
        (
            "unlink by relation and target",
            [{"op": "unlink", "id": ids["n"], "rel": "nas:about", "target": ids["t3"]}],
        ),
        (
            "unlink by edge id: the relation comes from the stored edge",
            [{"op": "unlink", "id": ids["n"], "edge_id": world.edges["nas:about_t1"]}],
        ),
        (
            "transition without attr moves the lifecycle the type has",
            [{"op": "transition", "id": ids["n"], "to": "published"}],
        ),
        (
            "transition with the listed attr",
            [{"op": "transition", "id": ids["n"], "to": "draft", "attr": "status"}],
        ),
    ]
    for step, ops in steps:
        outcome = write(run_cli, *ops)
        assert outcome.code == 0, (step, outcome.envelope)
    assert admin(run_cli, "audit", "verify").code == 0


def test_the_verbs_without_terms_are_granted_per_type(
    run_cli: CliRunner, world: World, tmp_path: Path
) -> None:
    """supersede, retract and alias carry no term: the type grant decides them, and the
    decisions of the write name the type only."""
    refused(
        write(run_cli, {"op": "set_alias", "id": world.ids["n"], "alias": "NOTE-0099"}),
        "write.alias",
        world.resource("type:nas:Note"),
    )
    install(run_cli, world, VERBS)
    ok = write(run_cli, {"op": "set_alias", "id": world.ids["n"], "alias": "NOTE-0099"})
    assert ok.code == 0, ok.envelope
    created = write(
        run_cli,
        {
            "op": "create",
            "type": "nas:Note",
            "local": "m",
            "attrs": {"title": "B", "body": "x", "kind": "question"},
            "edges": [{"rel": "nas:about", "target": world.ids["t3"]}],
        },
    )
    assert created.code == 0, created.envelope
    other = created.data["results"][0]["result"]["entities"][0]["id"]
    retracted = write(run_cli, {"op": "retract", "id": other}, reason="test")
    assert retracted.code == 0, retracted.envelope
    decisions = _events(world.root, world.graph_id)[-1]["authz"]["decisions"]
    assert {d["resource"] for d in decisions} == {world.resource(NOTE)}  # nothing below the type


# ---- what it refuses ---------------------------------------------------------------------

NOTE = "type:nas:Note"
TOPIC = "type:nas:Topic"


Case = tuple[str, Callable[[World], list[dict[str, Any]]], str, str]
NEW_NOTE = {"title": "B", "body": "x", "kind": "question"}


def unlisted(world: World, op: str, **keys: Any) -> list[dict[str, Any]]:
    subject = world.ids["t1"]
    return [{"op": op, "id": subject, **keys}]


# (name, ops, action, resource below the graph) of a write the list refuses
CASES: list[Case] = [
    (
        "create_unlisted_attribute",
        lambda w: [{"op": "create", "type": "nas:Note", "attrs": {**NEW_NOTE, "status": "draft"}}],
        "write.create",
        f"{NOTE}/attr:status",
    ),
    (
        "create_unlisted_text_attribute",
        lambda w: [
            {
                "op": "create",
                "type": "nas:Note",
                "attrs": NEW_NOTE,
                "attrs_text": {"status": "draft"},
            }
        ],
        "write.create",
        f"{NOTE}/attr:status",
    ),
    (
        "create_with_inline_edge_without_the_link_right",
        lambda w: [
            {
                "op": "create",
                "type": "nas:Note",
                "attrs": NEW_NOTE,
                "edges": [
                    {"rel": "nas:about", "target": w.ids["t3"]},
                    {"rel": "nas:cites", "target": w.ids["w"]},
                ],
            }
        ],
        "write.create",
        f"{NOTE}/rel:nas:cites",
    ),
    (
        "update_set",
        lambda w: [{"op": "update", "id": w.ids["n"], "set": {"title": "X"}}],
        "write.update",
        f"{NOTE}/attr:title",
    ),
    (
        "update_set_text",
        lambda w: [{"op": "update", "id": w.ids["n"], "set_text": {"title": "X"}}],
        "write.update",
        f"{NOTE}/attr:title",
    ),
    (
        "update_unset",
        lambda w: unlisted(w, "update", unset=["synonyms"]),
        "write.update",
        f"{TOPIC}/attr:synonyms",
    ),
    (
        "update_add",
        lambda w: unlisted(w, "update", add={"synonyms": ["a"]}),
        "write.update",
        f"{TOPIC}/attr:synonyms",
    ),
    (
        "update_add_text",
        lambda w: unlisted(w, "update", add_text={"synonyms": ["a"]}),
        "write.update",
        f"{TOPIC}/attr:synonyms",
    ),
    (
        "update_remove",
        lambda w: unlisted(w, "update", remove={"synonyms": ["a"]}),
        "write.update",
        f"{TOPIC}/attr:synonyms",
    ),
    (
        "update_remove_text",
        lambda w: unlisted(w, "update", remove_text={"synonyms": ["a"]}),
        "write.update",
        f"{TOPIC}/attr:synonyms",
    ),
    (
        "update_one_unlisted_among_listed",
        lambda w: [{"op": "update", "id": w.ids["n"], "set": {"body": "ok", "title": "X"}}],
        "write.update",
        f"{NOTE}/attr:title",
    ),
    (
        "link",
        lambda w: [{"op": "link", "id": w.ids["n"], "rel": "nas:cites", "target": w.ids["w"]}],
        "write.link",
        f"{NOTE}/rel:nas:cites",
    ),
    (
        "unlink_by_relation",
        lambda w: [{"op": "unlink", "id": w.ids["n"], "rel": "nas:cites", "target": w.ids["w"]}],
        "write.unlink",
        f"{NOTE}/rel:nas:cites",
    ),
    (
        "unlink_by_edge_id",
        lambda w: [{"op": "unlink", "id": w.ids["n"], "edge_id": w.edges["nas:cites_w"]}],
        "write.unlink",
        f"{NOTE}/rel:nas:cites",
    ),
]


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry_run"])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_an_unlisted_term_is_refused_with_its_resource(
    run_cli: CliRunner, world: World, case: Case, dry_run: bool
) -> None:
    (_, build, action, below) = case
    before = seq_of(run_cli)
    events = len(_events(world.root))
    outcome = write(run_cli, *build(world), dry_run=dry_run)
    refused(outcome, action, world.resource(below))
    assert seq_of(run_cli) == before  # nothing was written
    recorded = _events(world.root)
    if dry_run:
        assert len(recorded) == events  # a dry-run denial is not audited (B3.10)
        return
    assert len(recorded) == events + 1
    decisions = recorded[-1]["authz"]["decisions"]
    denied = [d for d in decisions if d["effect"] == "deny"]
    assert denied[0]["resource"] == world.resource(below)
    # the decision on the type stays next to the decisions on its terms
    assert world.resource(below.split("/")[0]) in {d["resource"] for d in decisions}


def test_a_refusal_carries_every_decision_sorted(run_cli: CliRunner, world: World) -> None:
    """The type and the term decisions of all ops, sorted by action and resource; the first
    denial is the one reported."""
    ids = world.ids
    outcome = write(
        run_cli,
        {"op": "update", "id": ids["n"], "set": {"title": "X", "body": "y"}},
        {"op": "link", "id": ids["n"], "rel": "nas:about", "target": ids["t3"]},
    )
    refused(outcome, "write.update", world.resource(f"{NOTE}/attr:title"))
    event = _events(world.root)[-1]
    assert event["result"]["status"] == "denied"
    pairs = [(d["action"], d["resource"], d["effect"]) for d in event["authz"]["decisions"]]
    assert pairs == sorted(pairs, key=lambda p: (p[0], p[1]))
    assert pairs == [
        ("write.link", world.resource(NOTE), "allow"),
        ("write.link", world.resource(f"{NOTE}/rel:nas:about"), "allow"),
        ("write.update", world.resource(NOTE), "allow"),
        ("write.update", world.resource(f"{NOTE}/attr:body"), "allow"),
        ("write.update", world.resource(f"{NOTE}/attr:title"), "deny"),
    ]
    assert event["target"]["types"] == ["nas:Note"]


def test_a_transition_is_decided_on_the_lifecycle_attribute(
    run_cli: CliRunner, world: World, tmp_path: Path
) -> None:
    """With and without ``attr`` a transition is decided on ``attr:<lifecycle>``; a graph
    deny rule on that attribute refuses both forms, a rule on another attribute neither."""
    install(run_cli, world, GRAPH_DENY, "--graph", "example_graph")
    ids = world.ids
    bare = {"op": "transition", "id": ids["n"], "to": "published"}
    named = {**bare, "attr": "status"}
    for op in (bare, named):
        for dry_run in (False, True):
            outcome = write(run_cli, op, dry_run=dry_run)
            assert outcome.code != 0 and outcome.error["code"] == "access.denied"
            assert outcome.error["details"]["action"] == "write.transition"
            assert outcome.error["details"]["resource"] == world.resource(f"{NOTE}/attr:status")
            assert outcome.error["details"]["rule"] == "deny.no_status_moves"
    # the same graph policy does not stop a type that is not decided on terms
    assert write(run_cli, {"op": "update", "id": ids["n"], "set": {"body": "z"}}).code == 0


def test_a_transition_attr_that_is_no_lifecycle_is_the_unknown_attribute(
    run_cli: CliRunner, world: World
) -> None:
    """``attr: body`` is a term like another: the list does not name it, so the caller is
    refused before validation could say that body is no lifecycle."""
    outcome = write(
        run_cli, {"op": "transition", "id": world.ids["n"], "to": "published", "attr": "body"}
    )
    refused(outcome, "write.transition", world.resource(f"{NOTE}/attr:body"))


# ---- unlink by edge id -------------------------------------------------------------------


def test_an_unlink_by_edge_id_without_a_term_is_left_to_normalize(
    run_cli: CliRunner, world: World
) -> None:
    """On an ``@local`` of the document, for an edge the subject does not have and for an
    unknown edge no relation can be read, so no term is decided; normalize refuses the op as
    it does without a policy (the finding, not ``access.denied``)."""
    ids = world.ids
    note = {"title": "B", "body": "x", "kind": "question"}
    other_edge = world.edges["nas:cites_w"]  # an edge of n, not of the second note
    second = write(
        run_cli,
        {
            "op": "create",
            "type": "nas:Note",
            "attrs": note,
            "edges": [{"rel": "nas:about", "target": ids["t3"]}],
        },
    )
    assert second.code == 0, second.envelope
    other_note = second.data["results"][0]["result"]["entities"][0]["id"]
    attempts = {
        "local": [
            {
                "op": "create",
                "type": "nas:Note",
                "local": "m",
                "attrs": note,
                "edges": [{"rel": "nas:about", "target": ids["t3"]}],
            },
            {"op": "unlink", "id": "@m", "edge_id": other_edge},
        ],
        "foreign_edge": [{"op": "unlink", "id": other_note, "edge_id": other_edge}],
        "unknown_edge": [
            {"op": "unlink", "id": ids["n"], "edge_id": "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"}
        ],
    }
    for name, ops in attempts.items():
        outcome = write(run_cli, *ops)
        assert outcome.error["code"] == "write.invalid", (name, outcome.envelope)
    # an unlink that exists is refused on the stored edge's relation, whatever the caller says
    kept = write(run_cli, {"op": "unlink", "id": ids["n"], "edge_id": other_edge})
    refused(kept, "write.unlink", world.resource(f"{NOTE}/rel:nas:cites"))


# ---- unknown terms and the other types ---------------------------------------------------


def test_an_unknown_attribute_of_a_limited_caller_is_an_access_denial(
    run_cli: CliRunner, world: World
) -> None:
    """Authorization comes first (W4): the list does not name ``nonsense``, so the caller
    hears ``access.denied`` on it, not ``data.structural.unknown_attribute``. A caller who may
    write every attribute of the type gets the finding."""
    ids = world.ids
    op = {"op": "update", "id": ids["n"], "set": {"nonsense": 1}}
    refused(write(run_cli, op), "write.update", world.resource(f"{NOTE}/attr:nonsense"))
    refused(
        write(run_cli, op, dry_run=True), "write.update", world.resource(f"{NOTE}/attr:nonsense")
    )
    install(run_cli, world, FULL)
    install(run_cli, world, GRAPH_DENY, "--graph", "example_graph")  # Note has term rules now
    found = write(run_cli, op)
    assert found.error["code"] == "write.invalid"
    assert [f["code"] for f in found.error["details"]["findings"]] == [
        "data.structural.unknown_attribute"
    ]
    # an unlisted attribute of a type without term rules is no business of the fine decisions
    web = {"op": "update", "id": ids["w"], "set": {"nonsense": 1}}
    assert write(run_cli, web).error["code"] == "write.invalid"


def test_a_type_without_term_rules_is_decided_on_the_type_alone(
    run_cli: CliRunner, world: World
) -> None:
    """Under ``level: write`` plus a term deny on one type, another type's events carry the
    type decision only; the denied term shows in its own type's decisions."""
    install(run_cli, world, FULL)
    install(run_cli, world, GRAPH_DENY, "--graph", "example_graph")
    ids = world.ids
    web = write(run_cli, {"op": "update", "id": ids["w"], "set": {"title": "T"}})
    assert web.code == 0, web.envelope
    decisions = _events(world.root, world.graph_id)[-1]["authz"]["decisions"]
    assert {d["resource"] for d in decisions} == {world.resource("type:nas:WebPage")}
    denied = write(run_cli, {"op": "update", "id": ids["n"], "set": {"title": "T"}})
    assert denied.error["details"]["rule"] == "deny.no_title"
    assert denied.error["details"]["resource"] == world.resource(f"{NOTE}/attr:title")


# ---- graph file add ----------------------------------------------------------------------


def test_file_add_is_decided_on_the_attributes_it_sets(
    run_cli: CliRunner, world: World, tmp_path: Path
) -> None:
    """``graph file add`` creates through the write service: a create list for a stored-file
    type names ``title``, ``content``, ``media_type`` and ``file_name``."""
    document = tmp_path / "scan.txt"
    document.write_text("scanned words\n")
    added = app(run_cli, "file", "add", str(document), "--type", "nas:Scan", env=ENV)
    assert added.code == 0, added.envelope
    other = tmp_path / "other.txt"
    other.write_text("other words\n")
    install(run_cli, world, POLICY.replace("content, media_type, file_name", "content, media_type"))
    refused_add = app(run_cli, "file", "add", str(other), "--type", "nas:Scan", env=ENV)
    refused(
        refused_add,
        "write.create",
        world.resource("type:nas:Scan/attr:file_name"),
    )
    dry = app(run_cli, "file", "add", str(other), "--type", "nas:Scan", "--dry-run", env=ENV)
    refused(dry, "write.create", world.resource("type:nas:Scan/attr:file_name"))


def _events(root: Path, graph: str | None = None) -> list[dict[str, Any]]:
    stream = INSTALLATION_STREAM if graph is None else graph_stream(graph)
    report = verify_stream(FileAuditLog(root).read_lines(stream), stream=stream)
    assert report.findings == ()
    return [dict(e) for e in report.events]
