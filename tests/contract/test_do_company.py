"""The company example's actions through ``ActionService.do`` on both backends (actions plan
A4, A5, A6): ``assign_position`` as a plain link behind two preconditions, ``hand_over_position``
and ``offboard_employee`` expanding ``all`` unlinks over zero, one and several edges."""

from pathlib import Path

import pytest
from support.actions import action_service, do, publish_actions, valid_sources
from support.services import ServiceEnv

from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.services.actions import ActionService


@pytest.fixture(params=("company",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root", example_version="1.1.0")
    publish_actions(env, valid_sources())
    return env


@pytest.fixture
def service(svc: ServiceEnv) -> ActionService:
    return action_service(svc)


def make(svc: ServiceEnv, cls: str, local: str) -> str:
    result = svc.create(cls, local)
    return str(next(e.id for e in result.entities if e.local == local))


def hold(svc: ServiceEnv, person: str, position: str) -> None:
    svc.apply({"op": "link", "id": person, "rel": "org:holds", "target": position})


def edges(svc: ServiceEnv, entity: str, rel: str) -> list[str]:
    current = svc.snapshot().get(EntityId(parse_id(entity)))
    assert current is not None
    return sorted(str(e.target) for e in current.edges if e.rel == rel)


def holders(svc: ServiceEnv, position: str) -> list[str]:
    found = svc.snapshot().incoming(EntityId(parse_id(position)), "org:holds")  # type: ignore[arg-type]
    return sorted(str(source) for source, _ in found)


def last_ops(svc: ServiceEnv) -> list[str]:
    return [str(op["op"]) for op in svc.txs()[-1].changeset["ops"]]  # type: ignore[index, union-attr]


def test_assign_position_links_the_person_from_the_date(
    svc: ServiceEnv, service: ActionService
) -> None:
    person, position = make(svc, "ppl:Employee", "p"), make(svc, "org:Position", "pos")
    head = svc.head_seq()
    result = do(
        service,
        svc,
        "assign_position",
        f"person={person}",
        f"position={position}",
        "since=2026-03-01",
    )
    assert result.write.seq == head + 1 and svc.head_seq() == head + 1
    assert holders(svc, position) == [person]
    current = svc.snapshot().get(EntityId(parse_id(person)))
    assert current is not None
    (edge,) = [e for e in current.edges if e.rel == "org:holds"]
    assert edge.qualifiers == {"since": "2026-03-01"}
    intent = svc.graph_events()[-1]
    assert intent["request"]["action"]["name"] == "assign_position"  # type: ignore[index]
    after = [e for e in svc.graph_events() if e["request"]["command"] == "do"]  # type: ignore[index]
    assert len(after) == 1  # one intent for the whole action


def test_assign_position_refuses_a_held_position_with_the_precondition_named(
    svc: ServiceEnv, service: ActionService
) -> None:
    first, second = make(svc, "ppl:Employee", "a"), make(svc, "ppl:Employee", "b")
    position = make(svc, "org:Position", "pos")
    hold(svc, first, position)
    head = svc.head_seq()
    with pytest.raises(GraphCliError) as caught:
        do(
            service,
            svc,
            "assign_position",
            f"person={second}",
            f"position={position}",
            "since=2026-03-01",
        )
    error = caught.value
    assert error.code == "action.precondition_failed"
    (failed,) = error.details["failed"]  # type: ignore[misc]
    assert failed["precondition"] == "position_vacant" and failed["entity"] == position  # type: ignore[index]
    assert failed["actual"]  # type: ignore[index]
    assert svc.head_seq() == head and holders(svc, position) == [first]
    refusal = svc.graph_events()[-1]
    assert refusal["result"]["status"] == "invalid"  # type: ignore[index]
    assert refusal["request"]["action"]["name"] == "assign_position"  # type: ignore[index]
    assert refusal["target"]["entities"] == [position]  # type: ignore[index]
    assert not [f for f in svc.verify().findings if f.severity.value == "error"]


@pytest.mark.parametrize("count", [1, 3])
def test_hand_over_removes_every_current_holder_and_links_the_new_one(
    svc: ServiceEnv, service: ActionService, count: int
) -> None:
    position, newcomer = make(svc, "org:Position", "pos"), make(svc, "ppl:Employee", "new")
    old = [make(svc, "ppl:Employee", f"old{n}") for n in range(count)]
    for person in old:
        hold(svc, person, position)  # `in` cardinality is not checked on writes
    dry = do(
        service,
        svc,
        "hand_over_position",
        f"position={position}",
        f"person={newcomer}",
        dry_run=True,
    )
    shown = {str(e.id): e.ops for e in dry.write.entities}
    assert {person: shown[person] for person in old} == {person: ("unlink",) for person in old}
    assert shown[newcomer] == ("link",)
    assert holders(svc, position) == sorted(old)  # a dry run writes nothing
    do(
        service,
        svc,
        "hand_over_position",
        f"position={position}",
        f"person={newcomer}",
        "since=2026-04-01",
    )
    assert holders(svc, position) == [newcomer]
    assert last_ops(svc) == ["unlink"] * count + ["link"]


def test_hand_over_of_a_vacant_position_is_refused_by_its_precondition(
    svc: ServiceEnv, service: ActionService
) -> None:
    position, newcomer = make(svc, "org:Position", "pos"), make(svc, "ppl:Employee", "new")
    with pytest.raises(GraphCliError) as caught:
        do(service, svc, "hand_over_position", f"position={position}", f"person={newcomer}")
    assert caught.value.code == "action.precondition_failed"
    assert holders(svc, position) == []


def test_hand_over_leaves_out_the_date_the_caller_left_out(
    svc: ServiceEnv, service: ActionService
) -> None:
    position, newcomer = make(svc, "org:Position", "pos"), make(svc, "ppl:Employee", "new")
    hold(svc, make(svc, "ppl:Employee", "old"), position)
    do(service, svc, "hand_over_position", f"position={position}", f"person={newcomer}")
    current = svc.snapshot().get(EntityId(parse_id(newcomer)))
    assert current is not None
    (edge,) = [e for e in current.edges if e.rel == "org:holds"]
    assert edge.qualifiers == {}


@pytest.mark.parametrize("positions", [0, 1, 3])
def test_offboard_removes_what_there_is_and_moves_the_employee_to_left(
    svc: ServiceEnv, service: ActionService, positions: int
) -> None:
    employee = make(svc, "ppl:Employee", "e")
    owned = [make(svc, "org:Position", f"pos{n}") for n in range(positions)]
    for position in owned:
        hold(svc, employee, position)
    created = svc.apply(
        {
            "op": "create",
            "type": "leg:LegalEntity",
            "local": "le",
            "attrs": {
                "name": "Fictional Works AG",
                "country": "CH",
                "legal_form": "AG",
                "in_group": True,
                "registration_number": "FICT-0001",
            },
        }
    )
    employer = str(created.entities[0].id)
    svc.apply({"op": "link", "id": employee, "rel": "leg:employed_by", "target": employer})
    with pytest.raises(GraphCliError) as caught:  # the action asks for a reason
        do(service, svc, "offboard_employee", f"employee={employee}")
    assert caught.value.code == "action.reason_required"
    do(service, svc, "offboard_employee", f"employee={employee}", reason="left the company")
    assert last_ops(svc) == ["unlink"] * (positions + 1) + ["transition"]
    assert edges(svc, employee, "org:holds") == [] and edges(svc, employee, "leg:employed_by") == []
    current = svc.snapshot().get(EntityId(parse_id(employee)))
    assert current is not None and current.attrs["status"] == "left"
    assert svc.txs()[-1].reason == "left the company"
    with pytest.raises(GraphCliError) as caught:  # now the precondition is false
        do(service, svc, "offboard_employee", f"employee={employee}", reason="again")
    assert caught.value.code == "action.precondition_failed"


def test_offboard_without_any_edge_still_writes_the_transition(
    svc: ServiceEnv, service: ActionService
) -> None:
    employee = make(svc, "ppl:Employee", "e")
    do(service, svc, "offboard_employee", f"employee={employee}", reason="left")
    assert last_ops(svc) == ["transition"]


class Source:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def test_a_type_the_actor_may_not_unlink_refuses_the_whole_action(svc: ServiceEnv) -> None:
    """The action is visible (its needs name ``ppl:Person``) and the holder is an
    ``ppl:External``, whose unlink the policy denies: nothing is written, the denial is audited."""
    position, newcomer = make(svc, "org:Position", "pos"), make(svc, "ppl:Employee", "new")
    holder = make(svc, "ppl:External", "ext")
    hold(svc, holder, position)
    deny: dict[str, JsonValue] = {
        "id": "no_unlink_external",
        "effect": "deny",
        "action": ["write.unlink"],
        "resource": f"graph:{svc.entry.id}/type:ppl:External",
    }
    document = {**BUILTIN_POLICY, "rules": [*BUILTIN_POLICY["rules"], deny]}  # type: ignore[misc]
    service = action_service(svc, Source(policy_set(document)))
    head = svc.head_seq()
    for dry_run in (True, False):
        with pytest.raises(GraphCliError) as caught:
            do(
                service,
                svc,
                "hand_over_position",
                f"position={position}",
                f"person={newcomer}",
                dry_run=dry_run,
            )
        assert caught.value.code == "access.denied"
    assert svc.head_seq() == head and holders(svc, position) == [holder]
    denial = svc.installation_events()[-1]
    assert denial["result"] == {"status": "denied", "code": "access.denied"}
    assert denial["request"]["action"]["name"] == "hand_over_position"  # type: ignore[index]
    assert len([e for e in svc.installation_events() if e["result"]["status"] == "denied"]) == 1  # type: ignore[index]
