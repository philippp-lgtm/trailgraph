"""S: ``graph do`` under a write allow list (write allow lists plan W4, W9) over the company
example.

The action listing and the run decide on concrete types: a parameter of an abstract class is met
by an allowed subtype, an optional parameter's entry counts as written, and an action stays
hidden when no argument could pass. A listed action can still be refused for an entity of a type
the caller may not write; the refusal names the resource, also when the edge to remove is
found by the expansion of ``all: true``.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.actions import VALID
from support.admin import Outcome, admin, init
from support.app import app
from support.fixtures import EXAMPLES, example_model
from support.policies import write_policy
from support.services import OpsFactory

ONTOLOGY = EXAMPLES / "company" / "ontology" / "1.1.0"
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
FACTORY = OpsFactory(example_model("company", "1.1.0"))

WRITES = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      ppl:Employee: {link: [org:holds], unlink: [org:holds]}
      ppl:External: {unlink: [ppl:sponsored_by]}
      ops:Task: {create: [title, due_on], link: [ops:assigned_to]}
no_project: {"*": write}
"""
OTHER_TYPE = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      org:Position: {update: [title]}
no_project: {"*": write}
"""
EVERYTHING = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""


def limits(*types: str) -> str:
    listed = "\n".join(f'  "{t}": read' for t in types)
    return f"schema: graph_cli.policy/1\nid: graph\ntypes:\n{listed}\n"


@dataclass
class World:
    graph_id: str
    ids: dict[str, str]
    work: Path

    def resource(self, below: str) -> str:
        return f"graph:{self.graph_id}/{below}"


def apply(run_cli: CliRunner, *ops: dict[str, Any]) -> list[dict[str, Any]]:
    done = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": list(ops)}))
    assert done.code == 0, done.envelope
    entities: list[dict[str, Any]] = done.data["results"][0]["result"]["entities"]
    return entities


def make(run_cli: CliRunner, cls: str, local: str) -> str:
    entities = apply(run_cli, *FACTORY.create(cls, local))
    return str(next(e["id"] for e in entities if e["local"] == local))


def install(run_cli: CliRunner, world: World, text: str, *args: str) -> None:
    path = write_policy(
        world.work, f"policy_{hashlib.sha256(text.encode()).hexdigest()[:12]}.yaml", text
    )
    applied = admin(run_cli, "policy", "apply", str(path), *args, "--reason", "test policy")
    assert applied.code == 0, applied.envelope


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(VALID))
    assert published.code == 0, published.envelope
    ids = {
        "employee": make(run_cli, "ppl:Employee", "e1"),
        "other_employee": make(run_cli, "ppl:Employee", "e2"),
        "external": make(run_cli, "ppl:External", "x"),
        "free": make(run_cli, "org:Position", "p_free"),
        "held_by_employee": make(run_cli, "org:Position", "p_e"),
        "held_by_external": make(run_cli, "org:Position", "p_x"),
    }
    for holder, position in (("employee", "held_by_employee"), ("external", "held_by_external")):
        qualifiers = {"since": "2026-01-01"}
        apply(
            run_cli,
            {
                "op": "link",
                "id": ids[holder],
                "rel": "org:holds",
                "target": ids[position],
                "qualifiers": qualifiers,
            },
        )
    found = World(created.data["id"], ids, tmp_path / "policies")
    install(run_cli, found, WRITES)
    return found


def listed(run_cli: CliRunner) -> list[str]:
    shown = app(run_cli, "describe", "actions", env=ENV)
    return [a["name"] for a in shown.data["actions"]] if shown.code == 0 else []


def assign(run_cli: CliRunner, person: str, position: str, *extra: str) -> Outcome:
    return app(
        run_cli,
        *("do", "assign_position", "--param", f"person={person}"),
        *("--param", f"position={position}", "--param", "since=2026-03-01", *extra),
        env=ENV,
    )


def hand_over(run_cli: CliRunner, position: str, person: str, *extra: str) -> Outcome:
    return app(
        run_cli,
        *("do", "hand_over_position", "--param", f"position={position}"),
        *("--param", f"person={person}", *extra),
        env=ENV,
    )


def refused(outcome: Outcome, action: str, resource: str, rule: str = "default.deny") -> None:
    assert outcome.code != 0 and outcome.error["code"] == "access.denied", outcome.envelope
    details = outcome.error["details"]
    assert (details["action"], details["resource"], details["rule"]) == (action, resource, rule)


# ---- listing -----------------------------------------------------------------------------


def test_an_action_is_listed_when_every_need_has_a_type_that_passes(
    run_cli: CliRunner, world: World
) -> None:
    """``assign_position`` links a ``ppl:Person`` (abstract): the Employee grant meets it. An
    action that also moves an employee's lifecycle needs a right the list does not give."""
    assert listed(run_cli) == ["assign_position", "create_task", "hand_over_position"]


