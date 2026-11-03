"""Transitions and relation room of one entity on the example company model (T5)."""

from collections.abc import Sequence
from dataclasses import replace
from typing import get_args

from support.actions import company_model
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.records import EdgeRecord, RecordStatus, Revision
from graph_cli.ontology_layer.affordances import (
    MAX_ENTRIES,
    RelationAffordances,
    relations,
    transitions,
)

MODEL = company_model()


def position(
    name: str,
    state: str = "active",
    edges: Sequence[EdgeRecord] = (),
    record: RecordStatus = "active",
) -> Revision:
    attrs = {"title": name.title(), "status": state}
    return revision(name, "org:Position", attrs, edges, status=record)


def holder(name: str, *positions: str, record: RecordStatus = "active") -> Revision:
    edges = [edge("org:holds", p, name=f"{name}>{p}") for p in positions]
    attrs = {"name": name, "status": "active"}
    return revision(name, "ppl:Employee", attrs, edges, status=record)


def moves(entity: Revision, *others: Revision) -> dict[str, object]:
    found = transitions(MODEL, entity, DictSnapshot(entity, *others)).to_json()
    return {"transitions": found["transitions"]}


def room(found: RelationAffordances, relation: str, direction: str) -> dict[str, object]:
    (entry,) = [
        e.to_json() for e in found.entries if (e.relation, e.direction) == (relation, direction)
    ]
    return entry


def test_the_initial_state_lists_its_outgoing_transitions() -> None:
    entity = position("lead", "planned")
    assert moves(entity) == {
        "transitions": [
            {
                "attr": "status",
                "lifecycle": "org:PositionLifecycle",
                "from": "planned",
                "to": [
                    {"state": "active", "allowed": True},
                    {"state": "abolished", "allowed": True},
                ],
            }
        ]
    }


def test_a_middle_state_has_only_its_own_transitions() -> None:
    (entry,) = transitions(MODEL, position("lead", "active"), DictSnapshot()).entries
    assert [t.state for t in entry.to] == ["abolished"]


def test_a_terminal_state_has_no_transitions() -> None:
    (entry,) = transitions(MODEL, position("lead", "abolished"), DictSnapshot()).entries
    assert entry.from_state == "abolished" and entry.to == ()
    assert entry.to_json()["to"] == []


def test_a_failing_guard_blocks_the_transition_and_names_it() -> None:
    entity = position("lead", "active")
    (entry,) = transitions(MODEL, entity, DictSnapshot(entity, holder("mara", "lead"))).entries
    (target,) = entry.to
    assert target.allowed is False
    assert target.to_json() == {
        "state": "abolished",
        "allowed": False,
        "blocked_by": [{"guard": "org.not_held", "summary": target.blocked_by[0].summary}],
    }
    assert target.blocked_by[0].summary


def test_a_guard_that_holds_again_allows_the_transition() -> None:
    entity = position("lead", "active")
    gone = holder("mara", "lead", record="superseded")
    (entry,) = transitions(MODEL, entity, DictSnapshot(entity, gone)).entries
    assert entry.to[0].allowed is True


def test_an_inherited_lifecycle_attribute_is_listed() -> None:
    entity = holder("mara")
    (entry,) = transitions(MODEL, entity, DictSnapshot(entity)).entries
    assert (entry.attr, entry.lifecycle) == ("status", "ppl:EmploymentLifecycle")
    assert [t.state for t in entry.to] == ["left"]


def test_a_class_without_a_lifecycle_has_no_transitions() -> None:
    entity = revision("hq", "org:OrgUnit", {"name": "HQ"})
    assert transitions(MODEL, entity, DictSnapshot(entity)).to_json() == {"transitions": []}


def test_a_superseded_or_retracted_entity_has_empty_groups() -> None:
    for record in get_args(RecordStatus)[1:]:
        entity = position("lead", record=record)
        assert transitions(MODEL, entity, DictSnapshot(entity)).entries == ()
        found = relations(MODEL, entity, DictSnapshot(entity))
        assert found.to_json() == {"relations": [], "full": []}


def test_a_relation_with_room_reports_count_bounds_and_room() -> None:
    entity = position("lead")
    found = relations(MODEL, entity, DictSnapshot(entity))
    assert room(found, "org:holds", "in") == {
        "relation": "org:holds",
        "direction": "in",
        "count": 0,
        "min": 0,
        "max": 1,
        "room": 1,
    }


def test_an_unbounded_relation_has_no_room_limit() -> None:
    entity = position("lead")
    entry = room(relations(MODEL, entity, DictSnapshot(entity)), "org:carries", "out")
    assert (entry["max"], entry["room"]) == (None, None)


def test_a_relation_at_its_maximum_is_named_not_listed() -> None:
    entity = position("lead")
    found = relations(MODEL, entity, DictSnapshot(entity, holder("mara", "lead")))
    assert "org:holds" in found.full
    assert all(e.relation != "org:holds" for e in found.entries)


def test_missing_edges_are_counted_when_below_the_minimum() -> None:
    entity = position("lead")
    entry = room(relations(MODEL, entity, DictSnapshot(entity)), "org:in_unit", "out")
    assert (entry["count"], entry["min"], entry["room"], entry["missing"]) == (0, 1, 1, 1)
    linked = position("lead", edges=[edge("org:in_unit", "hq")])
    found = relations(MODEL, linked, DictSnapshot(linked))
    assert "org:in_unit" in found.full  # max 1 reached: no room, nothing missing


def test_only_active_incoming_edges_count() -> None:
    entity = position("lead")
    old = holder("jon", "lead", record="superseded")
    found = relations(MODEL, entity, DictSnapshot(entity, old))
    assert room(found, "org:holds", "in")["count"] == 0


def test_the_domain_of_an_ancestor_covers_the_class() -> None:
    entity = holder("mara", "lead")
    found = relations(MODEL, entity, DictSnapshot(entity, position("lead")))
    entry = room(found, "org:holds", "out")  # declared for ppl:Person, the entity is an Employee
    assert (entry["count"], entry["room"]) == (1, None)
    assert room(found, "leg:employed_by", "out")["max"] == 1


def test_the_range_of_an_ancestor_covers_the_class() -> None:
    entity = holder("mara")
    found = relations(MODEL, entity, DictSnapshot(entity))
    assert room(found, "ppl:sponsored_by", "in")["count"] == 0  # range ppl:Employee


def test_relations_not_covering_the_class_and_core_relations_are_absent() -> None:
    entity = position("lead")
    found = relations(MODEL, entity, DictSnapshot(entity))
    names = {e.relation for e in found.entries} | set(found.full)
    assert "leg:employed_by" not in names
    assert not any(n.startswith("core:") for n in names)


def test_the_order_is_by_relation_then_direction() -> None:
    entity = position("lead")
    keys = [(e.relation, e.direction) for e in relations(MODEL, entity, DictSnapshot()).entries]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1] != "out"))


def test_groups_are_capped_and_count_the_rest() -> None:
    many = {f"org:rel_{n:03d}": MODEL.relations["org:carries"] for n in range(MAX_ENTRIES + 5)}
    big = replace(MODEL, relations=many)
    entity = position("lead")
    found = relations(big, entity, DictSnapshot(entity))
    assert len(found.entries) == MAX_ENTRIES and found.more == 5
    assert found.to_json()["relations_more"] == 5
    assert "full_more" not in found.to_json()
