"""Authored entity views rendered over the company example (plan V2, V5, V6): fields, relations,
neighbours with where, order and limits, rows of functions, two sections of one kind."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest
from support.cards import CountingSnapshot, as_snapshot
from support.snapshots import DictSnapshot, edge, eid, revision
from support.views import company_model, compiled_entries, one_entry

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import Revision
from graph_cli.kinetic import card
from graph_cli.kinetic.card import BYTE_LIMIT, ENTITY_LOADS, dumps, render, spec_of
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.kinetic.view_rows import RowsResult

MODEL = company_model()
HEAD = (
    "schema: graph_cli.view/1\nname: probe\nmodule: organization\nsummary: A probe.\n"
    "use_when: Testing.\ntakes: org:OrgUnit\n"
)

type Data = dict[str, Any]


def view(sections: str, extra: str = "") -> ViewEntry:
    return one_entry(HEAD + extra + "sections:\n" + sections)


def position(name: str, title: str, state: str, unit: str = "team", **attrs: JsonValue) -> Revision:
    values: dict[str, JsonValue] = {"title": title, "status": state, **attrs}
    return revision(name, "org:Position", values, [edge("org:in_unit", unit)], alias=f"POS-{name}")


def employee(name: str, holds: str, since: str | None = None, **attrs: JsonValue) -> Revision:
    values: dict[str, JsonValue] = {
        "name": f"Person {name}",
        "status": "active",
        "employee_number": f"E-{name}",
        "work_email": f"{name}@example.org",
        **attrs,
    }
    qualifiers: dict[str, JsonValue] = {} if since is None else {"since": since}
    return revision(name, "ppl:Employee", values, [edge("org:holds", holds, **qualifiers)])


def team(**attrs: JsonValue) -> Revision:
    values: dict[str, JsonValue] = {"name": "Reliability", "alt_labels": ["SRE", "Ops"], **attrs}
    return revision(
        "team",
        "org:Team",
        values,
        [edge("org:part_of", "dept"), edge("org:charged_to", "cc")],
        alias="TEAM-1",
    )


def world() -> tuple[Revision, list[Revision]]:
    unit = team()
    others = [
        revision("dept", "org:Department", {"name": "Engineering"}, alias="DEPT-1"),
        revision("cc", "org:CostCenter", {"name": "Platform", "code": "CC-10"}, alias="CC-10"),
        position("p1", "Engineer", "active"),
        position("p2", "Analyst", "active"),
        position("p3", "Architect", "planned"),
        position("p4", "Archivist", "abolished"),
        employee("e1", "p1", "2024-02-01"),
        employee("e2", "p2"),
        revision(
            "pol",
            "ops:Policy",
            {"title": "Security", "status": "in_force"},
            [edge("ops:applies_to", "team")],
            alias="POL-1",
        ),
    ]
    return unit, others


def shown(
    entry: ViewEntry,
    entity: Revision | None = None,
    others: Sequence[Revision] | None = None,
    *,
    params: Mapping[str, Any] | None = None,
    rows: Mapping[str, RowsResult] | None = None,
    **kwargs: Any,
) -> Data:
    if entity is None:
        entity, others = world()
    spec = spec_of(entry, MODEL, params)
    result = render(spec, entity, DictSnapshot(entity, *(others or ())), MODEL, rows=rows, **kwargs)
    return dict(result.to_json())


def section(data: Data, sid: str) -> Data:
    found = [s for s in data["sections"] if s.get("id") == sid]
    assert len(found) == 1, [s.get("id") for s in data["sections"]]
    return dict(found[0])


# fields


def test_the_view_names_itself_with_its_hash_and_sections_carry_ids() -> None:
    entry = compiled_entries()["unit_overview"]
    data = shown(
        entry, rows={"members": RowsResult("members_of_unit", "sha256:f", ("person",), ())}
    )
    assert data["view"] == {"name": "unit_overview", "origin": "authored", "hash": entry.hash}
    assert [(s["kind"], s["id"]) for s in data["sections"]] == [
        ("fields", "fields"),
        ("relations", "relations"),
        ("neighbors", "positions"),
        ("rows", "members"),
        ("affordances", "affordances"),
    ]
    assert section(data, "positions")["title"] == "Positions"
    assert data["entity"]["alias"] == "TEAM-1"
    assert data["bytes"] == len(dumps(data))


def test_listed_fields_show_exactly_those_and_name_no_others() -> None:
    entry = view("  - kind: fields\n    fields: [name]\n")
    fields = section({"sections": shown(entry)["sections"]}, "fields")
    assert fields == {
        "kind": "fields",
        "id": "fields",
        "items": [{"name": "name", "role": "label", "value": "Reliability"}],
    }
    both = view("  - kind: fields\n    fields: [alt_labels, name]\n")
    items = section({"sections": shown(both)["sections"]}, "fields")["items"]
    assert [i["name"] for i in items] == ["name", "alt_labels"]  # the label first, alt labels after
    assert "other" not in section({"sections": shown(both)["sections"]}, "fields")


def test_fields_all_shows_every_attribute_that_has_a_value() -> None:
    entry = one_entry(
        HEAD.replace("org:OrgUnit", "org:Position")
        + "sections:\n  - kind: fields\n    fields: all\n    max: 16\n"
    )
    pos = position("p9", "Engineer", "active", alt_labels=["Eng"])
    items = section(shown(entry, pos, [team()]), "fields")["items"]
    assert [i["name"] for i in items] == ["title", "alt_labels", "status"]
    assert "other" not in section(shown(entry, pos, [team()]), "fields")


def test_fields_all_never_shows_a_blob_and_counts_what_max_cuts() -> None:
    entry = view("  - kind: fields\n    fields: all\n    max: 1\n")
    fields = section(shown(entry), "fields")
    assert [i["name"] for i in fields["items"]] == ["name"]
    assert fields["other"] == ["alt_labels"]


def test_fields_left_out_are_the_generated_choice_of_the_entitys_class() -> None:
    entry = view("  - kind: fields\n")
    unit, others = world()
    derived = section(shown(entry, unit, others), "fields")["items"]
    generated = card.generated_spec(MODEL, "org:Team").sections[0]
    expected = render(
        card.ViewSpec("card", "generated", (generated,)), unit, DictSnapshot(unit, *others), MODEL
    ).to_json()["sections"][0]["items"]  # type: ignore[index, call-overload]
    assert derived == expected


def test_value_chars_cuts_long_strings_with_their_length() -> None:
    entry = view("  - kind: fields\n    fields: [name]\n    value_chars: 5\n")
    unit = team(name="A very long unit name")
    item = section(shown(entry, unit, []), "fields")["items"][0]
    assert item["value"] == "A ver…" and item["chars"] == 21


# relations


def test_a_relation_list_shows_these_relations_in_this_order() -> None:
    entry = view(
        "  - kind: relations\n    relations:\n      - org:charged_to\n"
        "      - {relation: ops:applies_to, direction: in}\n      - org:part_of\n"
    )
    groups = section(shown(entry), "relations")["groups"]
    assert [(g["relation"], g["direction"]) for g in groups] == [
        ("org:charged_to", "out"),
        ("ops:applies_to", "in"),
        ("org:part_of", "out"),
    ]
    assert groups[0]["items"][0]["ref"] == "CC-10"
    assert "more_groups" not in section(shown(entry), "relations")  # the others are not named


def test_a_relation_cap_names_the_listed_relations_that_are_cut() -> None:
    entry = view(
        "  - kind: relations\n    max_groups: 1\n    relations: [org:charged_to, org:part_of]\n"
    )
    data = section(shown(entry), "relations")
    assert [g["relation"] for g in data["groups"]] == ["org:charged_to"]
    assert data["more_groups"] == ["org:part_of"]


def test_a_relation_without_edges_is_not_a_group() -> None:
    entry = view("  - kind: relations\n    relations: [org:charged_to]\n")
    unit = replace(team(), edges=())
    assert [s["kind"] for s in shown(entry, unit, [])["sections"]] == []


def test_only_the_listed_relations_are_read() -> None:
    entry = view("  - kind: relations\n    relations: [org:charged_to]\n")
    unit, others = world()
    counting = CountingSnapshot(DictSnapshot(unit, *others))
    render(spec_of(entry, MODEL), unit, as_snapshot(counting), MODEL)
    assert {str(e) for e in counting.loaded} == {str(eid("cc"))}


# neighbors

POSITIONS = (
    "  - kind: neighbors\n    id: positions\n    relation: org:in_unit\n    direction: in\n"
    "    order: [title]\n    fields: [title, status]\n"
)


def test_neighbours_are_rows_with_their_fields_in_the_order_asked() -> None:
    data = section(shown(view(POSITIONS)), "positions")
    assert data["kind"] == "neighbors" and data["relation"] == "org:in_unit"
    assert data["direction"] == "in" and data["count"] == 4
    assert [r["ref"] for r in data["items"]] == ["POS-p2", "POS-p3", "POS-p4", "POS-p1"]
    assert data["items"][0] == {
        "ref": "POS-p2",
        "label": "Analyst",
        "fields": {"title": "Analyst", "status": "active"},
    }


def test_where_filters_the_rows_and_a_parameter_is_substituted() -> None:
    entry = view(
        POSITIONS.replace("    order", '    where: ["status=$status"]\n    order'),
        "params:\n  status:\n    type: string\n    default: active\n    summary: State.\n",
    )
    active = section(shown(entry, params={"status": "active"}), "positions")
    assert active["count"] == 2 and [r["ref"] for r in active["items"]] == ["POS-p2", "POS-p1"]
    planned = section(shown(entry, params={"status": "planned"}), "positions")
    assert [r["ref"] for r in planned["items"]] == ["POS-p3"] and planned["count"] == 1


def test_a_list_parameter_reaches_in_as_a_list_whatever_a_value_looks_like() -> None:
    entry = view(
        POSITIONS.replace("    order", '    where: ["title:in=$titles"]\n    order'),
        "params:\n  titles:\n    type: string\n    many: true\n    summary: Titles.\n",
    )
    unit, others = world()
    odd = position("p5", "Lead, Platform", "active")
    both = section(
        shown(entry, unit, [*others, odd], params={"titles": ["Lead, Platform", "Analyst"]}),
        "positions",
    )
    assert sorted(r["label"] for r in both["items"]) == ["Analyst", "Lead, Platform"]
    split = section(
        shown(entry, unit, [*others, odd], params={"titles": ["Lead", "Platform"]}), "positions"
    )
    assert split["count"] == 0  # a comma in a value is part of the value


def test_a_section_without_matches_says_so_with_a_count_of_zero() -> None:
    entry = view(POSITIONS.replace("    order", '    where: ["title=Nobody"]\n    order'))
    assert section(shown(entry), "positions") == {
        "kind": "neighbors",
        "id": "positions",
        "relation": "org:in_unit",
        "direction": "in",
        "count": 0,
    }


def test_max_cuts_the_rows_and_counts_the_rest() -> None:
    data = section(shown(view(POSITIONS + "    max: 3\n")), "positions")
    assert len(data["items"]) == 3 and data["count"] == 4 and data["more"] == 1


def test_the_default_order_is_by_name_then_id() -> None:
    entry = view(
        "  - kind: neighbors\n    id: positions\n    relation: org:in_unit\n    direction: in\n"
    )
    unit, others = world()
    data = section(shown(entry, unit, others), "positions")
    assert [r["label"] for r in data["items"]] == ["Analyst", "Architect", "Archivist", "Engineer"]
    reversed_ = section(shown(entry, unit, list(reversed(others))), "positions")
    assert reversed_ == data


def test_descending_order_and_ties_by_id() -> None:
    entry = view(POSITIONS.replace("order: [title]", 'order: ["title:desc"]'))
    refs = [r["ref"] for r in section(shown(entry), "positions")["items"]]
    assert refs == ["POS-p1", "POS-p4", "POS-p3", "POS-p2"]


def test_a_neighbour_that_is_not_active_says_so() -> None:
    unit, others = world()
    others = [replace(o, record_status="retracted") if o.alias == "CC-10" else o for o in others]
    entry = view("  - kind: neighbors\n    id: cost\n    relation: org:charged_to\n")
    row = section(shown(entry, unit, others), "cost")["items"][0]
    assert row["record_status"] == "retracted"


def test_a_row_names_its_neighbours_with_qualifiers_and_fields() -> None:
    entry = view(
        POSITIONS
        + "    relations:\n      - {relation: org:holds, direction: in, fields: [work_email]}\n"
    )
    data = section(shown(entry), "positions")
    held = next(r for r in data["items"] if r["ref"] == "POS-p1")
    assert held["relations"] == [
        {
            "relation": "org:holds",
            "direction": "in",
            "count": 1,
            "items": [
                {
                    "ref": str(eid("e1")),
                    "label": "Person e1",
                    "state": "active",
                    "qualifiers": {"since": "2024-02-01"},
                    "fields": {"work_email": "e1@example.org"},
                }
            ],
        }
    ]
    empty = next(r for r in data["items"] if r["ref"] == "POS-p3")
    assert "relations" not in empty  # a position nobody holds has no group


def test_a_row_relation_takes_where_order_and_max() -> None:
    unit, others = world()
    more = [employee("e3", "p1"), employee("e4", "p1", status="onboarding")]
    entry = view(
        "  - kind: neighbors\n    id: positions\n    relation: org:in_unit\n    direction: in\n"
        '    where: ["title=Engineer"]\n    relations:\n'
        '      - relation: org:holds\n        direction: in\n        where: ["status=active"]\n'
        '        order: ["name:desc"]\n        max: 2\n'
    )
    group = section(shown(entry, unit, [*others, *more]), "positions")["items"][0]["relations"][0]
    assert group["count"] == 2  # e4 is onboarding: the where drops it
    assert [i["label"] for i in group["items"]] == ["Person e3", "Person e1"]


def test_all_relations_of_a_row_are_listed_as_keys_only() -> None:
    entry = view(
        "  - kind: neighbors\n    id: positions\n    relation: org:in_unit\n    direction: in\n"
        '    where: ["title=Engineer"]\n    relations: all\n'
    )
    unit, others = world()
    pos = position("p1", "Engineer", "active")
    pos = replace(pos, edges=(*pos.edges, edge("org:carries", "r1"), edge("org:carries", "r2")))
    roles = [
        revision("r1", "org:Role", {"name": "Reviewer"}, alias="ROLE-1"),
        revision("r2", "org:Role", {"name": "Approver"}, alias="ROLE-2"),
    ]
    rest = [o for o in others if o.alias != "POS-p1"]
    row = section(shown(entry, unit, [pos, *roles, *rest]), "positions")["items"][0]
    assert row["relations"] == [  # every out-relation by name, keys only: no label, no state
        {
            "relation": "org:carries",
            "direction": "out",
            "count": 2,
            "items": [{"ref": "ROLE-2"}, {"ref": "ROLE-1"}],
        },
        {
            "relation": "org:in_unit",
            "direction": "out",
            "count": 1,
            "items": [{"ref": "TEAM-1"}],
        },
    ]


def test_edges_to_unreadable_entities_are_a_reference_only_and_never_match_a_where() -> None:
    unit = replace(team(), edges=(*team().edges, edge("org:charged_to", "gone")))
    entry = view("  - kind: neighbors\n    id: cost\n    relation: org:charged_to\n")
    rows = section(
        shown(entry, unit, [revision("cc", "org:CostCenter", {"name": "P", "code": "C"})]), "cost"
    )
    assert rows["count"] == 2
    assert {"ref": str(eid("gone"))} in rows["items"]
    filtered = view(
        '  - kind: neighbors\n    id: cost\n    relation: org:charged_to\n    where: ["code=C"]\n'
    )
    kept = section(
        shown(filtered, unit, [revision("cc", "org:CostCenter", {"name": "P", "code": "C"})]),
        "cost",
    )
    assert kept["count"] == 1


def test_shown_names_the_rows_and_their_neighbours() -> None:
    entry = view(POSITIONS + "    relations:\n      - {relation: org:holds, direction: in}\n")
    unit, others = world()
    result = render(spec_of(entry, MODEL), unit, DictSnapshot(unit, *others), MODEL)
    named = set(result.shown())
    assert {eid("p1"), eid("p2"), eid("p3"), eid("p4"), eid("e1"), eid("e2")} <= named


# rows


def test_rows_of_a_function_are_columns_and_items() -> None:
    entry = view("  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n")
    result = RowsResult(
        "members_of_unit",
        "sha256:abc",
        ("person", "position"),
        tuple({"person": f"P{i}", "position": "Engineer"} for i in range(12)),
    )
    data = section(shown(entry, rows={"rows": result}), "rows")
    assert data["function"] == {"name": "members_of_unit", "hash": "sha256:abc"}
    assert data["columns"] == ["person", "position"]
    assert len(data["items"]) == 10 and data["count"] == 12 and data["more"] == 2
    assert data["items"][0] == {"person": "P0", "position": "Engineer"}


def test_a_long_cell_is_cut_and_other_values_stay() -> None:
    entry = view("  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n")
    result = RowsResult("members_of_unit", None, ("a", "b"), ({"a": "x" * 500, "b": [1, 2]},))
    item = section(shown(entry, rows={"rows": result}), "rows")["items"][0]
    assert item["a"] == "x" * 200 + "…" and item["b"] == [1, 2]


def test_a_failing_section_answers_alone_and_the_rest_comes() -> None:
    entry = view(
        "  - kind: fields\n    fields: [name]\n"
        "  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n"
    )
    failed = RowsResult("members_of_unit", "sha256:abc", error=("function.stale", "it is stale"))
    data = shown(entry, rows={"rows": failed})
    assert [s["kind"] for s in data["sections"]] == ["fields", "rows"]
    assert section(data, "rows") == {
        "kind": "rows",
        "id": "rows",
        "function": {"name": "members_of_unit", "hash": "sha256:abc"},
        "error": {"code": "function.stale", "message": "it is stale"},
    }
    again = shown(entry, rows={"rows": failed})
    assert dumps(again) == dumps(data)  # deterministic


def test_a_rows_section_without_a_result_is_unavailable() -> None:
    entry = view("  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n")
    error = section(shown(entry), "rows")["error"]
    assert error["code"] == "view.section_unavailable"


def test_a_rows_section_with_no_rows_has_columns_and_a_count_of_zero() -> None:
    entry = view("  - kind: rows\n    function: members_of_unit\n    args: {unit: $entity}\n")
    empty = RowsResult("members_of_unit", None, ("person",), ())
    assert section(shown(entry, rows={"rows": empty}), "rows") == {
        "kind": "rows",
        "id": "rows",
        "function": {"name": "members_of_unit"},
        "columns": ["person"],
        "count": 0,
    }


# two sections of one kind, the limit and the spec


def test_two_sections_of_one_kind_are_rendered_and_trimmed_on_their_own() -> None:
    two = view(
        POSITIONS
        + "  - kind: neighbors\n    id: others\n    relation: org:in_unit\n    direction: in\n"
        '    where: ["status=active"]\n    fields: [title]\n'
    )
    unit, others = world()
    full = render(spec_of(two, MODEL), unit, DictSnapshot(unit, *others), MODEL)
    assert not full.cut
    first, second = (s for s in full.sections if isinstance(s, card.NeighborsSection))
    assert (first.count, second.count) == (4, 2)
    # one byte too many: the fields of the rows go in the last section only, the first keeps them
    tight = replace(spec_of(two, MODEL), byte_limit=full.size() - 1)
    data = render(tight, unit, DictSnapshot(unit, *others), MODEL).to_json()
    assert data["cut"] is True
    assert all("fields" in r for r in section(data, "positions")["items"])
    assert all("fields" not in r for r in section(data, "others")["items"])
    # a limit nothing but the header fits: the rows go first (the counts stay), then the sections
    # themselves leave a stub with their kind and id
    smallest = replace(spec_of(two, MODEL), byte_limit=1)
    data = render(smallest, unit, DictSnapshot(unit, *others), MODEL).to_json()
    for sid in ("positions", "others"):
        assert section(data, sid) == {"kind": "neighbors", "id": sid, "cut": True}
    seen = False
    for limit in range(full.size() - 50, 0, -25):  # on the way down the rows go, counts stay
        data = render(
            replace(spec_of(two, MODEL), byte_limit=limit), unit, DictSnapshot(unit, *others), MODEL
        ).to_json()
        found = {sid: section(data, sid) for sid in ("positions", "others")}
        if any(s.get("cut") for s in found.values()):
            break
        for sid, count in (("positions", 4), ("others", 2)):
            assert found[sid]["count"] == count  # exact whatever is trimmed
            seen = seen or len(found[sid].get("items", [])) < count
    assert seen


def test_the_limit_of_an_authored_view_lowers_the_cards_and_never_raises_it() -> None:
    assert spec_of(compiled_entries()["unit_overview"], MODEL).byte_limit == 4096
    assert spec_of(compiled_entries()["position_overview"], MODEL).byte_limit == BYTE_LIMIT


def test_the_spec_keeps_order_and_hash_and_is_deterministic() -> None:
    entry = compiled_entries()["unit_overview"]
    spec = spec_of(entry, MODEL, {"status": "active"})
    assert spec.name == "unit_overview" and spec.origin == "authored" and spec.hash == entry.hash
    assert [type(s).__name__ for s in spec.sections] == [
        "FieldsSpec",
        "RelationsSpec",
        "NeighborsSpec",
        "RowsSpec",
        "FilesSpec",
        "AffordancesSpec",
    ]
    assert spec_of(entry, MODEL, {"status": "active"}) == spec
    assert spec_of(entry, MODEL, {"status": "planned"}) != spec  # the parameter is in the plan


def test_the_generated_card_still_has_no_ids_titles_or_hash() -> None:
    unit, others = world()
    data = render(
        card.generated_spec(MODEL, "org:Team"), unit, DictSnapshot(unit, *others), MODEL
    ).to_json()
    assert data["view"] == {"name": "card", "origin": "generated"}
    assert all("id" not in s and "title" not in s for s in data["sections"])  # type: ignore[attr-defined]


def hub_world(per: int = 700) -> tuple[Revision, list[Revision]]:
    hub = revision("hub", "org:Department", {"name": "Hub"}, alias="DEPT-9")
    others: list[Revision] = []
    for i in range(per):
        others.append(position(f"q{i}", f"Role {i:04d}", "active", unit="hub"))
        others.append(
            revision(f"c{i}", "org:Team", {"name": f"Team {i:04d}"}, [edge("org:part_of", "hub")])
        )
        others.append(
            revision(
                f"pol{i}",
                "ops:Policy",
                {"title": f"Policy {i:04d}", "status": "in_force"},
                [edge("ops:applies_to", "hub")],
            )
        )
    return hub, others


IN_UNIT = (
    "  - kind: neighbors\n    id: {id}\n    relation: org:in_unit\n    direction: in\n    max: 50\n"
)
PART_OF = (
    "  - kind: neighbors\n    id: {id}\n    relation: org:part_of\n    direction: in\n    max: 50\n"
)
APPLIES = (
    "  - kind: neighbors\n    id: {id}\n    relation: ops:applies_to\n    direction: in\n"
    "    max: 50\n"
)


def test_sections_over_the_same_neighbours_read_them_once() -> None:
    hub, people = hub_world()
    entry = view("".join(IN_UNIT.format(id=name) for name in "abc"))
    counting = CountingSnapshot(DictSnapshot(hub, *people))
    data = render(spec_of(entry, MODEL), hub, as_snapshot(counting), MODEL).to_json()
    assert len(counting.loaded) == 500  # one read of the neighbours, not three
    a, b, c = (section(data, s) for s in "abc")  # type: ignore[arg-type]
    assert a["sampled"] == b["sampled"] == c["sampled"] == 500
    assert a["count"] == 700 and a["approx"] is True


def test_many_relations_sections_cost_no_more_loads_than_one() -> None:
    hub, people = hub_world(per=150)
    one = view("  - kind: relations\n    relations: [{relation: org:in_unit, direction: in}]\n")
    many = view(
        "".join(
            f"  - kind: relations\n    id: r{i}\n"
            "    relations: [{relation: org:in_unit, direction: in}]\n"
            for i in range(32)
        )
    )
    counts = []
    for entry in (one, many):
        counting = CountingSnapshot(DictSnapshot(hub, *people))
        render(spec_of(entry, MODEL), hub, as_snapshot(counting), MODEL)
        counts.append(len(counting.loaded))
    assert counts[0] == counts[1] <= 100  # the group reads at most its own cap, once


def test_the_loads_of_an_authored_view_never_exceed_the_budget() -> None:
    hub, people = hub_world()
    entry = view(
        "".join(
            template.format(id=f"{kind}{i}")
            for i, (kind, template) in enumerate([("a", IN_UNIT), ("b", PART_OF), ("c", APPLIES)])
        )
        + "  - kind: relations\n    id: all\n"
        + "  - kind: files\n"
    )
    counting = CountingSnapshot(DictSnapshot(hub, *people))
    data = render(spec_of(entry, MODEL), hub, as_snapshot(counting), MODEL).to_json()
    assert len(counting.loaded) <= ENTITY_LOADS
    first, second, third = (section(data, i) for i in ("a0", "b1", "c2"))
    assert first["sampled"] == 500 and second["sampled"] == 500
    # the budget is spent: the third section read nothing and counts the raw in-edges
    assert "items" not in third and third["approx"] is True and third["count"] == 700


def test_a_where_that_could_not_read_every_edge_says_its_count_is_a_lower_bound() -> None:
    hub, people = hub_world()
    where = '    where: ["status=active"]\n'
    entry = view(
        IN_UNIT.format(id="a")
        + PART_OF.format(id="b")
        + APPLIES.format(id="c").replace("    max: 50\n", where + "    max: 50\n")
    )
    counting = CountingSnapshot(DictSnapshot(hub, *people))
    data = render(spec_of(entry, MODEL), hub, as_snapshot(counting), MODEL).to_json()
    spent = section(data, "c")
    # nothing of the policies could be read: not "no policy is in force", but unread edges
    assert spent["count"] == 0 or spent["unread"] > 0
    assert spent["approx"] is True and spent["unread"] > 0
    assert spent["unread"] + spent.get("sampled", 0) == 700
    assert len(counting.loaded) <= ENTITY_LOADS


def test_a_parameter_that_is_not_given_takes_its_default_and_a_required_one_is_missing() -> None:
    entry = compiled_entries()["unit_overview"]
    given = spec_of(entry, MODEL, {"status": "active"})
    assert spec_of(entry, MODEL) == given  # status has the default active
    from graph_cli.kernel.errors import GraphCliError

    required = view(
        POSITIONS.replace("    order", '    where: ["title=$t"]\n    order'),
        "params:\n  t:\n    type: string\n    summary: A title.\n",
    )
    with pytest.raises(GraphCliError) as exc:
        spec_of(required, MODEL)
    assert exc.value.code == "view.parameter_missing"


def test_a_where_whose_parameter_is_not_bound_never_matches_the_literal_text() -> None:
    from graph_cli.kernel.errors import GraphCliError
    from graph_cli.kinetic.card import _plan  # the planner of the specs

    with pytest.raises(GraphCliError) as exc:
        _plan(MODEL, ("org:Position",), ("title=$t",), (), {})
    assert exc.value.code == "internal.error"
