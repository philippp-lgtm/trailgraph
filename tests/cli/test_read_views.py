"""S: one smoke test per read command family under a type read limit (type read limits plan V5;
the exhaustive "as if absent" tests per command follow in the next step).

The local app caller is limited by an installation grant with ``reads``: it sees notes, web pages,
books, scans and claims but not topics (and not the extended ``files:File``). Every answer about
a topic must equal the answer about an entity or term that does not exist; a visible note loses its
edge to a topic, with a hash that matches what is shown; a caller that sees every type gets the
raw answers.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.app import app, create_graph
from support.read_limits import (
    NOT_THERE,
    install,
    shown,
    the_same_but_for,
    without_suggestions,
)

from graph_cli.kernel.records import EdgeRecord, content_hash, content_projection

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}

LIMITED = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [nas:Note, nas:WebPage, nas:Book, nas:Scan, provenance:Claim]
no_project: {"*": write}
"""
# The app caller reads everything; another entrypoint is limited, so the graph is read limited.
UNLIMITED_ON_A_LIMITED_GRAPH = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: read}
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [nas:Note]
roles:
  reporting: {description: "Reporting: notes only"}
no_project: {"*": write}
"""
SETUP = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t1", "attrs": {"label": "Alpha topic"}},
        {"op": "create", "type": "nas:Topic", "local": "t2", "attrs": {"label": "Beta topic"}},
        {
            "op": "create",
            "type": "nas:WebPage",
            "local": "w",
            "attrs": {"title": "A page", "url": "https://example.org/p"},
        },
        {
            "op": "create",
            "type": "nas:Note",
            "local": "n",
            "attrs": {"title": "First note", "body": "b", "kind": "summary"},
            "edges": [
                {"rel": "nas:about", "target": "@t1"},
                {"rel": "nas:cites", "target": "@w"},
            ],
        },
        {
            "op": "create",
            "type": "nas:Note",
            "local": "m",
            "attrs": {"title": "Second note", "body": "c", "kind": "summary"},
            "edges": [{"rel": "nas:about", "target": "@t2"}],
        },
    ]
}


@dataclass
class World:
    ids: dict[str, str]
    aliases: dict[str, str]
    work: Path
    baseline: dict[str, Any]


def answers(run_cli: CliRunner, ids: dict[str, str]) -> dict[str, Any]:
    """The answers of the read commands that must not change for a caller who sees everything."""

    def run(*args: str) -> dict[str, Any]:
        return shown(app(run_cli, *args, env=ENV))

    return {
        "get note": run("get", ids["n"]),
        "get full": run("get", ids["n"], "--full"),
        "get topic": run("get", ids["t1"]),
        "find": run("find", "--limit", "100"),
        "find notes": run("find", "--type", "nas:Note"),
        "neighbors": run("neighbors", ids["n"]),
        "neighbors deep": run("neighbors", ids["w"], "--direction", "in", "--depth", "3"),
        "history": run("history", ids["n"]),
        "query": run("query", "MATCH (n:Note) RETURN n ORDER BY n.title"),
        "query all": run("query", "MATCH (n) RETURN count(n) AS n"),
        "describe types": run("describe", "types"),
        "lookup": run("lookup", "topic"),
        "search": run("search", "topic"),
    }


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    # Written before any policy is installed: the built-in one lets the app write
    done = app(run_cli, "apply", "-", env=ENV, input=json.dumps(SETUP))
    assert done.code == 0, done.envelope
    ids = {e["local"]: e["id"] for e in done.data["results"][0]["result"]["entities"]}
    aliases = {
        local: app(run_cli, "get", entity, env=ENV).data["entity"]["alias"]
        for local, entity in ids.items()
    }
    return World(ids, aliases, tmp_path / "policies", answers(run_cli, ids))


@pytest.fixture
def limited(run_cli: CliRunner, world: World) -> World:
    install(run_cli, world.work, LIMITED)
    return world


def test_a_hidden_entity_is_as_missing_as_an_unknown_id(run_cli: CliRunner, limited: World) -> None:
    hidden_id = limited.ids["t1"]
    hidden = shown(app(run_cli, "get", hidden_id, env=ENV))
    missing = shown(app(run_cli, "get", NOT_THERE, env=ENV))
    assert hidden["error"]["code"] == "entity.not_found"
    the_same_but_for(hidden, missing, (hidden_id, NOT_THERE))
    # by alias and by the end of its id, as an unknown alias and an unknown suffix answer
    alias = limited.aliases["t1"]
    by_alias = shown(app(run_cli, "get", alias, env=ENV))
    unknown_alias = shown(app(run_cli, "get", alias[:-1] + "9", env=ENV))
    the_same_but_for(by_alias, unknown_alias, (alias, alias[:-1] + "9"))
    suffix = hidden_id.replace("-", "")[-10:]
    by_suffix = shown(app(run_cli, "get", suffix, env=ENV))
    unknown_suffix = shown(app(run_cli, "get", "0123456789", env=ENV))
    the_same_but_for(by_suffix, unknown_suffix, (suffix, "0123456789"))
    for rev in ("1", "2"):
        rev_hidden = shown(app(run_cli, "get", hidden_id, "--rev", rev, env=ENV))
        rev_missing = shown(app(run_cli, "get", NOT_THERE, "--rev", rev, env=ENV))
        the_same_but_for(rev_hidden, rev_missing, (hidden_id, NOT_THERE))


def test_a_visible_entity_has_no_edge_to_a_hidden_one_and_a_matching_hash(
    run_cli: CliRunner, limited: World
) -> None:
    raw = limited.baseline["get full"]["data"]["entity"]
    assert {e["rel"] for e in raw["edges"]} == {"nas:about", "nas:cites"}
    outcome = app(run_cli, "get", limited.ids["n"], "--full", env=ENV)
    entity = outcome.data["entity"]
    assert [e["rel"] for e in entity["edges"]] == ["nas:cites"]
    assert limited.ids["t1"] not in json.dumps(outcome.envelope)
    projection = content_projection(
        type=entity["type"],
        record_status=entity["record_status"],
        alias=entity["alias"],
        attrs=entity["attrs"],
        edges=tuple(EdgeRecord.from_json(e) for e in entity["edges"]),
        valid_from=entity["valid_from"],
        valid_to=entity["valid_to"],
    )
    assert entity["content_hash"] == content_hash(projection) != raw["content_hash"]
    assert (entity["rev"], entity["tx_seq"]) == (raw["rev"], raw["tx_seq"])
    lean = app(run_cli, "get", limited.ids["n"], env=ENV).data["entity"]
    assert [e["rel"] for e in lean["edges"]] == ["nas:cites"]


def test_find_lists_no_hidden_entity_and_counts_what_is_left(
    run_cli: CliRunner, limited: World
) -> None:
    raw = limited.baseline["find"]
    outcome = app(run_cli, "find", "--limit", "100", env=ENV)
    ids = {i["id"] for i in outcome.data["items"]}
    assert limited.ids["t1"] not in ids and limited.ids["t2"] not in ids
    assert ids == {limited.ids[k] for k in ("n", "m", "w")}
    assert outcome.page is not None and outcome.page["total"] == 3
    assert len(raw["data"]["items"]) == 5
    for item in outcome.data["items"]:
        assert limited.ids["t1"] not in json.dumps(item)
    hidden_type = app(run_cli, "find", "--type", "nas:Topic", env=ENV)
    unknown_type = app(run_cli, "find", "--type", "nas:Nothing", env=ENV)
    assert hidden_type.error["code"] == unknown_type.error["code"] == "term.unknown"
    the_same_but_for(
        without_suggestions(shown(hidden_type)),
        without_suggestions(shown(unknown_type)),
        ("nas:Topic", "nas:Nothing"),
        ("Topic", "Nothing"),
    )
    # an abstract type expands to the visible subtypes only
    sources = app(run_cli, "find", "--type", "nas:Source", env=ENV)
    assert [i["type"] for i in sources.data["items"]] == ["nas:WebPage"]
    # a relation filter naming a hidden id is an empty page, as one naming a missing id
    rel = app(
        run_cli, "find", "--type", "nas:Note", "--rel", f"nas:cites={limited.ids['t1']}", env=ENV
    )
    gone = app(run_cli, "find", "--type", "nas:Note", "--rel", f"nas:cites={NOT_THERE}", env=ENV)
    assert (rel.code, rel.data["items"]) == (gone.code, gone.data["items"]) == (0, [])
    # the relation to the hidden type is unknown, as a relation that does not exist
    about = app(run_cli, "find", "--type", "nas:Note", "--rel", f"nas:about={NOT_THERE}", env=ENV)
    nothing = app(
        run_cli, "find", "--type", "nas:Note", "--rel", f"nas:nothing={NOT_THERE}", env=ENV
    )
    assert about.error["code"] == nothing.error["code"] == "term.unknown"


def test_neighbors_and_history_of_a_hidden_entity_are_as_for_a_missing_one(
    run_cli: CliRunner, limited: World
) -> None:
    hidden_id = limited.ids["t1"]
    for command in ("neighbors", "history"):
        hidden = shown(app(run_cli, command, hidden_id, env=ENV))
        missing = shown(app(run_cli, command, NOT_THERE, env=ENV))
        assert hidden["error"]["code"] == "entity.not_found", command
        the_same_but_for(hidden, missing, (hidden_id, NOT_THERE))
    near = app(run_cli, "neighbors", limited.ids["n"], env=ENV)
    assert {i["entity"]["id"] for i in near.data["items"]} == {limited.ids["w"]}
    deep = app(run_cli, "neighbors", limited.ids["w"], "--direction", "in", "--depth", "3", env=ENV)
    assert {i["entity"]["id"] for i in deep.data["items"]} == {
        limited.ids["n"]
    }  # not the topic behind it
    history = app(run_cli, "history", limited.ids["n"], env=ENV)
    assert history.code == 0 and limited.ids["t1"] not in json.dumps(history.envelope)
    assert history.data["items"][0]["changed"] is None or all(
        "nas:about" not in json.dumps(i["changed"]) for i in history.data["items"]
    )


def test_a_label_scan_with_a_hidden_label_is_an_unknown_label(
    run_cli: CliRunner, limited: World
) -> None:
    hidden = shown(app(run_cli, "query", "MATCH (n:Topic) RETURN n", env=ENV))
    missing = shown(app(run_cli, "query", "MATCH (n:Zzzz) RETURN n", env=ENV))
    assert hidden["error"]["code"] == "term.unknown"
    the_same_but_for(hidden, missing, ("Topic", "Zzzz"))
    notes = app(run_cli, "query", "MATCH (n:Note) RETURN n", env=ENV)
    for row in notes.data["rows"] if "rows" in notes.data else notes.data["items"]:
        assert "nas:about" not in json.dumps(row)
    total = app(run_cli, "query", "MATCH (n) RETURN count(n) AS n", env=ENV)
    assert json.dumps(total.data).count("3") >= 1 and "5" not in json.dumps(total.data["items"])
    assert limited.ids["t1"] not in json.dumps(notes.envelope)


def test_describe_shows_no_hidden_type(run_cli: CliRunner, limited: World) -> None:
    hidden = shown(app(run_cli, "describe", "type", "nas:Topic", env=ENV))
    missing = shown(app(run_cli, "describe", "type", "nas:Nothing", env=ENV))
    assert hidden["error"]["code"] == "term.unknown"
    the_same_but_for(
        without_suggestions(hidden),
        without_suggestions(missing),
        ("nas:Topic", "nas:Nothing"),
        ("Topic", "Nothing"),
    )
    suggested = hidden["error"]["details"].get("did_you_mean", [])
    assert "nas:Topic" not in suggested
    types = app(run_cli, "describe", "types", env=ENV)
    assert "nas:Topic" not in json.dumps(types.data)
    assert "nas:Note" in json.dumps(types.data)
    note = app(run_cli, "describe", "type", "nas:Note", env=ENV)
    assert "nas:about" not in json.dumps(note.data)  # its range is the hidden topic
    relations = app(run_cli, "describe", "relations", env=ENV)
    assert "nas:about" not in json.dumps(relations.data)


def test_lookup_and_search_find_no_hidden_entity(run_cli: CliRunner, limited: World) -> None:
    for command in ("lookup", "search"):
        outcome = app(run_cli, command, "Alpha topic", env=ENV)
        assert limited.ids["t1"] not in json.dumps(outcome.envelope), command
        found = app(run_cli, command, "First note", env=ENV)
        assert limited.ids["n"] in json.dumps(found.data), command


def test_a_caller_who_sees_every_type_gets_the_raw_answers(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, UNLIMITED_ON_A_LIMITED_GRAPH)
    assert answers(run_cli, world.ids) == world.baseline
    install(run_cli, world.work, LIMITED)
    assert answers(run_cli, world.ids) != world.baseline


def test_a_policy_without_read_rules_changes_nothing(run_cli: CliRunner, world: World) -> None:
    install(
        run_cli,
        world.work,
        LIMITED.replace(
            "    reads: [nas:Note, nas:WebPage, nas:Book, nas:Scan, provenance:Claim]\n", ""
        ),
    )
    assert answers(run_cli, world.ids) == world.baseline
