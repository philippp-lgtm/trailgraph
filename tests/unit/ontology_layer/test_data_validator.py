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
        (
            [{"op": "update", "id": D, "unset": ["colour"]}],
            ["data.structural.unknown_attribute"],
        ),
        (
            [{"op": "update", "id": D, "add": {"colour": ["red"]}}],
            ["data.structural.unknown_attribute"],
        ),
        (
            [{"op": "update", "id": D, "add_text": {"name": ["Desk 2"]}}],
            ["data.structural.cardinality_many"],
        ),
        (
            [{"op": "update", "id": D, "remove": {"serial_number": ["SN-0001"]}}],
            ["data.structural.cardinality_many"],
        ),
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


def test_rule_findings_with_aggregates_carry_their_values() -> None:
    _, findings = judge(NAS, note(kind="summary", status="published"), base=NAS_BASE)
    (finding,) = [f for f in findings if f.code == "data.rule.violated"]
    assert finding.rule_id == "nas.summary_cites_source"
    assert finding.expected is None
    assert finding.actual == [
        {"operand": {"count": {"rel": "nas:cites", "direction": "out"}}, "value": 0},
        {"operand": {"const": 1}, "value": 1},
    ]
    (orphan,) = [f for f in check_graph(NAS, DictSnapshot(TOPIC)) if f.rule_id]
    assert orphan.actual == [
        {"operand": {"count": {"rel": "nas:about", "direction": "in"}}, "value": 0},
        {"operand": {"const": 1}, "value": 1},
        {"operand": {"count": {"rel": "nas:broader", "direction": "in"}}, "value": 0},
    ]


def test_sum_rule_findings_carry_the_sum_and_the_bound() -> None:
    parts = {"sum": {"rel": "inv:part_of", "direction": "in", "attr": "quantity"}}
    serial = next(r for r in INV.rules if r.id == "inv.serial_single_unit")
    model = replace(
        INV, rules=(replace(serial, id="inv.parts", expr={"le": [parts, {"attr": "quantity"}]}),)
    )
    box = revision("box", "inv:Item", {"name": "Box", "quantity": 2})
    pieces = [
        revision(f"p{n}", "inv:Item", {"name": "P", "quantity": n}, [edge("inv:part_of", "box")])
        for n in (1, 2)
    ]
    (finding,) = [f for f in check_graph(model, DictSnapshot(box, *pieces)) if f.rule_id]
    assert finding.target["id"] == str(box.id)
    assert finding.actual == [
        {"operand": parts, "value": 3},
        {"operand": {"attr": "quantity"}, "value": 2},
    ]


def test_check_graph_reports_parts_heavier_than_their_item() -> None:
    model = compiled_model("inventory", "2.1.0")

    def thing(name: str, weight: str, *edges: Any) -> Any:
        attrs = {"label": name, "quantity": 1, "status": "in_stock", "weight": weight}
        placed = [edge("inv:located_in", "room"), edge("inv:in_category", "tools")]
        return revision(name, "inv:Item", attrs, [*placed, *edges])

    def snapshot(second: str) -> DictSnapshot:
        parts = [
            thing(n, w, edge("inv:part_of", "rack")) for n, w in (("p1", "2.5"), ("p2", second))
        ]
        tools = revision("tools", "inv:Category", {"name": "Tools"})
        return DictSnapshot(ROOM, tools, thing("rack", "5.00"), *parts)

    assert check_graph(model, snapshot("2.50")) == []  # positive control
    (finding,) = check_graph(model, snapshot("2.75"))
    assert (finding.code, finding.rule_id, finding.severity.value) == (
        "data.rule.violated",
        "inv.parts_within_weight",
        "warning",
    )
    assert finding.target["id"] == str(eid("rack"))
    weights = {
        "rel": "inv:part_of",
        "direction": "in",
        "attr": "weight",
        "where": {"record_status": ["active"]},
    }
    count = {k: v for k, v in weights.items() if k != "attr"}
    assert finding.actual == [
        {"operand": {"count": count}, "value": 2},
        {"operand": {"const": 1}, "value": 1},
        {"operand": {"sum": weights}, "value": "5.25"},
        {"operand": {"attr": "weight"}, "value": "5.00"},
    ]


