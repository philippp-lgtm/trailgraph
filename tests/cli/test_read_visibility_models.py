"""S: read views of a type-limited graph on the two other toy models: the facts that tell lookup
candidates apart (company), and affordances and functions (inventory); type read limits plan V5,
V7 (the parts of V7 that are not about hiding functions and actions by name).

The app caller is limited by an installation grant with ``reads``. Per test the answers of the
caller who sees everything are taken first, so each check shows what the limit removed.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES, ONTOLOGIES
from support.read_limits import install, shown, the_same_but_for, without_suggestions

from graph_cli.kernel.changeset import read_documents

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [{types}]
no_project: {{"*": write}}
"""
COUNT_THINGS = """\
schema: graph_cli.function/1
name: count_things
summary: Number of entities of every kind.
use_when: How many things the graph holds, whatever their type.
params: {}
query: |
  MATCH (n) RETURN count(n) AS things
returns:
  - column: things
    summary: Number of entities.
examples:
  - question: How many things are there?
"""


def ok(run_cli: CliRunner, *args: str) -> Outcome:
    outcome = app(run_cli, *args, env=ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def limit(run_cli: CliRunner, tmp_path: Path, *types: str) -> None:
    install(run_cli, tmp_path / "policies", POLICY.format(types=", ".join(types)))


# ---- company: the facts that tell lookup candidates apart --------------------------------

COMPANY_VISIBLE = ("ops:Guideline", "ops:Process", "ops:Training", "org:Department")
COMPANY_VISIBLE += ("org:Position", "ppl:Person")


@pytest.fixture
def company(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(ONTOLOGIES / "company" / "1.1.0"), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    seeds = read_documents((FIXTURES / "seeds" / "company" / "changes.jsonl").read_bytes())
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    return root


def facts(outcome: Outcome) -> dict[str, list[tuple[str, str | None]]]:
    """Per candidate label: the (relation, label of the target) of its facts."""
    found = {}
    for item in outcome.data["items"]:
        found[item["label"]] = [(f["rel"], f["label"]) for f in item["facts"] or []]
    return found


def test_lookup_facts_name_no_hidden_target(
    run_cli: CliRunner, company: Path, tmp_path: Path
) -> None:
    prefix = ("lookup", "Department A", "--match", "prefix")
    raw = ok(run_cli, *prefix)
    assert facts(raw) == {
        "Department A": [("org:charged_to", "Operations costs")],
        "Department A1": [("org:part_of", "Department A")],
    }
    team = ok(run_cli, "lookup", "Team A", "--match", "prefix")
    assert set(facts(team)) == {"Team A1", "Team A2"}
    lead_raw = ok(run_cli, "lookup", "Lead of Team", "--match", "prefix")
    assert ("org:in_unit", "Team A1") in facts(lead_raw)["Lead of Team A1"]
    limit(run_cli, tmp_path, *COMPANY_VISIBLE)  # hides cost centers and teams
    limited = ok(run_cli, *prefix)
    assert facts(limited) == {
        "Department A": [],  # the cost center it is charged to is hidden
        "Department A1": [("org:part_of", "Department A")],  # a department stays a fact
    }
    assert "Operations costs" not in json.dumps(limited.envelope)
    assert "charged_to" not in json.dumps(limited.envelope)
    # the other candidates are gone, as if no such entity existed
    asked = shown(app(run_cli, "lookup", "Team A", "--match", "prefix", env=ENV))
    absent = shown(app(run_cli, "lookup", "Nothing A", "--match", "prefix", env=ENV))
    the_same_but_for(asked, absent, ("Team A", "Nothing A"))
    assert asked["data"]["items"] == []
    # a position keeps the facts about visible entities and loses the one about its hidden team
    head = ok(run_cli, "lookup", "Head of Department", "--match", "prefix")
    assert facts(head) == {
        "Head of Department A": [("org:in_unit", "Department A")],
        "Head of Department B": [("org:in_unit", "Department B")],
    }
    lead = ok(run_cli, "lookup", "Lead of Team", "--match", "prefix")
    assert facts(lead) == {
        "Lead of Team A1": [("org:reports_to", "Head of Department A")],
        "Lead of Team B1": [("org:reports_to", "Head of Department B")],
    }
    assert "org:in_unit" not in json.dumps(lead.envelope)


def test_lookup_fit_names_no_hidden_unit(run_cli: CliRunner, company: Path, tmp_path: Path) -> None:
    raw = ok(run_cli, "lookup", "Department A", "--for", "part_of")
    assert "org:Team" in raw.data["fit"]["types"]
    limit(run_cli, tmp_path, *COMPANY_VISIBLE)
    fit = ok(run_cli, "lookup", "Department A", "--for", "part_of")
    assert fit.data["fit"]["types"] == ["org:Department", "org:OrgUnit"]
    source = ok(run_cli, "lookup", "Department A", "--for-source", "part_of")
    assert source.data["fit"]["types"] == ["org:Department", "org:OrgUnit"]
    for option in ("--for", "--for-source"):
        for hidden, other in (("charged_to", "nothing"), ("org:charged_to", "org:nothing")):
            asked = shown(app(run_cli, "lookup", "Department A", option, hidden, env=ENV))
            absent = shown(app(run_cli, "lookup", "Department A", option, other, env=ENV))
            assert asked["error"]["code"] == "term.unknown"
            for answer in (asked, absent):
                answer["error"]["details"].pop("did_you_mean", None)
            the_same_but_for(asked, absent, (hidden.split(":")[-1], "nothing"))


# ---- inventory: affordances and functions ------------------------------------------------


def item(local: str, name: str, location: str, *edges: dict[str, str], **attrs: Any) -> dict:  # type: ignore[type-arg]
    return {
        "op": "create",
        "type": "inv:Item",
        "local": local,
        "attrs": {"name": name, **attrs},
        "edges": [{"rel": "inv:located_in", "target": location}, *edges],
    }


@pytest.fixture
def inventory(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    functions = tmp_path / "functions"
    shutil.copytree(FIXTURES / "functions" / "inventory" / "valid", functions)
    (functions / "count_things.yaml").write_text(COUNT_THINGS)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions))
    assert published.code == 0, published.envelope
    part = {"rel": "inv:part_of", "target": "@chair"}
    document = {
        "ops": [
            {
                "op": "create",
                "type": "inv:Location",
                "local": "shelf",
                "attrs": {"name": "Shelf", "code": "S1"},
            },
            {
                "op": "create",
                "type": "inv:Location",
                "local": "rack",
                "attrs": {"name": "Rack", "code": "R1"},
            },
            {"op": "create", "type": "inv:Category", "local": "books", "attrs": {"name": "Books"}},
            item("chair", "Chair", "@shelf", {"rel": "inv:in_category", "target": "@books"}),
            item("seat", "Seat", "@rack", part),
            item("leg", "Leg", "@shelf", part, condition="damaged"),
            item("lamp", "Lamp", "@shelf", condition="damaged"),
        ]
    }
    write_documents(root, "example_graph", [document])
    found = ok(run_cli, "find", "--limit", "100", "--type", "inv:Item")
    ids = {i["attrs"]["name"].lower(): i["id"] for i in found.data["items"]}
    for kind in ("Location", "Category"):
        for entity in ok(run_cli, "find", "--type", f"inv:{kind}").data["items"]:
            ids[entity["attrs"]["name"].lower()] = entity["id"]
    return ids


def relation_counts(outcome: Outcome) -> list[tuple[str, str, int]]:
    return [
        (r["relation"], r["direction"], r["count"])
        for r in outcome.data["affordances"]["relations"]
    ]


def test_affordances_count_and_name_only_what_the_view_holds(
    run_cli: CliRunner, inventory: dict[str, str], tmp_path: Path
) -> None:
    ids = inventory
    chair = ok(run_cli, "get", ids["chair"], "--affordances")
    shelf = ok(run_cli, "get", ids["shelf"], "--affordances")
    assert relation_counts(chair) == [
        ("inv:in_category", "out", 1),
        ("inv:part_of", "out", 0),
        ("inv:part_of", "in", 2),
    ]
    assert chair.data["affordances"]["full"] == ["inv:located_in"]
    assert ("inv:located_in", "in", 3) in relation_counts(shelf)
    # items only: the relations to places and categories are gone from what an item can do
    limit(run_cli, tmp_path, "inv:Item")
    limited = ok(run_cli, "get", ids["chair"], "--affordances")
    assert relation_counts(limited) == [("inv:part_of", "out", 0), ("inv:part_of", "in", 2)]
    assert limited.data["affordances"]["full"] == []
    assert limited.data["affordances"]["transitions"] == chair.data["affordances"]["transitions"]
    text = json.dumps(limited.envelope)
    assert "located_in" not in text and "in_category" not in text
    for place in ("shelf", "rack", "books"):
        hidden = shown(app(run_cli, "get", ids[place], "--affordances", env=ENV))
        absent = shown(
            app(run_cli, "get", "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9", "--affordances", env=ENV)
        )
        the_same_but_for(hidden, absent, (ids[place], "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"))
    # places and categories only: the items that point at them are not counted, their relations
    # are not offered
    limit(run_cli, tmp_path, "inv:Location", "inv:Category")
    place = ok(run_cli, "get", ids["shelf"], "--affordances")
    assert relation_counts(place) == [("inv:within", "out", 0), ("inv:within", "in", 0)]
    assert "located_in" not in json.dumps(place.envelope)
    category = ok(run_cli, "get", ids["books"], "--affordances")
    assert category.data["affordances"]["relations"] == []
    assert place.data["entity"]["edges"] == []


def test_a_function_that_names_no_hidden_term_runs_over_the_view(
    run_cli: CliRunner, inventory: dict[str, str], tmp_path: Path
) -> None:
    ids = inventory
    damaged = ("run", "items_by_condition", "--param", "condition=damaged")
    raw = ok(run_cli, *damaged)
    assert [r["item"] for r in raw.data["items"]] == ["Lamp", "Leg"]
    assert ok(run_cli, "run", "count_things").data["items"] == [{"things": 7}]
    limit(run_cli, tmp_path, "inv:Item")  # the damaged items are visible
    assert [r["item"] for r in ok(run_cli, *damaged).data["items"]] == ["Lamp", "Leg"]
    # a node without a label reaches every type; the view counts what the caller sees
    assert ok(run_cli, "run", "count_things").data["items"] == [{"things": 4}]
    limit(run_cli, tmp_path, "inv:Location", "inv:Category")  # no item is visible
    assert ok(run_cli, "run", "count_things").data["items"] == [{"things": 3}]
    # a reference parameter to a hidden entity is a reference to a missing one
    limit(run_cli, tmp_path, "inv:Item", "inv:Location")  # the category is hidden
    where = ("run", "items_in_location", "--param")
    asked = shown(app(run_cli, *where, f"location={ids['books']}", env=ENV))
    absent = shown(app(run_cli, *where, "location=0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9", env=ENV))
    assert asked["error"]["code"] == "entity.not_found"
    the_same_but_for(asked, absent, (ids["books"], "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"))


# ---- a release that adds a type ------------------------------------------------------------

EVERYTHING = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""


@pytest.mark.doc_claim("access_control.new_types_stay_hidden")
def test_a_type_added_by_a_release_stays_hidden_from_a_reads_limited_caller(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    notes = ONTOLOGIES / "notes_and_sources"
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(notes / "1.1.0"))
    assert created.code == 0, created.envelope

    def types() -> list[str]:
        return [t["curie"] for t in ok(run_cli, "describe", "types").data["types"]]

    limit(run_cli, tmp_path, "nas:Note", "nas:Topic")
    before = types()
    assert before == ["nas:Note", "nas:Topic"]
    released = admin(run_cli, "ontology", "release", "example_graph", str(notes / "1.2.0"))
    assert released.code == 0, released.envelope
    assert types() == before  # the new type is not in the list of the caller
    asked = shown(app(run_cli, "describe", "type", "nas:GlossaryEntry", env=ENV))
    absent = shown(app(run_cli, "describe", "type", "nas:Nothing", env=ENV))
    assert asked["error"]["code"] == "term.unknown"
    the_same_but_for(
        without_suggestions(asked), without_suggestions(absent), ("GlossaryEntry", "Nothing")
    )
    heading = ("--set-json", 'heading=[{"en": "Term"}]')
    hidden = shown(app(run_cli, "create", "nas:GlossaryEntry", *heading, env=ENV))
    missing = shown(app(run_cli, "create", "nas:Nothing", *heading, env=ENV))
    assert hidden["code"] != 0 and hidden["error"]["code"] == "access.denied"
    the_same_but_for(hidden, missing, ("GlossaryEntry", "Nothing"))
    # a caller without a limit sees the new type at once
    install(run_cli, tmp_path / "policies", EVERYTHING)
    assert "nas:GlossaryEntry" in types()
