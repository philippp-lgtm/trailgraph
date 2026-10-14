"""validate_write and check_graph on both toy ontologies (contracts B3.17, B7.9, R.7; plan W5)."""

from dataclasses import replace
from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, eid, revision
from support.writes import codes, judge

from graph_cli.kernel.changeset import FloatLiteral
from graph_cli.kernel.findings import sort_findings
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import Bound, CompiledModel

INV = compiled_model("inventory")
NAS = compiled_model("notes_and_sources")

ROOM = revision("room", "inv:Location", {"name": "Room", "code": "R1"})
DESK = revision(
    "desk",
    "inv:Item",
    {"name": "Desk", "quantity": 1, "status": "in_stock", "serial_number": "SN-0001"},
    [edge("inv:located_in", "room", name="desk-room")],
)
OLD = revision(
    "old",
    "inv:Item",
    {"name": "Old", "quantity": 1, "status": "retired", "serial_number": "SN-0009"},
    [edge("inv:located_in", "room", name="old-room")],
    status="superseded",
)
INV_BASE = DictSnapshot(ROOM, DESK, OLD)

TOPIC = revision("topic", "nas:Topic", {"label": "Physics"})
WEB = revision("web", "nas:WebPage", {"title": "W", "url": "https://example.org/a"})
NOTE = revision(
    "note",
    "nas:Note",
    {"title": "N", "body": "text", "kind": "observation", "status": "draft"},
    [edge("nas:about", "topic", name="note-topic")],
)
NAS_BASE = DictSnapshot(TOPIC, WEB, NOTE)

R, D, OL = str(ROOM.id), str(DESK.id), str(OLD.id)
T, W, N = str(TOPIC.id), str(WEB.id), str(NOTE.id)


def item(**attrs: Any) -> dict[str, Any]:
    edges = attrs.pop("edges", [{"rel": "inv:located_in", "target": R}])
    return {
        "op": "create",
        "type": "inv:Item",
        "local": "x",
        "attrs": {"name": "X", **attrs},
        "edges": edges,
    }


def note(**attrs: Any) -> dict[str, Any]:
    edges = attrs.pop("edges", [{"rel": "nas:about", "target": T}])
    body = {"title": "T", "body": "b", "kind": "observation", **attrs}
    return {"op": "create", "type": "nas:Note", "local": "x", "attrs": body, "edges": edges}


def inv(*ops: dict[str, Any]) -> list[str]:
    return codes(judge(INV, *ops, base=INV_BASE)[1])


def nas(*ops: dict[str, Any]) -> list[str]:
    return codes(judge(NAS, *ops, base=NAS_BASE)[1])


