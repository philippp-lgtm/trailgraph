"""S: ``graph whoami`` and ``graph-admin policy explain`` under a write allow list (write allow
lists plan W3, W10) against a toy graph, checked against real writes.

The installation policy grants the local caller a read level plus ``writes`` (locally a policy is
a guardrail: the app caller matches a grant without a principal). ``whoami`` lists what the
caller may write; every probe write goes through ``graph apply --dry-run``, which authorizes as a
write does, and is refused with ``access.denied`` exactly when the list does not hold it.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import app, create_graph
from support.policies import write_policy

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
WRITES = {
    "nas:Note": {
        "create": ["body", "kind", "title"],
        "update": ["body", "kind"],
        "link": ["nas:about"],
        "unlink": ["nas:about"],
        "transition": True,  # the only lifecycle attribute of a note
    },
    "nas:Topic": {"create": True, "update": ["description"]},
    "nas:Scan": {"create": ["content", "file_name", "media_type", "title"]},
}
# Every verb of a note, a topic update with all attributes, and a whole-type grant
EVERY_VERB = POLICY.replace(
    "        transition: [status]\n",
    "        transition: [status]\n        supersede: true\n        retract: true\n"
    "        alias: true\n",
)
TOPIC_ALL = POLICY.replace(
    "      nas:Topic: {create: true, update: [description]}\n",
    "      nas:Topic: {create: true, update: true, link: true}\n",
)
READ_ONLY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: read}
no_project: {"*": write}
"""
FULL = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
TERM_DENY = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_label, action: [write.update], type: [nas:Topic], attribute: [label]}
  - {id: no_broader, action: [write.link], type: [nas:Topic], relation: [nas:broader]}
"""
NOTE_READ_ONLY = """\
schema: graph_cli.policy/1
id: graph
types:
  "nas:Note": read
"""
OBLIGATIONS = """\
schema: graph_cli.policy/1
id: graph
obligations:
  - {action: [write.create], require_reason: true}
  - {action: ["write.*"], max_batch: 5}