def test_rule_findings_without_aggregates_are_unchanged() -> None:
    _, findings = judge(INV, item(serial_number="SN-0002", quantity=2), base=INV_BASE)
    (finding,) = findings
    assert (finding.code, finding.rule_id) == ("data.rule.violated", "inv.serial_single_unit")
    assert finding.expected is None and finding.actual is None


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


GHOST = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"  # an id with no entity


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        (
            [{"op": "unlink", "id": D, "rel": "inv:bogus", "target": R}],
            ["data.referential.unknown_relation"],
        ),
        (
            [{"op": "unlink", "id": R, "rel": "inv:part_of", "target": D}],
            ["data.referential.domain_violation"],
        ),
        (
            [
                {"op": "link", "id": D, "rel": "inv:bogus", "target": R},
                {"op": "unlink", "id": D, "rel": "inv:bogus", "target": R},
            ],
            ["data.referential.unknown_relation"],
        ),
        (
            [
                {"op": "link", "id": R, "rel": "inv:part_of", "target": D},
                {"op": "unlink", "id": R, "rel": "inv:part_of", "target": D},
            ],
            ["data.referential.domain_violation"],
        ),
        (
            [
                {"op": "link", "id": D, "rel": "inv:part_of", "target": GHOST},
                {"op": "unlink", "id": D, "rel": "inv:part_of", "target": GHOST},
            ],
            ["data.referential.target_missing"],
        ),
        (
            [
                item(
                    edges=[
                        {"rel": "inv:located_in", "target": R},
                        {"rel": "inv:bogus", "target": R},
                    ]
                ),
                {"op": "unlink", "id": "@x", "rel": "inv:bogus", "target": R},
            ],
            ["data.referential.unknown_relation"],
        ),
        (
            [{"op": "transition", "id": D, "attr": "colour", "to": "in_use"}],
            ["data.structural.unknown_attribute"],
        ),
        (
            [{"op": "transition", "id": D, "attr": "name", "to": "in_use"}],
            ["data.lifecycle.transition_not_allowed"],
        ),
        (
            [
                {"op": "update", "id": D, "set": {"colour": "red"}},
                {"op": "update", "id": D, "unset": ["colour"]},
            ],
            ["data.structural.unknown_attribute"],
        ),
    ],
)
def test_names_from_ops_are_judged_without_the_post_state(
    ops: list[dict[str, Any]], expected: list[str]
) -> None:
    assert inv(*ops) == expected


def test_qualifiers_of_a_removed_edge_are_judged() -> None:
    link = {"op": "link", "id": N, "rel": "nas:cites", "target": W, "qualifiers": {"page": 3}}
    unlink = {"op": "unlink", "id": N, "rel": "nas:cites", "target": W}
    assert nas(link, unlink) == ["data.referential.qualifier_unknown"]
    valid = {**link, "qualifiers": {"locator": "p. 3"}}
    assert nas(valid, unlink) == []


def test_unlinking_an_existing_edge_is_not_judged_again() -> None:
    broken = revision("broken", "inv:Item", {**DESK.attrs, "serial_number": "SN-0005"})
    broken = replace(broken, edges=(*DESK.edges, edge("inv:bogus", "room", name="bogus")))
    base = DictSnapshot(ROOM, broken)
    unlink = {"op": "unlink", "id": str(broken.id), "rel": "inv:bogus", "target": R}
    assert codes(judge(INV, unlink, base=base)[1]) == []


def test_an_unknown_target_state_is_named() -> None:
    _, findings = judge(INV, {"op": "transition", "id": D, "to": "lost"}, base=INV_BASE)
    (finding,) = findings
    assert finding.code == "data.lifecycle.transition_not_allowed"
    assert "'lost' is not a state of" in finding.message


# ---- neighbours of touched entities (aggregate write rules) ---------------------------

ACTIVE_PARTS = {"rel": "inv:part_of", "direction": "in", "where": {"record_status": ["active"]}}
PARTS_SUM = {"sum": {**ACTIVE_PARTS, "attr": "quantity"}}
PARTS_COUNT = {"count": ACTIVE_PARTS}