def test_an_optional_parameters_entry_counts_as_written(run_cli: CliRunner, world: World) -> None:
    """``create_task`` writes ``due_on`` when ``due`` is given: a list without it hides the
    action even for a caller who would leave the parameter out."""
    install(run_cli, world, WRITES.replace("[title, due_on]", "[title]"))
    assert "create_task" not in listed(run_cli)
    missing = app(run_cli, "do", "create_task", "--param", "title=T", env=ENV)
    assert missing.error["code"] == "action.not_found"


def test_a_listed_action_runs_with_every_optional_parameter_given(
    run_cli: CliRunner, world: World
) -> None:
    ids = world.ids
    task = app(
        run_cli,
        *("do", "create_task", "--param", "title=Prepare", "--param", "due=2026-04-01"),
        *("--param", f"assignee={ids['employee']}"),
        env=ENV,
    )
    assert task.code == 0, task.envelope
    # the assignee is the edge's target: its type is no business of the writer's rights
    other = app(
        run_cli,
        *("do", "create_task", "--param", "title=Prepare", "--param", "due=2026-04-01"),
        *("--param", f"assignee={ids['external']}"),
        env=ENV,
    )
    assert other.code == 0, other.envelope


def test_an_action_is_hidden_when_no_subtype_of_its_class_passes(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world, OTHER_TYPE)
    assert listed(run_cli) == []
    missing = assign(run_cli, world.ids["employee"], world.ids["free"])
    assert (missing.code, missing.error["code"]) == (1, "action.not_found")


def test_type_limits_on_the_subtypes_decide_a_superclass_need(
    run_cli: CliRunner, world: World
) -> None:
    """Exact type rules hit the subtypes, never the abstract class: with both subtypes
    limited to read, no argument could pass and the action is hidden; with one, it is listed
    and runs for the other."""
    ids = world.ids
    install(run_cli, world, EVERYTHING)
    install(run_cli, world, limits("ppl:Employee"), "--graph", "example_graph")
    assert "assign_position" in listed(run_cli)
    ok = assign(run_cli, ids["external"], ids["free"])
    assert ok.code == 0, ok.envelope
    limited = assign(run_cli, ids["employee"], ids["held_by_employee"])
    refused(limited, "write.link", world.resource("type:ppl:Employee"), "type.ppl:Employee")
    install(run_cli, world, limits("ppl:Employee", "ppl:External"), "--graph", "example_graph")
    names = listed(run_cli)
    assert "assign_position" not in names and "hand_over_position" not in names
    assert "create_task" in names  # tasks are not limited
    hidden = assign(run_cli, ids["external"], ids["free"])
    assert hidden.error["code"] == "action.not_found"


# ---- running -----------------------------------------------------------------------------


def test_a_listed_action_is_refused_for_an_entity_of_a_type_the_caller_may_not_write(
    run_cli: CliRunner, world: World
) -> None:
    ids = world.ids
    ok = assign(run_cli, ids["employee"], ids["free"])
    assert ok.code == 0, ok.envelope
    refused(
        assign(run_cli, ids["external"], ids["free"], "--dry-run"),
        "write.link",
        world.resource("type:ppl:External"),
    )
    refused(
        assign(run_cli, ids["external"], ids["free"]),
        "write.link",
        world.resource("type:ppl:External"),
    )


def test_an_expanded_unlink_is_decided_on_the_owner_type_and_its_relation(
    run_cli: CliRunner, world: World
) -> None:
    """``hand_over_position`` removes the holder with ``all: true``: the expansion unlinks by
    edge id at the owner, so the holder's type and the stored relation decide. An Employee
    holder passes; an External holder may be unlinked on ``ppl:sponsored_by`` only."""
    ids = world.ids
    for dry_run in (True, False):
        extra = ("--dry-run",) if dry_run else ()
        refused(
            hand_over(run_cli, ids["held_by_external"], ids["other_employee"], *extra),
            "write.unlink",
            world.resource("type:ppl:External/rel:org:holds"),
        )
    ok = hand_over(run_cli, ids["held_by_employee"], ids["other_employee"])
    assert ok.code == 0, ok.envelope
    assert admin(run_cli, "audit", "verify").code == 0
