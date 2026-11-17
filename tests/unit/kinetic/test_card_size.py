"""The card stays below its byte and work limits however big the entity is (T17, OV8, OV9)."""

import json
from dataclasses import replace
from typing import Any

from support.actions import company_model, valid_sources
from support.cards import CountingSnapshot, as_snapshot, with_attribute
from support.fixtures import compiled_model
from support.functions import valid_sources as function_sources
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.kinetic.affordances import actions_for, functions_for
from graph_cli.kinetic.card import BYTE_LIMIT, dumps, generated_spec, render
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import load_entries
from graph_cli.ontology_layer.affordances import relations, transitions
from graph_cli.ontology_layer.model import CompiledModel

NOTES = compiled_model("notes_and_sources", "1.2.0")
LONG_LABEL = "L" * 300


def bigger_note_model(out: int, into: int, prefix: str = "nas:r") -> CompiledModel:
    """The notes model with a Note of 40 attributes (10 required, two lifecycles), ``out`` relations
    from a Note to a Topic and ``into`` relations from a Topic to a Note."""
    model = NOTES
    for n in range(37):
        model = with_attribute(
            model, "nas:Note", f"attr_{n:02d}", "body", required=n < 10, unique=False
        )
    model = with_attribute(model, "nas:Note", "phase", "status")
    about = NOTES.relations["nas:about"]
    relations_ = dict(NOTES.relations)
    for n in range(out):
        relations_[f"{prefix}out_{n:02d}"] = about
    for n in range(into):
        relations_[f"{prefix}in_{n:02d}"] = replace(
            about, domain=("nas:Topic",), range=("nas:Note",)
        )
    return replace(model, relations=relations_)


def big_entity(
    out: int = 30, into: int = 10, per: int = 200, files: int = 200, prefix: str = "nas:r"
) -> tuple[Revision, list[Revision]]:
    values: dict[str, JsonValue] = {
        "title": LONG_LABEL,
        "body": "b" * 2000,
        "kind": "observation",
        "status": "draft",
        "phase": "draft",
    }
    values.update({f"attr_{n:02d}": "v" * 2000 for n in range(37)})
    edges: list[EdgeRecord] = []
    others: list[Revision] = []
    for r in range(out):
        for i in range(per):
            name = f"o{r}_{i}"
            edges.append(edge(f"{prefix}out_{r:02d}", name))
            others.append(revision(name, "nas:Topic", {"label": LONG_LABEL + name}))
    for f in range(files):
        name = f"f{f}"
        edges.append(edge("files:from_file", name, page=f + 1))
        scan: dict[str, JsonValue] = {
            "title": LONG_LABEL + name,
            "content": f"sha256:{f}",
            "media_type": "application/pdf",
        }
        others.append(revision(name, "nas:Scan", scan))
    entity = revision("big", "nas:Note", values, edges)
    for r in range(into):
        for i in range(per):
            name = f"i{r}_{i}"
            others.append(
                revision(
                    name,
                    "nas:Topic",
                    {"label": LONG_LABEL + name},
                    [edge(f"{prefix}in_{r:02d}", "big")],
                )
            )
    return entity, others


def affordances_of_many() -> dict[str, JsonValue]:
    long = "a_long_name_of_a_synthetic_entry"
    return {
        "record_status": "active",
        "transitions": [
            {
                "attr": attr,
                "lifecycle": "nas:NoteLifecycle",
                "from": "draft",
                "to": [
                    {
                        "state": "published",
                        "allowed": False,
                        "blocked_by": [{"guard": long, "summary": "x" * 200}],
                    },
                    {"state": "archived", "allowed": True},
                ],
            }
            for attr in ("status", "phase")
        ],
        "actions": [
            {
                "action": f"{long}_{n:03d}",
                "as": "note",
                "applies": n % 2 == 0,
                "blocked_by": [],
                "unchecked": [],
            }
            for n in range(40)
        ],
        "actions_more": 20,
        "functions": [{"function": f"{long}_{n:03d}", "as": "note"} for n in range(40)],
        "functions_more": 20,
    }


def test_a_huge_entity_gives_a_card_below_the_limit_with_exact_counts() -> None:
    model = bigger_note_model(30, 10)
    entity, others = big_entity()
    spec = generated_spec(model, "nas:Note")
    card = render(
        spec, entity, DictSnapshot(entity, *others), model, affordances=affordances_of_many()
    )
    data: Any = card.to_json()
    assert len(dumps(data)) < BYTE_LIMIT
    assert data["bytes"] == len(dumps(data)) and data["cut"] is True
    sections = {(s["kind"], s.get("variant")): s for s in data["sections"]}

    rel = sections[("relations", None)]
    shown = {(g["relation"], g["direction"]) for g in rel["groups"]}
    assert shown and all(len(g["items"]) >= 1 for g in rel["groups"])
    for g in rel["groups"]:
        assert g["count"] == 200 and g["count"] == len(g["items"]) + g.get("more", 0)
    assert len(shown) + len(rel["more_groups"]) == 40  # every relation with edges, none lost
    assert set(rel["more_groups"]).isdisjoint({r for r, _ in shown})

    fields = sections[("fields", None)]
    valued = {"title", "body", "kind", "status", "phase", *(f"attr_{n:02d}" for n in range(37))}
    accounted = [i["name"] for i in fields["items"]] + fields.get("other", [])
    assert len(accounted) + fields.get("other_more", 0) == len(valued)
    assert set(accounted) <= valued
    assert {"title", "status", "phase"} <= {i["name"] for i in fields["items"]}  # never trimmed

    files = sections[("files", None)]
    assert len(files["items"]) + files.get("more", 0) == 200 and "sha256" not in str(files)

    aff = sections[("affordances", None)]
    assert aff["actions"] == {"apply": aff["actions"]["apply"], "blocked": 20}
    assert len(aff["actions"]["apply"]) == 5 and aff["actions_more"] == 15 + 20
    assert len(aff["functions"]) == 5 and aff["functions_more"] == 35 + 20
    assert len(aff["transitions"]) == 2  # the summary is never trimmed