def with_rules(*rules: tuple[str, str, Any]) -> CompiledModel:
    """INV with ``inv.serial_single_unit`` plus write rules ``(id, severity, expr)`` on items."""
    serial = next(r for r in INV.rules if r.id == "inv.serial_single_unit")
    extra = tuple(replace(serial, id=i, severity=sev, expr=expr) for i, sev, expr in rules)
    return replace(INV, rules=(serial, *extra))


SUM_MODEL = with_rules(
    (
        "inv.parts_add_up",
        "warning",
        {
            "implies": [
                {"ge": [PARTS_COUNT, {"const": 1}]},
                {"eq": [PARTS_SUM, {"attr": "quantity"}]},
            ]
        },
    ),
    ("inv.few_parts", "error", {"le": [PARTS_COUNT, {"const": 2}]}),
)


def stored(name: str, quantity: int, *edges: Any, **kw: Any) -> Any:
    return revision(
        name,
        "inv:Item",
        {"name": name, "quantity": quantity, **kw.pop("attrs", {})},
        [edge("inv:located_in", "room", name=f"{name}-room"), *edges],
        **kw,
    )


BOX = stored("box", 3, attrs={"serial_number": "SN-0100"})  # violates the serial rule already
BIN = stored("bin", 5)
PART_A = stored("a", 1, edge("inv:part_of", "box", name="a-box"))
PART_B = stored("b", 2, edge("inv:part_of", "box", name="b-box"))
TREE = DictSnapshot(ROOM, BOX, BIN, PART_A, PART_B)
BOX_ID, BIN_ID, A_ID, B_ID = (str(r.id) for r in (BOX, BIN, PART_A, PART_B))


def rule_hits(*ops: dict[str, Any], model: CompiledModel = SUM_MODEL, base: Any = TREE) -> Any:
    _, findings = judge(model, *ops, base=base)
    return sorted((f.rule_id, f.target["id"], f.severity.value) for f in findings if f.rule_id)


def part(quantity: int, parent: str = BOX_ID) -> dict[str, Any]:
    return item(
        quantity=quantity,
        edges=[{"rel": "inv:located_in", "target": R}, {"rel": "inv:part_of", "target": parent}],
    )


def test_a_consistent_tree_has_no_rule_findings() -> None:
    assert rule_hits({"op": "update", "id": A_ID, "set": {"name": "A"}}) == []


def test_creating_a_child_rechecks_its_parent() -> None:
    assert rule_hits(part(1)) == [
        ("inv.few_parts", BOX_ID, "error"),  # an error on a neighbour blocks the write
        ("inv.parts_add_up", BOX_ID, "warning"),
    ]
    assert rule_hits(part(2, BIN_ID)) == [("inv.parts_add_up", BIN_ID, "warning")]
    assert rule_hits(part(5, BIN_ID)) == []


def test_changes_of_children_recheck_old_and_new_parents() -> None:
    update = {"op": "update", "id": A_ID, "set": {"quantity": 2}}
    assert rule_hits(update) == [("inv.parts_add_up", BOX_ID, "warning")]
    unlink = {"op": "unlink", "id": B_ID, "rel": "inv:part_of", "target": BOX_ID}
    assert rule_hits(unlink) == [("inv.parts_add_up", BOX_ID, "warning")]
    assert rule_hits({"op": "retract", "id": B_ID}) == [("inv.parts_add_up", BOX_ID, "warning")]
    link = {"op": "link", "id": B_ID, "rel": "inv:part_of", "target": BIN_ID}
    assert rule_hits(unlink, link) == sorted(
        [("inv.parts_add_up", BIN_ID, "warning"), ("inv.parts_add_up", BOX_ID, "warning")]
    )
    fixed = {"op": "update", "id": BIN_ID, "set": {"quantity": 2}}
    assert rule_hits(unlink, link, fixed) == [("inv.parts_add_up", BOX_ID, "warning")]


def test_out_aggregates_recheck_the_sources() -> None:
    parent_sum = {"sum": {"rel": "inv:part_of", "direction": "out", "attr": "quantity"}}
    model = with_rules(("inv.small_parent", "warning", {"le": [parent_sum, {"const": 10}]}))
    update = {"op": "update", "id": BOX_ID, "set": {"quantity": 11}}
    assert rule_hits(update, model=model) == sorted(
        [
            ("inv.serial_single_unit", BOX_ID, "error"),  # the touched box itself
            ("inv.small_parent", A_ID, "warning"),
            ("inv.small_parent", B_ID, "warning"),
        ]
    )


