"""S: ``graph whoami`` and ``graph-admin policy explain`` under a type read limit (type read limits
plan V8) against a toy graph, checked against real reads.

The local app caller is limited by an installation grant with ``reads``. ``whoami`` lists the
types it sees; every type is probed with ``describe type`` and ``find --type``, which answer
exactly for the listed ones. The lists of limits and writes never name a type the caller does not
see, and callers that see every type get the answer they got before the feature.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.files import write_pdf
from support.fixtures import GOLDENS, assert_golden
from support.policies import write_policy

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
GRAPH = "example_graph"
# The concrete types of notes_and_sources and the ones the caller below sees
CONCRETE = (
    "files:File",
    "nas:Book",
    "nas:Note",
    "nas:Scan",
    "nas:Topic",
    "nas:WebPage",
    "provenance:Claim",
)
SEEN = ["nas:Note", "nas:Scan", "nas:WebPage", "provenance:Claim"]
HIDDEN = [t for t in CONCRETE if t not in SEEN]
LIMITED = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [nas:Note, nas:Scan, nas:WebPage, provenance:Claim]
    writes:
      nas:Note: {create: [title, body, kind], link: [nas:cites]}
no_project: {"*": write}
"""
# The same grant for another entrypoint: the app caller sees every type, the graph is limited
OTHER_ENTRYPOINT = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting"}
grants:
  - {entrypoint: app, graphs: [example_graph], level: read}
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [nas:Note]
no_project: {"*": write}
"""
NO_READ_RULES = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: read}
no_project: {"*": write}
"""
# The caller is granted topics; the graph layer takes them away, limits notes to reading and
# books and a prefix to reading; none of these names may reach a caller who does not see the type
GRAPH_LAYER = """\
schema: graph_cli.policy/1
id: graph
types:
  nas:Topic: none
  nas:Book: read
  nas:Note: read
  "provenance:*": read
  "files:*": write
"""
# A grant cannot write a type it hides, so the writes on topics need the read of them
NO_TOPICS = "schema: graph_cli.policy/1\nid: graph\ntypes:\n  nas:Topic: none\n"
WITH_TOPICS = LIMITED.replace(
    "reads: [nas:Note, nas:Scan, nas:WebPage, provenance:Claim]",
    "reads: [nas:Note, nas:Scan, nas:Topic, nas:WebPage, provenance:Claim]",
).replace(
    "link: [nas:cites]}\n",
    "link: [nas:about, nas:cites]}\n      nas:Topic: {create: true, update: [description]}\n",
)
SETUP = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t", "attrs": {"label": "Alpha topic"}},
        {"op": "create", "type": "nas:Book", "local": "b", "attrs": {"title": "A book"}},
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
            "attrs": {"title": "A note", "body": "text", "kind": "summary"},
            "edges": [{"rel": "nas:about", "target": "@t"}],
        },
    ]
}


class World:
    def __init__(self, run_cli: CliRunner, work: Path) -> None:
        self.run_cli = run_cli
        self.work = work


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, GRAPH, "notes_and_sources", version="1.1.0")
    write_documents(root, GRAPH, [SETUP])
    for name, extra in (("plain", ()), ("scan", ("--type", "nas:Scan"))):
        path = write_pdf(tmp_path / "files" / f"{name}.pdf", name)
        added = app(run_cli, "file", "add", str(path), *extra, env=ENV)
        assert added.code == 0, added.envelope
    return World(run_cli, tmp_path / "policies")


def install(run_cli: CliRunner, work: Path, text: str, *args: str) -> None:
    name = f"policy_{hashlib.sha256(text.encode()).hexdigest()[:12]}.yaml"
    applied = admin(
        run_cli, "policy", "apply", str(write_policy(work, name, text)), *args, "--reason", "test"
    )
    assert applied.code == 0, applied.envelope


def whoami(run_cli: CliRunner) -> dict[str, Any]:
    outcome = app(run_cli, "whoami", env=ENV)
    assert outcome.code == 0, outcome.envelope
    (graph,) = [g for g in outcome.data["permissions"]["graphs"] if g["graph"] == GRAPH]
    assert isinstance(graph, dict)
    return graph


def explain(run_cli: CliRunner, *args: str) -> Outcome:
    return admin(run_cli, "policy", "explain", "--graph", GRAPH, *args)


