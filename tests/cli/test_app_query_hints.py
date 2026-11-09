"""S: the hint on a `query` answer that a published function covers (usage shapes plan U4,
U5): wording for a single and a list parameter, order before the paging hint, at most two
hints, nothing for an uncovered query, an error or a caller who may not read functions, and
a hint command that runs."""

import shlex
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph
from support.fixtures import FIXTURES
from support.policies import write_policy

SINGLE = FIXTURES / "functions" / "inventory" / "valid" / "items_in_location.yaml"
MANY = """\
schema: graph_cli.function/1
name: items_in_locations
summary: Items stored in any of several locations
use_when: What is stored in these places; items sitting in one of several locations.
params:
  locations:
    type: ref
    class: inv:Location
    many: true
    summary: The locations to list.
query: |
  MATCH (i:Item)-[:located_in]->(l:Location)
  WHERE l.id IN $locations
  RETURN i.name AS item, i.quantity AS quantity
  ORDER BY item
returns:
  - column: item
    summary: Name of the item.
  - column: quantity
    summary: Number of units.
examples:
  - question: What is in these rooms?
featured: true
"""
COVERED = "MATCH (i:Item)-[:located_in]->(l:Location) WHERE l.id = '{id}' RETURN i.name AS item"
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
NO_EXPLORE_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
"""


@pytest.fixture
def location(run_cli: CliRunner, isolated_env: dict[str, str]) -> str:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    place = app(
        run_cli, "create", "inv:Location", "--set", "name=Hall", "--set", "code=R1", env=ENV
    )
    assert place.code == 0, place.envelope
    place_id: str = place.data["entities"][0]["id"]
    for name in ("Anvil", "Crate"):
        item = app(
            run_cli,
            "create",
            "inv:Item",
            "--set",
            f"name={name}",
            "--edge",
            f"inv:located_in={place_id}",
            env=ENV,
        )
        assert item.code == 0, item.envelope
    return place_id


def publish(run_cli: CliRunner, tmp_path: Path, *, single: bool, many: bool) -> None:
    folder = tmp_path / "functions"
    folder.mkdir()
    if single:
        (folder / SINGLE.name).write_text(SINGLE.read_text())
    if many:
        (folder / "items_in_locations.yaml").write_text(MANY)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


def hints(envelope: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = envelope.get("hints", [])
    return found


def test_a_covered_query_hints_the_function(
    run_cli: CliRunner, location: str, tmp_path: Path
) -> None:
    publish(run_cli, tmp_path, single=True, many=False)
    plain = app(run_cli, "query", COVERED.format(id=location), env=ENV)
    assert plain.code == 0, plain.envelope
    (hint,) = hints(plain.envelope)
    assert hint["text"] == (
        "Function items_in_location covers this query: Items stored in a location. "
        "It takes one inv:Location per run."
    )
    assert shlex.split(hint["command"]) == [
        "graph", "run", "items_in_location", "--graph", "example_graph",
        "--param", "location=<ref>",
    ]  # fmt: skip

    # the hint command runs once the placeholder is a real reference
    command = shlex.split(hint["command"].replace("'location=<ref>'", f"location={location}"))
    ran = app(run_cli, *command[1:], env=ENV)
    assert ran.code == 0, ran.envelope
    assert [i["item"] for i in ran.data["items"]] == ["Anvil", "Crate"]


def test_a_list_parameter_says_to_repeat_it(
    run_cli: CliRunner, location: str, tmp_path: Path
) -> None:
    publish(run_cli, tmp_path, single=False, many=True)
    found = app(run_cli, "query", COVERED.format(id=location), env=ENV)
    (hint,) = hints(found.envelope)
    assert hint["text"] == (
        "Function items_in_locations covers this query: Items stored in any of several "
        "locations. It takes a list: repeat --param locations."
    )
    assert "locations=<ref>" in hint["command"]
    command = shlex.split(hint["command"].replace("'locations=<ref>'", f"locations={location}"))
    assert app(run_cli, *command[1:], env=ENV).code == 0


def test_the_data_does_not_change_and_at_most_two_hints_come_before_paging(
    run_cli: CliRunner, location: str, tmp_path: Path
) -> None:
    before = app(run_cli, "query", COVERED.format(id=location), "--limit", "1", env=ENV)
    assert hints(before.envelope)[0]["text"] == "More results available"
    publish(run_cli, tmp_path, single=True, many=True)
    after = app(run_cli, "query", COVERED.format(id=location), "--limit", "1", env=ENV)
    found = hints(after.envelope)
    assert len(found) == 3 and found[-1] == hints(before.envelope)[0]
    assert {h["text"].split(" covers")[0] for h in found[:2]} == {
        "Function items_in_location",
        "Function items_in_locations",
    }
    assert after.data == before.data and after.envelope["page"] == before.envelope["page"]
    assert {k: v for k, v in after.envelope.items() if k != "hints"} == {
        k: v for k, v in before.envelope.items() if k != "hints"
    }


def test_an_uncovered_query_and_an_error_have_no_cover_hint(
    run_cli: CliRunner, location: str, tmp_path: Path
) -> None:
    publish(run_cli, tmp_path, single=True, many=False)
    other = app(run_cli, "query", "MATCH (l:Location) RETURN l.code AS code", env=ENV)
    assert other.code == 0 and hints(other.envelope) == []
    broken = app(run_cli, "query", "MATCH (n RETURN n", env=ENV)
    assert broken.code != 0 and not broken.envelope.get("hints")


def test_explain_shows_the_hint_and_run_does_not(
    run_cli: CliRunner, location: str, tmp_path: Path
) -> None:
    publish(run_cli, tmp_path, single=True, many=False)
    explained = app(run_cli, "query", COVERED.format(id=location), "--explain", env=ENV)
    assert explained.code == 0 and "plan" in explained.data
    (hint,) = hints(explained.envelope)
    assert hint["text"].startswith("Function items_in_location covers this query")
    ran = app(run_cli, "run", "items_in_location", "--param", f"location={location}", env=ENV)
    assert ran.code == 0 and hints(ran.envelope) == []


def test_the_hint_shows_in_text_mode(run_cli: CliRunner, location: str, tmp_path: Path) -> None:
    publish(run_cli, tmp_path, single=True, many=False)
    shown = run_cli("graph", "query", COVERED.format(id=location), env=ENV)
    assert shown.returncode == 0, shown.stderr
    assert "Function items_in_location covers this query" in shown.stdout + shown.stderr


def test_a_caller_without_read_function_gets_no_hint(
    run_cli: CliRunner, location: str, tmp_path: Path
) -> None:
    publish(run_cli, tmp_path, single=True, many=False)
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE_FUNCTIONS)
    assert admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy").code == 0
    text = COVERED.format(id=location)
    hidden = app(run_cli, "query", text, env=EXPLORE)
    assert hidden.code == 0 and hints(hidden.envelope) == []
    allowed = app(run_cli, "query", text, env=ENV)
    assert len(hints(allowed.envelope)) == 1
    assert hidden.data == allowed.data