def test_touched_entities_are_checked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_cli.ontology_layer import data_validator

    calls: list[tuple[str, str]] = []
    original = data_validator.evaluate

    def counting(expr: Any, entity: Any, snapshot: Any, model: Any) -> bool:
        calls.append((str(entity.id), next(r.id for r in model.rules if r.expr is expr)))
        return original(expr, entity, snapshot, model)

    monkeypatch.setattr(data_validator, "evaluate", counting)
    update = {"op": "update", "id": BOX_ID, "set": {"quantity": 4}}
    assert rule_hits(part(1), update) == [
        ("inv.few_parts", BOX_ID, "error"),
        ("inv.serial_single_unit", BOX_ID, "error"),  # the box is touched now
    ]
    box_calls = [rule for entity, rule in calls if entity == BOX_ID]
    assert sorted(box_calls) == ["inv.few_parts", "inv.parts_add_up", "inv.serial_single_unit"]


def test_rules_without_aggregates_never_run_on_neighbours() -> None:
    # The box violates the serial rule in the base state; touching a part does not report it.
    assert rule_hits({"op": "update", "id": A_ID, "set": {"name": "A"}}) == []


def test_inactive_and_unknown_type_neighbours_are_skipped() -> None:
    old_box = stored("box", 3, status="superseded")
    base = DictSnapshot(ROOM, old_box, PART_A, PART_B)
    assert rule_hits(part(1), base=base) == []
    ghost = revision("ghost", "inv:Gone", {"name": "g"})
    base = DictSnapshot(ROOM, ghost)
    assert rule_hits(part(1, str(ghost.id)), base=base) == []


# ---- possible duplicates (F47) ----------------------------------------------

OPTICS = revision("optics", "nas:Topic", {"label": "Optics", "synonyms": ["Lenses"]})
GONE = revision("gone", "nas:Topic", {"label": "Acoustics"}, status="superseded")
NAMES = DictSnapshot(TOPIC, OPTICS, GONE, WEB, NOTE)
OP = str(OPTICS.id)


def topic(local: str, label: str, **attrs: Any) -> dict[str, Any]:
    return {"op": "create", "type": "nas:Topic", "local": local, "attrs": {"label": label, **attrs}}


def names(*ops: dict[str, Any]) -> list[tuple[str, str | None, str, Any]]:
    """``(code, local or id, path, actual)`` of every finding."""
    found = judge(NAS, *ops, base=NAMES)[1]
    return [
        (
            f.code,
            str(f.target.get("local") or f.target.get("id")),
            str(f.target.get("path")),
            f.actual,
        )
        for f in found
    ]


def test_a_new_name_already_carried_by_another_entity_warns() -> None:
    physics = {
        "id": T,
        "alias": None,
        "type": "nas:Topic",
        "attr": "label",
        "role": "label",
        "name": "Physics",
        "match": "same",
    }
    assert names(topic("x", "Mechanics", synonyms=["physics"])) == [
        ("data.semantic.possible_duplicate", "x", "attrs.synonyms", [physics])
    ]
    # Main name against an alternative one, any case; a hidden name counts too.
    lenses = {
        "id": OP,
        "alias": None,
        "type": "nas:Topic",
        "attr": "synonyms",
        "role": "alt_label",
        "name": "Lenses",
        "match": "same",
    }
    assert names(topic("x", "LENSES")) == [
        ("data.semantic.possible_duplicate", "x", "attrs.label", [lenses])
    ]
    assert names(topic("x", "Mirrors", misspellings=["lenses"]))[0][2] == "attrs.misspellings"
    (finding,) = judge(NAS, topic("x", "Mechanics", synonyms=["physics"]), base=NAMES)[1]
    assert finding.severity.value == "warning"
    assert finding.message == f"'physics' is also a name of {T} (nas:Topic, label)"