def visible_types(run_cli: CliRunner) -> list[str]:
    """The concrete types the caller reaches: ``describe type`` answers and ``find --type`` does
    not refuse (and, for the types holding data, returns it)."""
    found = []
    for type_curie in CONCRETE:
        described = app(run_cli, "describe", "type", type_curie, env=ENV)
        listed = app(run_cli, "find", "--type", type_curie, env=ENV)
        assert (described.code == 0) == (listed.code == 0), (type_curie, described.envelope)
        if described.code == 0:
            found.append(type_curie)
        else:
            assert described.error["code"] == listed.error["code"] == "term.unknown"
    return found


def holding_data(run_cli: CliRunner) -> set[str]:
    """The types of entities ``find`` returns with no type given."""
    items = app(run_cli, "find", "--limit", "100", env=ENV).data["items"]
    return {item["type"] for item in items}


def named(answer: Any, types: list[str]) -> list[str]:
    """The types of ``types`` that appear anywhere in ``answer``."""
    text = json.dumps(answer)
    return [t for t in types if t in text]


# ---- whoami ---------------------------------------------------------------------------------


def test_whoami_lists_the_types_the_caller_sees(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, LIMITED)
    graph = whoami(run_cli)
    assert graph["reads"] == SEEN
    assert graph["level"] == "read"
    # the same set as the reads that work
    assert visible_types(run_cli) == graph["reads"]
    assert holding_data(run_cli) == {"nas:Note", "nas:WebPage", "nas:Scan"}
    assert {"nas:Topic", "nas:Book", "files:File"} & holding_data(run_cli) == set()


def test_whoami_names_no_hidden_type_in_writes(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, WITH_TOPICS)
    graph = whoami(run_cli)
    assert sorted(graph["writes"]) == ["nas:Note", "nas:Topic"]
    assert graph["writes"]["nas:Note"]["link"] == ["nas:about", "nas:cites"]
    # the graph layer takes the topics away: their write list goes, and with them the relation
    # whose range is a topic (the caller's own model view has no such relation either)
    install(run_cli, world.work, NO_TOPICS, "--graph", GRAPH)
    graph = whoami(run_cli)
    assert sorted(graph["writes"]) == ["nas:Note"]
    assert graph["writes"]["nas:Note"]["link"] == ["nas:cites"]
    assert app(run_cli, "describe", "relation", "nas:about", env=ENV).code != 0
    assert named(graph, ["nas:Topic", "nas:Book", "files:File", "nas:about"]) == [], graph


def test_the_graph_layer_limits_show_only_for_types_the_caller_sees(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, WITH_TOPICS)
    install(run_cli, world.work, GRAPH_LAYER, "--graph", GRAPH)
    graph = whoami(run_cli)
    # the grant gives topics, the graph layer's none takes them away again
    assert graph["reads"] == SEEN
    assert graph["types"] == {"nas:Note": "read", "provenance:*": "read"}
    # a hidden type, and a prefix none of whose types is seen, are named nowhere
    assert named(graph, ["nas:Topic", "nas:Book", "files:"]) == []
    assert visible_types(run_cli) == SEEN


def test_the_layer_limits_of_a_caller_who_sees_every_type_stay_whole(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, NO_READ_RULES)
    install(run_cli, world.work, GRAPH_LAYER.replace("  nas:Topic: none\n", ""), "--graph", GRAPH)
    graph = whoami(run_cli)
    assert "reads" not in graph
    assert graph["types"] == {
        "files:*": "write",
        "nas:Book": "read",
        "nas:Note": "read",
        "provenance:*": "read",
    }


def test_a_caller_who_sees_every_type_gets_the_answer_it_got_before(
    run_cli: CliRunner, world: World
) -> None:
    """A policy without read rules, and one that limits another entrypoint, give the same whoami
    for the app caller: no ``reads`` and nothing else changed."""
    install(run_cli, world.work, NO_READ_RULES)
    plain = whoami(run_cli)
    assert sorted(plain) == ["graph", "level", "obligations", "types", "withheld"]
    install(run_cli, world.work, OTHER_ENTRYPOINT)
    explained = explain(run_cli)
    assert explained.code == 0, explained.envelope
    unlimited = whoami(run_cli)
    assert json.dumps(unlimited, sort_keys=True) == json.dumps(plain, sort_keys=True)
    assert "reads" not in explained.data
    assert visible_types(run_cli) == list(CONCRETE)