def test_valid_writes_on_both_fixtures_have_no_findings() -> None:
    assert inv(item(), {"op": "transition", "id": D, "to": "in_use"}) == []
    assert (
        nas(
            note(
                edges=[
                    {"rel": "nas:about", "target": T},
                    {"rel": "nas:cites", "target": W, "qualifiers": {"locator": "p. 3"}},
                ]
            ),
            {"op": "transition", "id": N, "to": "published"},
        )
        == []
    )


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        (
            [{"op": "create", "type": "inv:Thing", "attrs": {"bogus": FloatLiteral("1.5")}}],
            ["data.structural.unknown_type"],
        ),
        ([item(colour="red")], ["data.structural.unknown_attribute"]),
        (
            [
                {
                    "op": "create",
                    "type": "inv:Item",
                    "edges": [{"rel": "inv:located_in", "target": R}],
                }
            ],
            ["data.structural.missing_required"],
        ),
        ([item(quantity="2")], ["data.structural.datatype_mismatch"]),
        ([item(quantity=-1)], ["data.structural.facet_violation"]),
        ([item(serial_number="bad serial")], ["data.structural.facet_violation"]),
        ([item(serial_number=7)], ["data.structural.datatype_mismatch"]),  # not also facet-checked
        ([item(condition="shiny")], ["data.structural.enum_value_unknown"]),
        ([item(name=["a", "b"])], ["data.structural.cardinality_many"]),
        ([item(acquired_on="2026-02-30")], ["data.structural.datatype_mismatch"]),
        ([item(name=None)], ["data.structural.datatype_mismatch"]),
        ([item(edges=[])], ["data.referential.cardinality_out"]),
        (
            [
                item(
                    edges=[
                        {"rel": "inv:located_in", "target": R},
                        {"rel": "inv:located_in", "target": R},
                    ]
                )
            ],
            ["data.referential.cardinality_out"],
        ),
        (
            [
                item(
                    edges=[
                        {"rel": "inv:located_in", "target": R},
                        {"rel": "inv:stored_at", "target": R},
                    ]
                )
            ],
            ["data.referential.unknown_relation"],
        ),
        (
            [
                item(
                    edges=[
                        {"rel": "inv:located_in", "target": "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"}
                    ]
                )
            ],
            ["data.referential.target_missing"],
        ),
        (
            [item(edges=[{"rel": "inv:located_in", "target": D}])],
            ["data.referential.range_violation"],
        ),
        (
            [{"op": "link", "id": R, "rel": "inv:part_of", "target": D}],
            ["data.referential.domain_violation"],
        ),
        (
            [{"op": "link", "id": D, "rel": "inv:in_category", "target": R}],
            ["data.referential.range_violation"],
        ),
        (
            [
                item(
                    edges=[
                        {"rel": "inv:located_in", "target": R},
                        {"rel": "inv:part_of", "target": OL},
                    ]
                )
            ],
            ["data.referential.target_not_active"],
        ),
        (
            [{"op": "unlink", "id": D, "rel": "inv:part_of", "target": R}],
            ["data.referential.edge_missing"],
        ),
        ([item(serial_number="SN-0001")], ["data.semantic.unique_violation"]),
        ([item(serial_number="SN-0009")], []),  # the superseded entity's value is free
        ([{"op": "link", "id": D, "rel": "inv:part_of", "target": D}], ["data.semantic.cycle"]),
        ([item(serial_number="SN-0002", quantity=2)], ["data.rule.violated"]),
        ([item(status="in_use")], ["data.lifecycle.invalid_initial_state"]),
        (
            [{"op": "transition", "id": D, "to": "ordered"}],
            ["data.lifecycle.transition_not_allowed"],
        ),
        (
            [
                {"op": "update", "id": D, "set": {"condition": "damaged"}},
                {"op": "transition", "id": D, "to": "in_use"},
            ],
            ["data.lifecycle.guard_failed"],
        ),
        (
            [{"op": "update", "id": D, "set": {"status": "in_use"}}],
            ["data.lifecycle.direct_update_of_lifecycle_attribute"],
        ),
        (
            [{"op": "update", "id": D, "unset": ["serial_number"]}],
            ["data.mutability.immutable_changed"],
        ),
        ([{"op": "update", "id": D, "set": {"acquired_on": "2026-01-01"}}], []),  # write-once
    ],
)
def test_inventory_layers(ops: list[dict[str, Any]], expected: list[str]) -> None:
    assert inv(*ops) == expected


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        (
            [{"op": "create", "type": "nas:Source", "attrs": {"title": "S"}}],
            ["data.structural.abstract_type"],
        ),
        ([note(edges=[])], ["data.referential.cardinality_out"]),
        (
            [
                note(
                    edges=[
                        {"rel": "nas:about", "target": T},
                        {"rel": "nas:cites", "target": W, "qualifiers": {"page": 3}},
                    ]
                )
            ],
            ["data.referential.qualifier_unknown"],
        ),
        (
            [
                note(
                    edges=[
                        {"rel": "nas:about", "target": T},
                        {"rel": "nas:cites", "target": W, "qualifiers": {"locator": 3}},
                    ]
                )
            ],
            ["data.structural.datatype_mismatch"],
        ),
        (
            [note(edges=[{"rel": "nas:about", "target": T}, {"rel": "nas:cites", "target": T}])],
            ["data.referential.range_violation"],
        ),
        (
            [{"op": "create", "type": "nas:Topic", "attrs": {"label": "Physics"}}],
            ["data.semantic.unique_violation"],
        ),
        (
            [
                {
                    "op": "create",
                    "type": "nas:Topic",
                    "local": "a",
                    "attrs": {"label": "A"},
                    "edges": [{"rel": "nas:broader", "target": "@b"}],
                },
                {
                    "op": "create",
                    "type": "nas:Topic",
                    "local": "b",
                    "attrs": {"label": "B"},
                    "edges": [{"rel": "nas:broader", "target": "@a"}],
                },
            ],
            ["data.semantic.cycle"],
        ),
        (
            [
                {"op": "update", "id": N, "set": {"body": ""}},
                {"op": "transition", "id": N, "to": "published"},
            ],
            ["data.lifecycle.guard_failed"],
        ),
        (
            [
                {"op": "update", "id": N, "set": {"kind": "summary"}},
                {"op": "transition", "id": N, "to": "published"},
            ],
            ["data.rule.violated"],
        ),
        (
            [{"op": "update", "id": W, "set": {"url": "https://example.org/b"}}],
            ["data.mutability.immutable_changed"],
        ),
        (
            [
                {"op": "update", "id": W, "set": {"retrieved_at": "2026-01-01"}},
                {"op": "update", "id": W, "set": {"retrieved_at": "2026-02-01"}},
            ],
            [],
        ),
        (
            [{"op": "update", "id": W, "set": {"url": "no scheme"}}],
            ["data.mutability.immutable_changed", "data.structural.datatype_mismatch"],
        ),
        ([note(title=FloatLiteral("1.5"))], ["data.structural.float_not_allowed"]),
    ],
)
def test_notes_layers(ops: list[dict[str, Any]], expected: list[str]) -> None:
    assert sorted(nas(*ops)) == sorted(expected)


def test_float_literals_from_input_are_refused() -> None:
    assert inv(item(quantity=FloatLiteral("1.0"))) == ["data.structural.float_not_allowed"]