def test_punctuation_accents_and_word_breaks_do_not_tell_names_apart() -> None:
    mail = revision("mail", "nas:Topic", {"label": "E-Mail", "misspellings": ["emial"]})
    base = DictSnapshot(mail)
    for label in ("email", "E Mail", "É-mail!"):
        (finding,) = judge(NAS, topic("x", label), base=base)[1]
        assert finding.actual[0]["match"] == "same"  # type: ignore[index]
    # A hidden name is matched but never shown.
    (finding,) = judge(NAS, topic("x", "Emial"), base=base)[1]
    assert finding.actual[0]["role"] == "hidden_label"  # type: ignore[index]
    assert finding.actual[0]["name"] is None  # type: ignore[index]


def test_a_typo_of_one_word_is_a_similar_name() -> None:
    (finding,) = judge(NAS, topic("x", "Optcs"), base=NAMES)[1]
    assert finding.code == "data.semantic.possible_duplicate"
    assert finding.message == f"'Optcs' is close to a name of {OP} (nas:Topic, label)"
    assert finding.actual[0]["match"] == "similar"  # type: ignore[index]
    assert finding.actual[0]["name"] == "Optics"  # type: ignore[index]
    assert [who for _, who, *_ in names(topic("x", "Mechanics", synonyms=["Lense"]))] == ["x"]


def test_numbers_short_words_and_other_words_are_not_typos() -> None:
    labels = ("Part 1", "Phase I", "Bank", "Debitor", "Cost accounting")
    base = DictSnapshot(*(revision(t, "nas:Topic", {"label": t}) for t in labels))
    for label in ("Part 2", "Phase II", "Band", "Kreditor", "Cost planning", "Part 1 b"):
        assert judge(NAS, topic("x", label), base=base)[1] == [], label
    # Exact names only in the batch check: a similar pair already stored stays quiet.
    pair = DictSnapshot(OPTICS, revision("optcs", "nas:Topic", {"label": "Optcs"}))
    assert [f for f in check_graph(NAS, pair) if "duplicate" in f.code] == []


def test_unique_refusal_is_not_repeated_as_a_duplicate() -> None:
    assert [code for code, *_ in names(topic("x", "Physics"))] == ["data.semantic.unique_violation"]


def test_only_new_names_of_active_entities_in_the_family_count() -> None:
    assert names(topic("x", "Acoustics")) == []  # the other one is superseded
    assert names(topic("x", "N")) == []  # a note title: another family
    assert names({"op": "update", "id": T, "set": {"synonyms": ["physics"]}}) == []  # its own
    # Optics keeps "Lenses"; a name it already had is not new, even if shared later.
    shared = DictSnapshot(TOPIC, OPTICS, revision("glass", "nas:Topic", {"label": "Lenses"}))
    found = judge(NAS, {"op": "update", "id": OP, "set": {"misspellings": ["optix"]}}, base=shared)
    assert codes(found[1]) == []
    assert codes(
        judge(NAS, {"op": "update", "id": OP, "set": {"misspellings": ["physics"]}}, base=shared)[1]
    ) == ["data.semantic.possible_duplicate"]


def test_two_new_entities_with_one_name_report_each_other() -> None:
    found = names(topic("a", "Thermodynamics"), topic("b", "thermodynamics"))
    assert [(code, who) for code, who, *_ in found] == [
        ("data.semantic.possible_duplicate", "a"),
        ("data.semantic.possible_duplicate", "b"),
    ]


def test_check_graph_reports_a_shared_name_once() -> None:
    twin = revision("twin", "nas:Topic", {"label": "Natural philosophy", "synonyms": ["physics"]})
    found = [f for f in check_graph(NAS, DictSnapshot(TOPIC, twin)) if "duplicate" in f.code]
    (finding,) = found
    first, second = sorted((TOPIC, twin), key=lambda r: str(r.id))
    assert finding.target["id"] == str(first.id)
    assert [c["id"] for c in finding.actual] == [str(second.id)]  # type: ignore[union-attr]


