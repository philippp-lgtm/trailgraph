"""S: finding published views (object views stage 2, step 7; plan V12, T4, T5).

The inventory graph holds the inventory functions and the inventory views
(``tests/fixtures/views/inventory``) plus one more entity view of locations that is not the
default; a second graph without views holds the same data. ``describe views``, ``describe view``,
``describe search --kind view`` and the ``views`` affordance of an entity name the views; the
second graph answers every older command exactly as before.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
PLAIN = {"GRAPH_CLI_GRAPH": "plain_graph"}
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
SUMMARY = """\
schema: graph_cli.view/1
name: location_summary
summary: A location with its code, short.
use_when: A quick look at a place when the overview lists too much.
takes: inv:Location
sections:
  - kind: fields
    fields: [name, code]
  - kind: affordances
"""


def documents() -> dict[str, Any]:
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "inv:Location", "local": place, "attrs": attrs}
        for place, attrs in (
            ("shelf", {"name": "Shelf room", "code": "SH"}),
            ("cellar", {"name": "Cellar", "code": "CE"}),
        )
    ]
    ops.append(
        {
            "op": "create",
            "type": "inv:Item",
            "local": "chair",
            "attrs": {"name": "Chair", "condition": "good"},
            "edges": [{"rel": "inv:located_in", "target": "@shelf"}],
        }
    )
    return {"ops": ops}


def publish_into(run_cli: CliRunner, graph: str, kind: str, folder: Path) -> None:
    published = admin(run_cli, "graph", kind, "publish", graph, str(folder))
    assert published.code == 0, published.envelope


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    views = tmp_path / "views"
    shutil.copytree(VIEWS, views)
    (views / "location_summary.yaml").write_text(SUMMARY)
    for graph in ("example_graph", "plain_graph"):
        create_graph(run_cli, graph, "inventory")
        publish_into(run_cli, graph, "functions", FUNCTIONS)
        write_documents(root, graph, [documents()])
    publish_into(run_cli, "example_graph", "views", views)
    return root


def ok(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    outcome = app(run_cli, *args, env=env or ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def alias_of(run_cli: CliRunner, kind: str, env: dict[str, str] | None = None) -> str:
    found = ok(run_cli, "find", "--type", kind, env=env).data["items"]
    return str(found[0]["alias"])


# ---- describe views / describe view ----------------------------------------------------------


def test_describe_views_lists_the_views_with_what_they_take(run_cli: CliRunner, root: Path) -> None:
    data = ok(run_cli, "describe", "views").data
    assert data["schema"] == "graph_cli.model/1"
    by_name = {v["name"]: v for v in data["views"]}
    assert list(by_name) == [
        "item_catalog",
        "items_in_location",
        "location_overview",
        "location_summary",
    ]
    assert by_name["location_overview"]["takes"] == "inv:Location"
    assert by_name["location_overview"]["default"] is True
    assert "default" not in by_name["location_summary"]
    assert "takes" not in by_name["item_catalog"]
    assert by_name["items_in_location"]["signature"] == "location: ref inv:Location"
    assert all(v["status"] == "active" for v in by_name.values())
    text = run_cli("graph", "describe", "views", env=ENV)
    assert text.returncode == 0
    assert "location_overview()  [inventory]  (takes inv:Location, default)" in text.stdout
    assert "item_catalog()  [inventory]  (set view)" in text.stdout


def test_a_graph_without_views_lists_none(run_cli: CliRunner, root: Path) -> None:
    assert ok(run_cli, "describe", "views", env=PLAIN).data["views"] == []
    text = run_cli("graph", "describe", "views", env=PLAIN)
    assert text.returncode == 0 and text.stdout == "-\n"
    missing = app(run_cli, "describe", "view", "location_overview", env=PLAIN)
    assert (missing.code, missing.error["code"]) == (1, "view.not_found")


def test_describe_view_gives_the_definition_and_how_to_call_it(
    run_cli: CliRunner, root: Path
) -> None:
    entity = ok(run_cli, "describe", "view", "location_overview").data["view"]
    assert entity["takes"] == "inv:Location" and entity["default"] is True
    assert [(s["kind"], s["id"]) for s in entity["sections"]] == [
        ("fields", "fields"),
        ("neighbors", "items"),
        ("rows", "contents"),
    ]
    assert entity["touches"]["functions"] == ["items_in_location"]
    assert entity["hash"].startswith("sha256:")
    assert entity["call"] == "graph view REF --graph example_graph --name location_overview"
    parameterised = ok(run_cli, "describe", "view", "items_in_location").data["view"]
    assert parameterised["takes"] is None
    assert parameterised["call"] == (
        "graph view --graph example_graph --name items_in_location --param location=VALUE"
    )
    text = run_cli("graph", "describe", "view", "items_in_location", env=ENV).stdout
    assert text.startswith("items_in_location(location: ref inv:Location)  [inventory]\n")
    assert "  set view\n" in text and "  contents  rows" not in text
    assert "Call: graph view --graph example_graph --name items_in_location --param" in text
    overview = run_cli("graph", "describe", "view", "location_overview", env=ENV).stdout
    assert "  contents  rows  items_in_location\n" in overview
    assert "  items  neighbors  inv:located_in (in)\n" in overview
    assert "Functions run: items_in_location\n" in overview


def test_the_hash_of_describe_is_the_hash_of_the_answer(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location")
    described = ok(run_cli, "describe", "view", "location_overview").data["view"]["hash"]
    assert ok(run_cli, "view", shelf).data["view"]["hash"] == described


def test_an_unknown_view_names_the_close_ones_and_the_search(
    run_cli: CliRunner, root: Path
) -> None:
    missing = app(run_cli, "describe", "view", "location_overviw", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "view.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "location_overview"
    assert missing.error["hint"].endswith("describe search <words> --kind view")


# ---- describe search ------------------------------------------------------------------------


def test_describe_search_finds_views_next_to_the_other_kinds(
    run_cli: CliRunner, root: Path
) -> None:
    only = ok(run_cli, "describe", "search", "overview", "--kind", "view").data["items"]
    assert [(i["kind"], i["curie"]) for i in only] == [
        ("view", "location_overview"),
        ("view", "location_summary"),  # its purpose text says "the overview"
    ]
    assert only[0]["match"] == {"field": "name", "how": "word"}
    assert only[1]["match"] == {"field": "use_when", "how": "phrase"}
    assert only[0]["module"] == "inventory" and only[0]["of"] is None
    purpose = ok(run_cli, "describe", "search", "overview lists too much", "--kind", "view").data
    assert [i["curie"] for i in purpose["items"]] == ["location_summary"]
    assert purpose["items"][0]["match"]["field"] == "use_when"
    every = ok(run_cli, "describe", "search", "location").data["items"]
    assert {"type", "view"} <= {i["kind"] for i in every}
    text = run_cli("graph", "describe", "search", "catalog", "--kind", "view", env=ENV).stdout
    assert text.startswith("view  item_catalog  [inventory]  Every item and location")


def test_a_graph_without_views_has_no_view_hits(run_cli: CliRunner, root: Path) -> None:
    for kind in ("view", None):
        extra = ["--kind", kind] if kind else []
        found = ok(run_cli, "describe", "search", "overview", *extra, env=PLAIN).data["items"]
        assert all(i["kind"] != "view" for i in found)


# ---- the affordance --------------------------------------------------------------------------


def test_get_affordances_names_the_other_views_of_the_class(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location")
    found = ok(run_cli, "get", shelf, "--affordances").data["affordances"]
    assert found["views"] == [{"view": "location_summary"}]  # the default is what `view` shows
    assert "views_more" not in found
    item = alias_of(run_cli, "inv:Item")
    assert "views" not in ok(run_cli, "get", item, "--affordances").data["affordances"]


def test_the_card_names_the_views_and_leaves_out_the_one_it_shows(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = alias_of(run_cli, "inv:Location")
    card = ok(run_cli, "view", shelf, "--name", "card").data
    affordances = next(s for s in card["sections"] if s["kind"] == "affordances")
    assert affordances["views"] == ["location_summary"]
    shown = ok(run_cli, "view", shelf, "--name", "location_summary").data
    section = next(s for s in shown["sections"] if s["kind"] == "affordances")
    assert "views" not in section  # the default is what `view` shows; this one is shown now
    text = run_cli("graph", "view", shelf, "--name", "card", env=ENV).stdout
    assert "view --name location_summary" in text


def test_a_graph_without_views_answers_as_it_did(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location", PLAIN)
    found = ok(run_cli, "get", shelf, "--affordances", env=PLAIN).data["affordances"]
    assert "views" not in found and "views_more" not in found
    card = ok(run_cli, "view", shelf, env=PLAIN).data
    dump = json.dumps(card)
    assert '"views' not in dump
