"""S: graph get --affordances on the example company model with its action and function
fixtures published (affordances plan T5, step 3): absent flag, the four groups, visibility,
the --rev refusal, historical views and superseded entities."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.actions import VALID as ACTIONS
from support.admin import admin, init
from support.app import app
from support.fixtures import EXAMPLES, example_model
from support.policies import write_policy
from support.services import OpsFactory

ONTOLOGY = EXAMPLES / "company" / "ontology" / "1.1.0"
FUNCTIONS = EXAMPLES / "company" / "functions"  # written against the example ontology
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
FACTORY = OpsFactory(example_model("company", "1.1.0"))
NO_EMPLOYEE_WRITES = """\
schema: graph_cli.policy/1
id: graph
types:
  "ppl:Employee": read
"""


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    for kind, folder in (("actions", ACTIONS), ("functions", FUNCTIONS)):
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    return root


def create(run_cli: CliRunner, cls: str, local: str) -> str:
    ops = FACTORY.create(cls, local)
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    entities = applied.data["results"][0]["result"]["entities"]
    return str(next(e["id"] for e in entities if e["local"] == local))


def affordances(run_cli: CliRunner, ref: str, *options: str) -> dict[str, Any]:
    outcome = app(run_cli, "get", ref, "--affordances", *options, env=ENV)
    assert outcome.code == 0, outcome.envelope
    found: dict[str, Any] = outcome.data["affordances"]
    return found


def test_without_the_flag_there_is_no_affordances_key(run_cli: CliRunner, root: Path) -> None:
    position = create(run_cli, "org:Position", "p")
    plain = app(run_cli, "get", position, env=ENV)
    assert plain.code == 0 and "affordances" not in plain.data
    assert set(plain.data) == {"entity", "current_rev", "superseded_by", "retracted_by"}


def test_a_position_shows_all_four_groups(run_cli: CliRunner, root: Path) -> None:
    position = create(run_cli, "org:Position", "p")
    found = affordances(run_cli, position)
    assert found["record_status"] == "active"
    (lifecycle,) = found["transitions"]
    assert lifecycle["attr"] == "status" and lifecycle["lifecycle"] == "org:PositionLifecycle"
    assert lifecycle["to"] and all("allowed" in t for t in lifecycle["to"])
    holds = next(r for r in found["relations"] if r["relation"] == "org:holds")
    assert (holds["direction"], holds["count"], holds["max"], holds["room"]) == ("in", 0, 1, 1)
    actions = {(a["action"], a["as"]): a for a in found["actions"]}
    assert ("assign_position", "position") in actions
    assert ("hand_over_position", "position") in actions
    assert all(isinstance(a["applies"], bool) for a in actions.values())
    assert {f["function"] for f in found["functions"]} == {"reporting_chain"}


def test_a_function_is_listed_for_a_unit(run_cli: CliRunner, root: Path) -> None:
    unit = create(run_cli, "org:Department", "u")
    found = affordances(run_cli, unit)
    assert {(f["function"], f["as"]) for f in found["functions"]} >= {("members_of_unit", "unit")}
    assert found["actions"] == []


def test_a_relation_at_its_maximum_is_named_full(run_cli: CliRunner, root: Path) -> None:
    person = create(run_cli, "ppl:Employee", "mara")
    position = create(run_cli, "org:Position", "p")
    linked = app(run_cli, "link", person, "org:holds", position, env=ENV)
    assert linked.code == 0, linked.envelope
    found = affordances(run_cli, position)
    assert "org:holds" in found["full"]
    assert all(r["relation"] != "org:holds" for r in found["relations"])


def test_a_hidden_action_is_not_listed(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    employee = create(run_cli, "ppl:Employee", "mara")
    before = {a["action"] for a in affordances(run_cli, employee)["actions"]}
    assert "offboard_employee" in before
    path = write_policy(tmp_path / "work", "policy.yaml", NO_EMPLOYEE_WRITES)
    applied = admin(
        run_cli, "policy", "apply", str(path), "--graph", "example_graph", "--reason", "test"
    )
    assert applied.code == 0, applied.envelope
    found = affordances(run_cli, employee)
    assert {a["action"] for a in found["actions"]} == before - {"offboard_employee"}
    assert found["transitions"]  # model-level, not filtered by write rights


def test_rev_is_refused_with_the_flag(run_cli: CliRunner, root: Path) -> None:
    position = create(run_cli, "org:Position", "p")
    refused = app(run_cli, "get", position, "--affordances", "--rev", "1", env=ENV)
    assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument")
    assert refused.error["details"]["argument"] == "--affordances"


def test_as_of_answers_for_the_snapshot_of_that_point(run_cli: CliRunner, root: Path) -> None:
    position = create(run_cli, "org:Position", "p")
    before = app(run_cli, "get", position, env=ENV).envelope["graph"]["seq"]
    person = create(run_cli, "ppl:Employee", "mara")
    assert app(run_cli, "link", person, "org:holds", position, env=ENV).code == 0
    now = affordances(run_cli, position)
    then = affordances(run_cli, position, "--as-of", f"tx:{before}")
    assert "org:holds" in now["full"] and "org:holds" not in then["full"]
    assert any(r["relation"] == "org:holds" for r in then["relations"])


def test_a_superseded_entity_has_empty_groups(run_cli: CliRunner, root: Path) -> None:
    old = create(run_cli, "org:Position", "old")
    new = create(run_cli, "org:Position", "new")
    done = app(run_cli, "supersede", new, old, "--reason", "replaced", env=ENV)
    assert done.code == 0, done.envelope
    outcome = app(run_cli, "get", old, "--affordances", env=ENV)
    assert outcome.warning_codes == ["entity.superseded"]
    assert outcome.data["affordances"] == {
        "record_status": "superseded",
        "transitions": [],
        "relations": [],
        "full": [],
        "actions": [],
        "functions": [],
    }
