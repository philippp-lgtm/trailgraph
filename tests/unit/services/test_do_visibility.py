"""``graph do`` for a caller who does not see every type (type read limits plan V6): the
reference parameters, the preconditions and the expansion of ``unlink ... all: true`` read what the
caller sees, and what it does not see stays as it was.

The company example: positions are held by persons, which are employees or externals. The
caller writes everything but employees are ``none`` to it.
"""

from pathlib import Path
from typing import Any

import pytest
from support.actions import action_service, do, publish_actions, valid_sources
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.policy import PolicySet

GRAPH_NAME = "example_graph"
FULL = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
HIDE_EMPLOYEES = """\
schema: graph_cli.policy/1
id: graph
types:
  "ppl:Employee": none
"""
HIDE_BOTH = """\
schema: graph_cli.policy/1
id: graph
types:
  "ppl:Employee": none
  "ppl:External": none
"""
GHOST = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"


class Fixed:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


class Company:
    def __init__(self, tmp_path: Path) -> None:
        self.env = ServiceEnv("memory", "company", tmp_path / "root", example_version="1.1.0")
        graph = {GRAPH_NAME: self.env.entry.id}
        installation = compile_policy(FULL.encode(), file="i.yaml", graphs=graph).document
        layer = compile_policy(
            HIDE_EMPLOYEES.encode(),
            file="g.yaml",
            graphs=graph,
            graph=self.env.entry.id,
            roles=frozenset(),
        ).document
        policy = Fixed(policy_set(installation, layer))  # type: ignore[arg-type]
        publish_actions(self.env, valid_sources())
        self.service = action_service(self.env, policy)
        self.ids: dict[str, EntityId] = {}

    def made(self, cls: str, local: str) -> EntityId:
        entities = self.env.create(cls, local).entities
        found = next(e.id for e in entities if e.local == local)
        assert found is not None
        self.ids[local] = found
        return found

    def hold(self, person: str, position: str) -> None:
        self.env.apply(
            {
                "op": "link",
                "id": str(self.ids[person]),
                "rel": "org:holds",
                "target": str(self.ids[position]),
                "qualifiers": {"since": "2026-01-01"},
            },
            reason="test",
        )

    def holders(self, position: str) -> list[str]:
        names = {entity: local for local, entity in self.ids.items()}
        found = self.env.snapshot().incoming(self.ids[position], "org:holds")  # type: ignore[arg-type]
        return sorted(names[source] for source, _ in found)

    def hand_over(self, position: str, person: str, **kw: Any) -> Any:
        return do(
            self.service,
            self.env,
            "hand_over_position",
            f"position={self.ids.get(position, position)}",
            f"person={self.ids.get(person, person)}",
            **kw,
        )

    def refused(self, position: str, person: str) -> GraphCliError:
        with pytest.raises(GraphCliError) as caught:
            self.hand_over(position, person)
        return caught.value


@pytest.fixture
def company(tmp_path: Path) -> Company:
    found = Company(tmp_path)
    for cls, local in (
        ("org:Position", "p"),
        ("org:Position", "q"),
        ("ppl:External", "x"),
        ("ppl:External", "y"),
        ("ppl:Employee", "e"),
    ):
        found.made(cls, local)
    return found


def test_unlink_all_in_removes_the_edges_the_caller_sees_and_keeps_the_others(
    company: Company,
) -> None:
    company.hold("x", "p")  # a visible holder
    company.hold("e", "p")  # a holder the caller does not see
    result = company.hand_over("p", "y", reason="test")
    assert company.holders("p") == ["e", "y"]  # the employee's edge stays
    named = {e.id for e in result.write.entities}
    assert company.ids["e"] not in named and company.ids["x"] in named
    assert str(company.ids["e"]) not in str(result.to_json())


def test_a_precondition_counts_what_the_caller_sees(company: Company) -> None:
    company.hold("e", "p")  # held, but by someone the caller does not see
    error = company.refused("p", "y")
    assert error.code == "action.precondition_failed"
    (failed,) = error.details["failed"]
    assert failed["precondition"] == "position_held"
    assert failed["actual"][0]["value"] == 0  # not 1: the aggregate is over the view
    assert str(company.ids["e"]) not in str(error.details)
    assert company.holders("p") == ["e"]


def test_a_reference_parameter_naming_a_hidden_entity_is_a_missing_one(company: Company) -> None:
    for hidden, missing in ((str(company.ids["e"]), GHOST),):
        a = company.refused("p", hidden)
        b = company.refused("p", missing)
        assert a.code == b.code == "entity.not_found"
        assert (a.message.replace(hidden, missing), a.details["id"]) == (b.message, hidden)
    # the class check does not tell what the hidden entity is: an employee is not an org:Position
    wrong = company.refused(str(company.ids["e"]), "y")
    assert wrong.code == "entity.not_found" and "Employee" not in str(wrong.details)
    seen = company.refused(str(company.ids["x"]), "y")  # a visible entity of another class
    assert seen.code == "action.parameter_type" and seen.details["found"] == "ppl:External"


RELEASE = """\
schema: graph_cli.action/1
name: release_position
module: organization
summary: Take the holder of a position away.
use_when: A position should have no holder any more.
params:
  position:
    type: ref
    class: org:Position
    summary: The position.
ops:
  - {op: unlink, id: $position, rel: org:holds, direction: in, all: true}
examples:
  - request: Nobody holds the lead position any more.
"""


def test_an_action_with_a_need_the_caller_cannot_meet_is_an_unknown_action_in_the_write(
    tmp_path: Path,
) -> None:
    """``graph do`` hides such an action; if a policy change lets one reach the write service
    (the listing is taken before the lock), the answer is the one for an action that does not
    exist, never an authorization of nothing."""
    from graph_cli.kernel.changeset import ChangeSet
    from graph_cli.kernel.records import RequestMeta
    from graph_cli.kinetic.action_compile import compile_actions
    from graph_cli.kinetic.action_entries import load_action_entries
    from graph_cli.services.write import ActionPlan, WriteRequest

    env = ServiceEnv("memory", "company", tmp_path / "root", example_version="1.1.0")
    sources = {**valid_sources(), "release_position.yaml": RELEASE}
    publish_actions(env, sources)
    compiled, _ = compile_actions(sources, env.model)
    entry = next(e for e in load_action_entries(compiled) if e.name == "release_position")
    graph = {GRAPH_NAME: env.entry.id}
    document = compile_policy(FULL.encode(), file="i.yaml", graphs=graph).document
    layer = compile_policy(
        HIDE_BOTH.encode(),
        file="g.yaml",
        graphs=graph,
        graph=env.entry.id,
        roles=frozenset(),
    ).document
    service = env.make_service(Fixed(policy_set(document, layer)))  # type: ignore[arg-type]
    position = next(e.id for e in env.create("org:Position", "p").entities if e.local == "p")
    request = WriteRequest(
        actor=env.actor,
        graph=GRAPH_NAME,
        changeset=ChangeSet(ops=(), reason="test", idempotency_key=None),
        dry_run=False,
        request=RequestMeta("do", "x"),
        plan=ActionPlan(entry, {"position": str(position)}),
    )
    head = env.head_seq()
    with pytest.raises(GraphCliError) as caught:
        service.apply(request)
    assert caught.value.code == "action.not_found"
    assert env.head_seq() == head