def test_the_card_of_an_ordinary_entity_is_not_cut() -> None:
    model = bigger_note_model(3, 2)
    entity, others = big_entity(3, 2, 4, 2)
    entity = replace(
        entity,
        attrs={"title": "A note", "body": "Text.", "kind": "observation", "status": "draft"},
    )
    card = render(generated_spec(model, "nas:Note"), entity, DictSnapshot(entity, *others), model)
    data: Any = card.to_json()
    assert "cut" not in data and data["bytes"] < 3 * 1024


def test_the_card_of_a_typical_company_entity_stays_below_three_kib() -> None:
    model = company_model()
    actions = load_action_entries(compile_actions(valid_sources(), model)[0])
    functions = load_entries(compile_catalog(function_sources(), model)[0])
    held = revision(
        "lead", "org:Position", {"title": "Site Reliability Engineer", "status": "active"}
    )
    unit = revision("ops", "org:Team", {"name": "Reliability"}, alias="TEAM-0004")
    people = [
        revision(
            f"p{n}",
            "ppl:Employee",
            {"name": f"Person {n}", "status": "active", "employee_number": f"E-{n:03d}"},
            [edge("org:holds", "lead", since="2021-09-01")],
            alias=f"EMP-{n:04d}",
        )
        for n in range(2)
    ]
    entity = revision(
        "lead",
        "org:Position",
        {"title": "Site Reliability Engineer", "status": "active", "alt_labels": ["SRE"]},
        [edge("org:in_unit", "ops"), edge("org:carries", "role")],
        alias="POS-0013",
    )
    role = revision("role", "org:Role", {"name": "Staff member"}, alias="ROLE-0001")
    snapshot = DictSnapshot(entity, unit, role, held, *people)
    found: dict[str, JsonValue] = {"record_status": "active"}
    found.update(transitions(model, entity, snapshot).to_json())
    found.update(relations(model, entity, snapshot).to_json())
    found.update(actions_for(entity, list(actions), snapshot, model).to_json())
    found.update(functions_for(entity, list(functions), model).to_json())
    card = render(generated_spec(model, "org:Position"), entity, snapshot, model, affordances=found)
    data: Any = card.to_json()
    assert "cut" not in data
    assert data["bytes"] < 3 * 1024, json.dumps(data, indent=1)


def test_adversarial_ids_and_twenty_groups_are_pinned_by_the_guard() -> None:
    model = bigger_note_model(20, 0, prefix="nas:a_much_longer_relation_name_")
    entity, others = big_entity(20, 0, 3, 0, prefix="nas:a_much_longer_relation_name_")
    card = render(
        generated_spec(model, "nas:Note"),
        entity,
        DictSnapshot(entity, *others),
        model,
        affordances=affordances_of_many(),
    )
    data: Any = card.to_json()
    assert len(dumps(data)) < BYTE_LIMIT and data["cut"] is True
    rel = next(s for s in data["sections"] if s["kind"] == "relations")
    assert len(rel["groups"]) + len(rel.get("more_groups", [])) == 20
    # the same input gives the same bytes
    again = render(
        generated_spec(model, "nas:Note"),
        entity,
        DictSnapshot(entity, *reversed(others)),
        model,
        affordances=affordances_of_many(),
    )
    assert dumps(again.to_json()) == dumps(data)


def test_loads_stay_at_or_below_nine_hundred_for_the_huge_entity() -> None:
    model = bigger_note_model(30, 10)
    entity, others = big_entity()
    counting = CountingSnapshot(DictSnapshot(entity, *others))
    render(generated_spec(model, "nas:Note"), entity, as_snapshot(counting), model)
    assert len(counting.loaded) <= 900
    assert len(counting.loaded) >= 100  # the shown groups are really read


def test_a_hub_with_twenty_thousand_in_edges_loads_at_most_one_hundred_revisions() -> None:
    hub = revision("hub", "nas:Topic", {"label": "Hub"})
    notes = [
        revision(
            f"n{i}",
            "nas:Note",
            {"title": f"Note {i:05d}", "body": "t", "kind": "observation", "status": "draft"},
            [edge("nas:about", "hub")],
        )
        for i in range(20000)
    ]
    counting = CountingSnapshot(DictSnapshot(hub, *notes))
    card = render(generated_spec(NOTES, "nas:Topic"), hub, as_snapshot(counting), NOTES)
    assert len(counting.loaded) <= 100
    data: Any = card.to_json()
    group = next(s for s in data["sections"] if s["kind"] == "relations")["groups"][0]
    assert group["count"] == 20000 and group["approx"] is True and group["sampled"] == 100