def test_whoami_answers_for_a_graph_whose_types_are_all_hidden(
    run_cli: CliRunner, world: World
) -> None:
    """Level read for the graph, every type taken away by the graph layer: reads is empty."""
    deny = "schema: graph_cli.policy/1\nid: graph\ntypes:\n" + "".join(
        f'  "{prefix}:*": none\n' for prefix in ("files", "nas", "provenance")
    )
    install(run_cli, world.work, NO_READ_RULES)
    install(run_cli, world.work, deny, "--graph", GRAPH)
    graph = whoami(run_cli)
    assert graph["reads"] == [] and graph["types"] == {}
    assert named(graph, list(CONCRETE)) == []
    assert visible_types(run_cli) == []


# ---- policy explain -------------------------------------------------------------------------


def test_explain_shows_the_same_reads_and_writes_as_whoami(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, LIMITED)
    graph = whoami(run_cli)
    out = explain(run_cli)
    assert out.code == 0, out.envelope
    assert out.data["reads"] == graph["reads"] == SEEN
    assert out.data["writes"] == graph["writes"]
    assert named(out.data, HIDDEN) == []
    assert "type_decision" not in out.data


def test_explain_with_an_action_decides_one_type_and_adds_no_reads(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, LIMITED)
    for type_curie in CONCRETE:
        out = explain(run_cli, "--action", "read.get", "--type", type_curie)
        assert out.code == 0, out.envelope
        (decision,) = out.data["decisions"]
        assert "reads" not in out.data and "writes" not in out.data
        assert (decision["effect"] == "allow") == (type_curie in SEEN), (type_curie, decision)
        if type_curie in SEEN:
            assert decision["rule"].endswith(".reads"), decision


def test_explain_reflects_none_and_graph_denies(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, WITH_TOPICS)
    assert explain(run_cli).data["reads"] == [
        "nas:Note",
        "nas:Scan",
        "nas:Topic",
        "nas:WebPage",
        "provenance:Claim",
    ]
    install(run_cli, world.work, GRAPH_LAYER, "--graph", GRAPH)
    out = explain(run_cli)
    assert out.data["reads"] == SEEN
    topic = explain(run_cli, "--action", "read.get", "--type", "nas:Topic").data["decisions"][0]
    assert topic["effect"] == "deny" and topic["rule"] == "type.nas:Topic"
    # a graph deny of read.get on one type hides it as well
    deny = (
        "schema: graph_cli.policy/1\nid: graph\ndeny:\n"
        "  - {id: no_scans, action: [read.get], type: [nas:Scan]}\n"
    )
    install(run_cli, world.work, deny, "--graph", GRAPH)
    out = explain(run_cli)
    assert out.data["reads"] == ["nas:Note", "nas:Topic", "nas:WebPage", "provenance:Claim"]
    assert visible_types(run_cli) == out.data["reads"]


def test_explain_for_callers_that_see_every_type_adds_nothing(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, NO_READ_RULES)
    out = explain(run_cli)
    assert out.code == 0, out.envelope
    assert "reads" not in out.data and "writes" not in out.data


# ---- goldens --------------------------------------------------------------------------------


def test_goldens_of_a_limited_reader(run_cli: CliRunner, world: World) -> None:
    """The graph entry of ``whoami`` and the parts of ``policy explain`` that do not hold the
    graph id, for the reader of ``WITH_TOPICS`` with ``GRAPH_LAYER`` on top (notes writable)."""
    install(run_cli, world.work, WITH_TOPICS)
    # notes stay writable here, so the golden shows their write list
    layer = GRAPH_LAYER.replace("  nas:Note: read\n", "  nas:Scan: read\n")
    install(run_cli, world.work, layer, "--graph", GRAPH)
    explained = explain(run_cli).data
    shown_by_explain = {k: explained[k] for k in ("level", "withheld", "reads", "writes")}
    for name, document in (
        ("whoami_limited_reader", whoami(run_cli)),
        ("policy_explain_limited_reader", shown_by_explain),
    ):
        assert_golden(
            GOLDENS / "access_control" / f"{name}.json",
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
        )