def test_targets_carry_path_type_and_local() -> None:
    _, findings = judge(INV, item(colour="red"), base=INV_BASE)
    (finding,) = findings
    assert finding.target["path"] == "attrs.colour" and finding.target["local"] == "x"
    assert finding.target["type"] == "inv:Item"
    assert finding.expected is None or isinstance(finding.expected, list)
    _, findings = judge(INV, {"op": "create", "type": "inv:Iten", "attrs": {}}, base=INV_BASE)
    assert isinstance(findings[0].expected, list) and findings[0].expected[0] == "inv:Item"


def test_findings_are_sorted_and_carry_rule_ids() -> None:
    working, findings = judge(
        INV,
        item(serial_number="SN-0002", quantity=2, colour="red"),
        {"op": "update", "id": D, "set": {"condition": "damaged"}},
        {"op": "transition", "id": D, "to": "in_use"},
        base=INV_BASE,
    )
    assert findings == sort_findings(findings)
    by_code = {f.code: f for f in findings}
    assert by_code["data.rule.violated"].rule_id == "inv.serial_single_unit"
    assert by_code["data.lifecycle.guard_failed"].rule_id == "inv.usable"
    assert by_code["data.rule.violated"].target["id"] == str(working.locals["x"])


def test_explicit_alias_must_be_unbound() -> None:
    base = DictSnapshot(ROOM, replace(DESK, alias="ITEM-0001"))
    _, findings = judge(INV, {"op": "set_alias", "id": R, "alias": "ITEM-0001"}, base=base)
    assert codes(findings) == ["data.semantic.unique_violation"]
    assert findings[0].target["path"] == "alias"
    _, findings = judge(
        INV,
        {"op": "set_alias", "id": R, "alias": "LOC-0005"},
        {"op": "set_alias", "id": D, "alias": "LOC-0005"},
        base=base,
    )
    assert codes(findings) == ["data.semantic.unique_violation"]


def test_supersede_lets_a_replacement_take_over_unique_values() -> None:
    _, findings = judge(
        INV,
        item(serial_number="SN-0001"),
        {"op": "supersede", "by": "@x", "targets": [D]},
        base=INV_BASE,
    )
    assert findings == []


def test_check_graph_is_clean_on_valid_data() -> None:
    assert check_graph(INV, INV_BASE) == [
        f for f in check_graph(INV, INV_BASE) if f.severity.value == "warning"
    ]
    assert (
        check_graph(
            NAS,
            DictSnapshot(
                TOPIC,
                WEB,
                NOTE,
                revision(
                    "cite",
                    "nas:Note",
                    {"title": "C", "body": "b", "kind": "observation", "status": "draft"},
                    [edge("nas:about", "topic", name="c-t"), edge("nas:cites", "web", name="c-w")],
                ),
            ),
        )
        == []
    )


def test_check_graph_reports_batch_rules_and_state_values() -> None:
    found = check_graph(NAS, NAS_BASE)
    assert [(f.code, f.rule_id, f.severity.value) for f in found] == [
        ("data.rule.violated", "nas.source_cited", "warning")
    ]
    broken = DictSnapshot(ROOM, replace(DESK, attrs={**DESK.attrs, "status": "lost"}))
    assert codes(check_graph(INV, broken)) == ["data.structural.enum_value_unknown"]


def test_check_graph_unique_duplicates_and_cycles() -> None:
    twin = revision(
        "twin", "nas:Topic", {"label": "Physics"}, [edge("nas:broader", "topic", name="tw")]
    )
    loop = replace(TOPIC, edges=(edge("nas:broader", "twin", name="lp"),))
    found = check_graph(NAS, DictSnapshot(loop, twin, WEB, NOTE))
    assert sorted(codes(found)) == sorted(
        ["data.semantic.unique_violation"] * 2 + ["data.semantic.cycle", "data.rule.violated"]
    )


def test_check_graph_cardinality_in() -> None:
    located = INV.relations["inv:located_in"]
    model: CompiledModel = replace(
        INV, relations={**INV.relations, "inv:located_in": replace(located, into=Bound(1, 1))}
    )
    empty = revision(
        "empty",
        "inv:Location",
        {"name": "E", "code": "E1"},
        [edge("inv:within", "room", name="e-r")],
    )
    second = revision(
        "second",
        "inv:Item",
        {"name": "S", "quantity": 1, "status": "ordered"},
        [edge("inv:located_in", "room", name="s-r")],
    )
    found = [
        f
        for f in check_graph(model, DictSnapshot(ROOM, DESK, empty, second))
        if f.code == "data.referential.cardinality_in"
    ]
    assert sorted(str(f.target["id"]) for f in found) == sorted([R, str(empty.id)])
    assert {f.target["path"] for f in found} == {"incoming.inv:located_in"}


def test_check_graph_ignores_inactive_entities_for_rules() -> None:
    bad = revision(
        "bad",
        "inv:Item",
        {"name": "B", "quantity": 3, "serial_number": "SN-0003", "status": "retired"},
        [edge("inv:located_in", "room", name="b-r")],
        status="retracted",
    )
    assert check_graph(INV, DictSnapshot(ROOM, DESK, bad)) == []
    assert eid("bad") == bad.id