"""
SETUP = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t1", "attrs": {"label": "One"}},
        {"op": "create", "type": "nas:Topic", "local": "t2", "attrs": {"label": "Two"}},
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
            "edges": [{"rel": "nas:about", "target": "@t1"}],
        },
    ]
}
VALUES: dict[str, Any] = {
    "title": "x",
    "body": "b",
    "kind": "summary",
    "status": "draft",
    "label": "L",
    "description": "d",
    "synonyms": ["a"],
    "content": "c",
    "media_type": "text/plain",
    "file_name": "f.txt",
    "scanned_on": "2026-01-01",
    "source_url": "https://example.org/s",
}


@dataclass
class World:
    graph_id: str
    ids: dict[str, str]
    work: Path


def install(run_cli: CliRunner, world: World, text: str, *args: str) -> None:
    path = write_policy(
        world.work, f"policy_{hashlib.sha256(text.encode()).hexdigest()[:12]}.yaml", text
    )
    applied = admin(run_cli, "policy", "apply", str(path), *args, "--reason", "test policy")
    assert applied.code == 0, applied.envelope


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    init(run_cli, isolated_env)
    graph_id = create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    # The data is written before any policy is installed: the built-in one lets the app write
    done = app(run_cli, "apply", "-", env=ENV, input=json.dumps(SETUP))
    assert done.code == 0, done.envelope
    ids = {e["local"]: e["id"] for e in done.data["results"][0]["result"]["entities"]}
    found = World(graph_id, ids, tmp_path / "policies")
    install(run_cli, found, POLICY)
    return found


def whoami(run_cli: CliRunner) -> dict[str, Any]:
    outcome = app(run_cli, "whoami", env=ENV)
    assert outcome.code == 0, outcome.envelope
    (graph,) = [g for g in outcome.data["permissions"]["graphs"] if g["graph"] == "example_graph"]
    assert isinstance(graph, dict)
    return graph


def explain(run_cli: CliRunner, *args: str) -> Outcome:
    return admin(run_cli, "policy", "explain", "--graph", "example_graph", *args)


def probe(world: World, type_curie: str, verb: str, term: str | None) -> dict[str, Any]:
    """One write of ``verb`` on ``type_curie`` that touches ``term`` alone."""
    ids = world.ids
    subject = ids["n"] if type_curie == "nas:Note" else ids["t1"]
    other = ids["w"] if term == "nas:cites" else ids["t2"]
    if verb == "create":
        return {"op": "create", "type": type_curie, "attrs": {str(term): VALUES[str(term)]}}
    if verb == "update":
        return {"op": "update", "id": subject, "set": {str(term): VALUES[str(term)]}}
    if verb in ("link", "unlink"):
        return {"op": verb, "id": subject, "rel": term, "target": other}
    if verb == "transition":
        return {"op": "transition", "id": subject, "to": "published", "attr": term}
    if verb == "alias":
        return {"op": "set_alias", "id": subject, "alias": "NOTE-0099"}
    return {"op": verb, "id": subject}


def permitted(run_cli: CliRunner, op: dict[str, Any]) -> bool:
    """Whether authorization lets the write through; what validation says after is not asked."""
    outcome = app(
        run_cli,
        "apply",
        "-",
        "--dry-run",
        env=ENV,
        input=json.dumps({"ops": [op], "reason": "probe"}),
    )
    if outcome.code == 0:
        return True
    return bool(outcome.error["code"] != "access.denied")


# (type, verb, term) of the writes to try; the terms named in the list and some it leaves out
PROBES: list[tuple[str, str, str | None]] = [
    *(("nas:Note", "create", t) for t in ("title", "body", "kind", "status")),
    *(("nas:Note", "update", t) for t in ("title", "body", "kind")),
    *(("nas:Note", "link", t) for t in ("nas:about", "nas:cites")),
    *(("nas:Note", "unlink", t) for t in ("nas:about", "nas:cites")),
    ("nas:Note", "transition", "status"),
    *(("nas:Note", v, None) for v in ("retract", "alias")),
    *(("nas:Topic", "create", t) for t in ("label", "description", "synonyms")),
    *(("nas:Topic", "update", t) for t in ("label", "description", "synonyms")),
    *(("nas:Topic", v, "nas:broader") for v in ("link", "unlink")),
    *(("nas:Scan", "create", t) for t in ("title", "content", "scanned_on", "source_url")),
]


def listed(writes: dict[str, Any], type_curie: str, verb: str, term: str | None) -> bool:
    right = writes.get(type_curie, {}).get(verb)
    return right is True or (right is not None and term in right)


def check_against_writes(run_cli: CliRunner, world: World, writes: dict[str, Any]) -> None:
    for type_curie, verb, term in PROBES:
        want = (
            listed(writes, type_curie, verb, term) if term else verb in writes.get(type_curie, {})
        )
        got = permitted(run_cli, probe(world, type_curie, verb, term))
        assert got == want, (type_curie, verb, term, writes.get(type_curie))


# ---- what whoami lists is what a write may do ---------------------------------------------


def test_whoami_lists_what_the_caller_may_write(run_cli: CliRunner, world: World) -> None:
    graph = whoami(run_cli)
    assert graph["level"] == "read"
    assert graph["writes"] == WRITES
    # true: every term allowed; a list: the allowed ones, sorted; absent: not granted
    assert graph["writes"]["nas:Note"]["transition"] is True
    assert "supersede" not in graph["writes"]["nas:Note"]
    check_against_writes(run_cli, world, graph["writes"])


def test_the_verbs_without_terms_are_listed_per_type(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world, EVERY_VERB)
    graph = whoami(run_cli)
    note = graph["writes"]["nas:Note"]
    assert (note["supersede"], note["retract"], note["alias"]) == (True, True, True)
    assert "supersede" not in graph["writes"]["nas:Topic"]
    check_against_writes(run_cli, world, graph["writes"])


def test_a_grant_of_all_terms_shows_as_true_where_nothing_is_taken_away(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world, TOPIC_ALL)
    topic = whoami(run_cli)["writes"]["nas:Topic"]
    assert topic == {"create": True, "update": True, "link": True}
    check_against_writes(run_cli, world, whoami(run_cli)["writes"])


def test_a_term_deny_of_the_graph_shows_as_a_list(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world, TOPIC_ALL)
    install(run_cli, world, TERM_DENY, "--graph", "example_graph")
    graph = whoami(run_cli)
    topic = graph["writes"]["nas:Topic"]
    assert topic["update"] == ["description", "misspellings", "synonyms"]  # not label
    # a topic has more relations than nas:broader (imported modules), only that one is denied
    assert topic["link"] and "nas:broader" not in topic["link"]
    assert topic["link"] == sorted(topic["link"])
    assert topic["create"] is True  # the deny names update and link only
    check_against_writes(run_cli, world, graph["writes"])
    # the refusal names the rule of the deny
    refused = app(
        run_cli,
        "apply",
        "-",
        "--dry-run",
        env=ENV,
        input=json.dumps({"ops": [probe(world, "nas:Topic", "update", "label")]}),
    )
    assert refused.error["code"] == "access.denied"
    assert refused.error["details"]["rule"] == "deny.no_label"
    assert refused.error["details"]["resource"].endswith("/type:nas:Topic/attr:label")


def test_a_verb_whose_only_listed_term_is_denied_stays_as_an_empty_list(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world, POLICY.replace("update: [description]", "update: [label]"))
    install(run_cli, world, TERM_DENY, "--graph", "example_graph")
    assert whoami(run_cli)["writes"]["nas:Topic"] == {"create": True, "update": []}


def test_a_read_only_type_limit_takes_the_verbs_of_the_type_away(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world, NOTE_READ_ONLY, "--graph", "example_graph")
    graph = whoami(run_cli)
    assert graph["types"] == {"nas:Note": "read"}
    # the grant's terms still decide on their own, but the type decision is required too
    assert sorted(graph["writes"]) == ["nas:Scan", "nas:Topic"]
    check_against_writes(run_cli, world, graph["writes"])


def test_a_limit_on_every_listed_type_leaves_no_writes_key(
    run_cli: CliRunner, world: World
) -> None:
    text = NOTE_READ_ONLY.replace(
        '  "nas:Note": read\n', '  "nas:Note": read\n  "nas:Topic": read\n  "nas:Scan": read\n'
    )
    install(run_cli, world, text, "--graph", "example_graph")
    assert "writes" not in whoami(run_cli)


def test_a_limited_writer_sees_the_obligations_of_its_writes(
    run_cli: CliRunner, world: World
) -> None:
    before = whoami(run_cli)["obligations"]
    assert before == {}  # nothing is required of a write yet
    install(run_cli, world, OBLIGATIONS, "--graph", "example_graph")
    obligations = whoami(run_cli)["obligations"]
    assert obligations["write.create"] == {"require_reason": True, "max_batch": 5}
    for verb in ("update", "link", "unlink", "transition"):
        assert obligations[f"write.{verb}"] == {"max_batch": 5}
    # a verb the list does not grant is not an allowed write, so it has no obligations
    assert not {"write.supersede", "write.retract", "write.alias"} & set(obligations)
    # what whoami shows is what a write meets
    over = app(
        run_cli,
        "apply",
        "-",
        env=ENV,
        input=json.dumps(
            {"ops": [probe(world, "nas:Topic", "create", "label")] * 6, "reason": "test"}
        ),
    )
    assert over.error["code"] == "access.batch_too_large", over.envelope
    quiet = app(
        run_cli,
        "apply",
        "-",
        "--dry-run",
        env=ENV,
        input=json.dumps({"ops": [probe(world, "nas:Topic", "create", "label")]}),
    )
    assert quiet.error["code"] == "access.reason_required", quiet.envelope


# ---- callers without a write allow list are unchanged ------------------------------------


@pytest.mark.parametrize("text", [FULL, READ_ONLY], ids=["write_level", "read_level_no_writes"])
def test_a_caller_without_a_write_allow_list_gets_no_writes_key(
    run_cli: CliRunner, world: World, text: str
) -> None:
    install(run_cli, world, text)
    graph = whoami(run_cli)
    assert sorted(graph) == ["graph", "level", "obligations", "types", "withheld"]
    explained = explain(run_cli)
    assert explained.code == 0, explained.envelope
    assert "writes" not in explained.data and "type_decision" not in explained.data


def test_the_write_level_keeps_its_obligations_and_no_writes_key(
    run_cli: CliRunner, world: World
) -> None:
    """A grant with writes and one at level write, together: the level decides, as before."""
    both = POLICY.replace(
        "grants:\n",
        "grants:\n  - {entrypoint: app, graphs: [example_graph], level: write}\n",
    )
    install(run_cli, world, both)
    graph = whoami(run_cli)
    assert graph["level"] == "write" and "writes" not in graph


# ---- policy explain ------------------------------------------------------------------------


def test_explain_decides_on_one_attribute_and_shows_the_type_next_to_it(
    run_cli: CliRunner, world: World
) -> None:
    def decide(attribute: str, action: str = "write.update") -> dict[str, Any]:
        out = explain(run_cli, "--action", action, "--type", "nas:Note", "--attribute", attribute)
        assert out.code == 0, out.envelope
        return out.data  # type: ignore[no-any-return]

    listed_one = decide("body")
    assert listed_one["resource"] == f"graph:{world.graph_id}/type:nas:Note/attr:body"
    assert [(d["effect"], d["rule"]) for d in listed_one["decisions"]] == [
        ("allow", "grant.1.update")
    ]
    assert listed_one["type_decision"]["effect"] == "allow"
    unlisted = decide("title")
    assert [(d["effect"], d["rule"]) for d in unlisted["decisions"]] == [("deny", "default.deny")]
    assert unlisted["type_decision"]["effect"] == "allow"  # the type is writable, the term is not
    lifecycle = decide("status", "write.transition")
    assert lifecycle["decisions"][0]["effect"] == "allow"
    # an exact type limit does not match the terms below it: the term is allowed, the type is
    # not, and a write needs both
    install(run_cli, world, NOTE_READ_ONLY, "--graph", "example_graph")
    limited = decide("body")
    assert limited["decisions"][0]["effect"] == "allow"
    assert limited["type_decision"]["effect"] == "deny"
    assert limited["type_decision"]["rule"].startswith("type.")
    assert permitted(run_cli, probe(world, "nas:Note", "update", "body")) is False


def test_explain_decides_on_one_relation(run_cli: CliRunner, world: World) -> None:
    def decide(relation: str, action: str = "write.link") -> dict[str, Any]:
        out = explain(run_cli, "--action", action, "--type", "nas:Note", "--relation", relation)
        assert out.code == 0, out.envelope
        return out.data  # type: ignore[no-any-return]

    about = decide("nas:about")
    assert about["resource"] == f"graph:{world.graph_id}/type:nas:Note/rel:nas:about"
    assert about["decisions"][0]["rule"] == "grant.1.link"
    assert about["type_decision"]["effect"] == "allow"
    assert decide("nas:cites")["decisions"][0]["effect"] == "deny"
    assert decide("nas:about", "write.unlink")["decisions"][0]["rule"] == "grant.1.unlink"
    # an inline edge of a create is decided as write.create on the relation (the link right)
    inline = decide("nas:about", "write.create")
    assert inline["decisions"][0]["rule"] == "grant.1.edge"
    assert inline["type_decision"]["effect"] == "allow"
    assert decide("nas:cites", "write.create")["decisions"][0]["effect"] == "deny"


def test_explain_refuses_unusable_term_arguments(run_cli: CliRunner, world: World) -> None:
    def refused(*args: str) -> dict[str, Any]:
        out = explain(run_cli, *args)
        assert out.code != 0, out.envelope
        return out.error  # type: ignore[no-any-return]

    base = ("--action", "write.update", "--type", "nas:Note")
    both = refused(*base, "--attribute", "body", "--relation", "nas:about")
    assert both["code"] == "usage.invalid_argument" and both["details"]["argument"] == "relation"
    no_type = refused("--action", "write.update", "--attribute", "body")
    assert no_type["code"] == "usage.missing_argument" and no_type["details"]["argument"] == "type"
    no_action = refused("--type", "nas:Note", "--relation", "nas:about")
    assert no_action["code"] == "usage.missing_argument"
    assert no_action["details"]["argument"] == "action"
    bad_attribute = refused(*base, "--attribute", "Not-A-Name")
    assert bad_attribute["code"] == "usage.invalid_argument"
    assert bad_attribute["details"]["argument"] == "attribute"
    bad_relation = refused(*base, "--relation", "about")
    assert bad_relation["code"] == "usage.invalid_argument"
    assert bad_relation["details"]["argument"] == "relation"


def test_explain_without_an_action_shows_the_writes(run_cli: CliRunner, world: World) -> None:
    out = explain(run_cli)
    assert out.code == 0, out.envelope
    assert out.data["writes"] == WRITES == whoami(run_cli)["writes"]
    assert "type_decision" not in out.data
    # with an action the answer is one decision, as before
    one = explain(run_cli, "--action", "write.update", "--type", "nas:Note")
    assert "writes" not in one.data
    assert [d["effect"] for d in one.data["decisions"]] == ["allow"]
    # a caller with no list in the explanation gets none
    install(run_cli, world, READ_ONLY)
    assert "writes" not in explain(run_cli).data