def deprecating(model: CompiledModel, *terms: str) -> CompiledModel:
    """``model`` with ``inv:Item``, its attribute ``attr:<name>``, an enum or a relation
    marked deprecated."""

    def doc(term: Any) -> Any:
        return replace(term, doc=replace(term.doc, deprecated=True, summary="Use the other."))

    classes = dict(model.classes)
    item_class = classes["inv:Item"]
    attributes = {
        name: doc(a) if f"attr:{name}" in terms else a for name, a in item_class.attributes.items()
    }
    classes["inv:Item"] = replace(
        doc(item_class) if "inv:Item" in terms else item_class, attributes=attributes
    )
    return replace(
        model,
        classes=classes,
        enums={k: doc(v) if k in terms else v for k, v in model.enums.items()},
        relations={k: doc(v) if k in terms else v for k, v in model.relations.items()},
    )


def deprecated_uses(model: CompiledModel, *ops: dict[str, Any]) -> list[tuple[str, Any]]:
    found = judge(model, *ops, base=INV_BASE)[1]
    assert all(f.severity.value == "warning" for f in found if "deprecated" in f.code)
    return [(str(f.target.get("path")), f.actual) for f in found if "deprecated" in f.code]


def test_a_write_that_uses_a_deprecated_term_warns() -> None:
    model = deprecating(INV, "inv:Item", "attr:serial_number", "inv:Condition", "inv:located_in")
    assert deprecated_uses(model, item(serial_number="SN-1234", condition="good")) == [
        ("attrs.condition", "inv:Condition"),
        ("attrs.serial_number", "serial_number"),
        ("edges.inv:located_in", "inv:located_in"),
        ("type", "inv:Item"),
    ]
    (finding,) = [
        f for f in judge(model, item(), base=INV_BASE)[1] if f.target.get("path") == "type"
    ]
    assert finding.message == "inv:Item is deprecated: Use the other."


def test_values_and_edges_an_entity_already_had_are_not_reported_again() -> None:
    model = deprecating(INV, "inv:Item", "attr:serial_number", "inv:located_in")
    assert deprecated_uses(model, {"op": "update", "id": D, "set": {"name": "Desk 2"}}) == []
    assert deprecated_uses(
        model, {"op": "update", "id": D, "set": {"serial_number": "SN-0002"}}
    ) == [("attrs.serial_number", "serial_number")]
    assert deprecated_uses(
        model, {"op": "link", "id": D, "rel": "inv:located_in", "target": R}
    ) == [("edges.inv:located_in", "inv:located_in")]


def test_check_graph_does_not_report_deprecated_terms() -> None:
    model = deprecating(INV, "inv:Item", "attr:serial_number", "inv:located_in")
    assert [f for f in check_graph(model, INV_BASE) if "deprecated" in f.code] == []


def list_text_findings(*ops: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (f.code, str(f.target.get("local") or f.target.get("id")), str(f.target.get("path")))
        for f in judge(NAS, *ops, base=NAS_BASE)[1]
    ]


def test_a_json_array_given_as_text_to_a_list_is_refused() -> None:
    array = '["optics", "lenses"]'
    expected = ("data.structural.cardinality_many", T, "attrs.synonyms")
    assert list_text_findings({"op": "update", "id": T, "set_text": {"synonyms": array}}) == [
        expected
    ]
    assert list_text_findings({"op": "update", "id": T, "add_text": {"synonyms": [array]}}) == [
        expected
    ]
    assert list_text_findings(
        {"op": "update", "id": T, "remove_text": {"synonyms": ["x", " [1]"]}}
    ) == [expected]
    assert list_text_findings(
        {
            "op": "create",
            "type": "nas:Topic",
            "local": "x",
            "attrs_text": {"label": "Optics", "synonyms": array},
        }
    ) == [("data.structural.cardinality_many", "x", "attrs.synonyms")]
    (finding,) = judge(
        NAS, {"op": "update", "id": T, "set_text": {"synonyms": array}}, base=NAS_BASE
    )[1]
    assert "--set-json" in finding.message and "--add" in finding.message


def test_bracketed_text_and_json_lists_are_fine() -> None:
    assert (
        list_text_findings({"op": "update", "id": T, "set_text": {"synonyms": "[sic] optics"}})
        == []
    )
    assert list_text_findings({"op": "update", "id": T, "set_text": {"label": '["a"]'}}) == []
    assert list_text_findings({"op": "update", "id": T, "set": {"synonyms": ["a", "b"]}}) == []
