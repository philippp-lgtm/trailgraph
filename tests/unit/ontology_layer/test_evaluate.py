"""Expression evaluator against the AST nodes and every fixture guard and rule (B7.15)."""

from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.evaluate import evaluate, exact_sum, explain
from graph_cli.ontology_layer.expr_ast import aggregate_relations, unknown_nodes
from graph_cli.ontology_layer.model import CompiledModel

NAS = compiled_model("notes_and_sources")
INV = compiled_model("inventory")


def a(name: str) -> dict[str, Any]:
    return {"attr": name}


def c(value: Any) -> dict[str, Any]:
    return {"const": value}


def holds(expr: Any, attrs: dict[str, Any], model: CompiledModel = INV, **kw: Any) -> bool:
    entity = revision("e", kw.pop("type", "inv:Item"), attrs, kw.pop("edges", ()), **kw)
    return evaluate(expr, entity, DictSnapshot(entity), model)


@pytest.mark.parametrize(
    ("expr", "attrs", "expected"),
    [
        ({"eq": [a("quantity"), c(1)]}, {"quantity": 1}, True),
        ({"eq": [a("quantity"), c(True)]}, {"quantity": 1}, False),  # type-strict
        ({"eq": [a("quantity"), c(1)]}, {}, False),
        ({"ne": [a("quantity"), c(1)]}, {"quantity": 2}, True),
        ({"ne": [a("quantity"), c(1)]}, {}, False),  # absent matches nothing
        ({"ne": [a("quantity"), c(1)]}, {"quantity": None}, False),  # null is absent
        ({"lt": [a("quantity"), c(2)]}, {"quantity": 1}, True),
        ({"le": [a("quantity"), c(1)]}, {"quantity": 1}, True),
        ({"gt": [a("quantity"), c(1)]}, {"quantity": 1}, False),
        ({"ge": [a("quantity"), c(1)]}, {"quantity": 1}, True),
        ({"gt": [a("name"), c(1)]}, {"name": "x"}, False),  # no order across types
        ({"lt": [a("name"), c("b")]}, {"name": "a"}, True),
        ({"in": [a("condition"), c(["new", "good"])]}, {"condition": "good"}, True),
        ({"in": [a("condition"), c(["new", "good"])]}, {"condition": "worn"}, False),
        ({"exists": a("serial_number")}, {"serial_number": "SN-1"}, True),
        ({"exists": a("serial_number")}, {}, False),
        ({"nonempty": a("name")}, {"name": ""}, False),
        ({"nonempty": a("name")}, {"name": "x"}, True),
        ({"nonempty": a("tags")}, {"tags": []}, False),
        ({"nonempty": a("tags")}, {"tags": {"k": 1}}, True),
        ({"nonempty": a("quantity")}, {"quantity": 0}, True),
        ({"nonempty": a("name")}, {}, False),
        ({"and": [{"exists": a("name")}, {"exists": a("quantity")}]}, {"name": "x"}, False),
        ({"or": [{"exists": a("name")}, {"exists": a("quantity")}]}, {"name": "x"}, True),
        ({"not": {"exists": a("name")}}, {}, True),
        ({"implies": [{"exists": a("name")}, {"exists": a("quantity")}]}, {}, True),
        ({"implies": [{"exists": a("name")}, {"exists": a("quantity")}]}, {"name": "x"}, False),
    ],
)
def test_nodes(expr: Any, attrs: dict[str, Any], expected: bool) -> None:
    assert holds(expr, attrs) is expected


def test_self_record_status() -> None:
    expr = {"eq": [{"self": "record_status"}, c("superseded")]}
    assert holds(expr, {}, status="superseded")
    assert not holds(expr, {})


def test_decimal_attributes_compare_numerically(monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_cli.ontology_layer import evaluate as module

    monkeypatch.setattr(module._Evaluator, "is_decimal", lambda self, node: node == a("price"))
    assert holds({"eq": [a("price"), c(12)]}, {"price": "12.00"})
    assert holds({"gt": [a("price"), c("9.5")]}, {"price": "12.00"})
    assert not holds({"eq": [a("name"), c(12)]}, {"name": "12.00"})


def test_count_out_and_in_with_where() -> None:
    model = NAS
    note = revision(
        "n",
        "nas:Note",
        edges=[edge("nas:cites", "web"), edge("nas:cites", "book"), edge("nas:about", "t")],
    )
    other = revision("m", "nas:Note", edges=[edge("nas:cites", "web")], status="superseded")
    web = revision("web", "nas:WebPage", {"title": "w", "url": "https://x"})
    book = revision("book", "nas:Book", {"title": "b"}, status="retracted")
    topic = revision("t", "nas:Topic", {"label": "t"})
    snap = DictSnapshot(note, other, web, book, topic)

    def count(entity: Any, spec: dict[str, Any], n: int) -> bool:
        return evaluate({"eq": [{"count": spec}, c(n)]}, entity, snap, model)

    assert count(note, {"rel": "nas:cites", "direction": "out"}, 2)
    assert count(
        note, {"rel": "nas:cites", "direction": "out", "where": {"type": ["nas:Source"]}}, 2
    )
    assert count(note, {"rel": "nas:cites", "direction": "out", "where": {"type": ["nas:Book"]}}, 1)
    active = {"record_status": ["active"]}
    assert count(note, {"rel": "nas:cites", "direction": "out", "where": active}, 1)
    assert count(web, {"rel": "nas:cites", "direction": "in"}, 2)
    assert count(web, {"rel": "nas:cites", "direction": "in", "where": active}, 1)
    assert count(topic, {"rel": "nas:cites", "direction": "in"}, 0)
    dangling = revision("d", "nas:Note", edges=[edge("nas:cites", "missing")])
    assert count(dangling, {"rel": "nas:cites", "direction": "out"}, 1)
    assert count(dangling, {"rel": "nas:cites", "direction": "out", "where": active}, 0)


PART_SUM = {"rel": "inv:part_of", "direction": "in", "attr": "weight"}


def assembly(*weights: Any, **kw: Any) -> tuple[Any, DictSnapshot]:
    """An item with one part per weight (``None``: the part has no weight)."""
    item = revision("item", "inv:Item", kw.pop("attrs", {"name": "x"}))
    parts = [
        revision(
            f"p{i}",
            "inv:Item",
            {"name": f"p{i}"} if w is None else {"name": f"p{i}", "weight": w},
            [edge("inv:part_of", "item")],
        )
        for i, w in enumerate(weights)
    ]
    return item, DictSnapshot(item, *parts, *kw.pop("extra", ()))


def total(*weights: Any) -> Any:
    item, snap = assembly(*weights)
    found: Any = explain({"exists": {"sum": PART_SUM}}, item, snap, INV)
    return found[0]["value"]


@pytest.mark.parametrize(
    ("weights", "expected"),
    [
        ((), 0),
        ((1, 2), 3),
        ((-5, 2), -3),
        (("1.5", "2.25"), "3.75"),
        (("1.50", 2), "3.50"),
        (("-0.5", "0.5"), "0.0"),
        (("0.000000001", "0.000000002"), "0.000000003"),
        (
            ("123456789012345678901234567890.123456789", "0.000000001"),
            "123456789012345678901234567890.123456790",
        ),
        ((2**53 - 1, 2**53 - 1), str(2 * (2**53 - 1))),  # beyond C.1: reported as a string
        ((1, None), None),
        ((1, True), None),
        (("1e5",), None),
    ],
)
def test_sum_is_exact(weights: tuple[Any, ...], expected: Any) -> None:
    assert total(*weights) == expected


def test_exact_sum_keeps_large_integers() -> None:
    assert exact_sum([2**53 - 1, 2**53 - 1]) == 2 * (2**53 - 1)
    assert exact_sum([]) == 0


def test_sum_compares_numerically_and_exactly() -> None:
    def holds_sum(op: str, rhs: Any, *weights: Any, attrs: Any = None) -> bool:
        item, snap = assembly(*weights, attrs=attrs or {"name": "x"})
        return evaluate({op: [{"sum": PART_SUM}, rhs]}, item, snap, INV)

    assert holds_sum("eq", c(3), "1.5", "1.50")
    assert holds_sum("le", c(3), 1, 2)
    assert not holds_sum("lt", c(3), 1, 2)
    assert holds_sum("eq", c(0))
    assert holds_sum("le", a("quantity"), "1.5", 2, attrs={"quantity": 4})
    assert not holds_sum("le", a("quantity"), "2.5", 2, attrs={"quantity": 4})
    assert holds_sum("eq", a("limit"), "0.1", "0.2", attrs={"limit": "0.30"})
    assert not holds_sum("le", a("limit"), 1, attrs={})  # absent right side
    big = 2 * (2**53 - 1)
    assert holds_sum("eq", c(str(big)), 2**53 - 1, 2**53 - 1)
    assert not holds_sum("eq", c(str(big - 1)), 2**53 - 1, 2**53 - 1)


def test_absent_sum() -> None:
    item, snap = assembly(1, None)
    absent = {"sum": PART_SUM}
    assert not evaluate({"exists": absent}, item, snap, INV)
    assert not evaluate({"ge": [absent, c(0)]}, item, snap, INV)
    assert not evaluate({"ne": [absent, c(0)]}, item, snap, INV)
    assert evaluate({"exists": absent}, *assembly(1, 2), INV)
    dangling = revision("d", "inv:Item", {"name": "d"}, [edge("inv:part_of", "missing")])
    out = {"sum": {**PART_SUM, "direction": "out"}}
    assert not evaluate({"exists": out}, dangling, DictSnapshot(dangling), INV)


def test_sum_where_and_directions() -> None:
    retired = revision(
        "r",
        "inv:Item",
        {"name": "r", "weight": "9"},
        [edge("inv:part_of", "item")],
        status="retracted",
    )
    item, snap = assembly("1.5", "2", extra=[retired])
    active = {**PART_SUM, "where": {"record_status": ["active"]}}
    assert explain({"exists": {"sum": active}}, item, snap, INV) == [
        {"operand": {"sum": active}, "value": "3.5"}
    ]
    assert explain({"exists": {"sum": PART_SUM}}, item, snap, INV) == [
        {"operand": {"sum": PART_SUM}, "value": "12.5"}
    ]
    typed = {**PART_SUM, "where": {"type": ["inv:Location"]}}
    assert evaluate({"eq": [{"sum": typed}, c(0)]}, item, snap, INV)
    up = {"rel": "inv:part_of", "direction": "out", "attr": "quantity"}
    parent = revision("item", "inv:Item", {"name": "x", "quantity": 7})
    assert evaluate({"eq": [{"sum": up}, c(7)]}, retired, DictSnapshot(parent, retired), INV)


def test_explain() -> None:
    item, snap = assembly(1, 2, attrs={"name": "x", "quantity": 2})
    parts = {"count": {"rel": "inv:part_of", "direction": "in"}}
    expr = {
        "implies": [
            {"ge": [parts, c(1)]},
            {"and": [{"le": [{"sum": PART_SUM}, a("quantity")]}, {"exists": {"sum": PART_SUM}}]},
        ]
    }
    assert explain(expr, item, snap, INV) == [
        {"operand": parts, "value": 2},
        {"operand": c(1), "value": 1},
        {"operand": {"sum": PART_SUM}, "value": 3},
        {"operand": a("quantity"), "value": 2},
    ]
    assert explain({"le": [{"sum": PART_SUM}, a("limit")]}, item, snap, INV) == [
        {"operand": {"sum": PART_SUM}, "value": 3},
        {"operand": a("limit"), "value": None},
    ]
    assert explain({"eq": [a("quantity"), c(1)]}, item, snap, INV) is None


def test_ast_walkers() -> None:
    expr = {
        "implies": [
            {"ge": [{"count": {"rel": "inv:part_of", "direction": "in"}}, c(1)]},
            {"not": {"le": [{"sum": {**PART_SUM, "direction": "out"}}, {"avg": {}}]}},
        ]
    }
    assert unknown_nodes(expr) == ["avg"]
    assert unknown_nodes({"xor": [{"eq": [a("x"), c(1)]}]}) == ["xor"]
    assert unknown_nodes(rule(INV, "inv.serial_single_unit")) == []
    assert aggregate_relations(expr) == {("inv:part_of", "in"), ("inv:part_of", "out")}
    assert aggregate_relations(rule(INV, "inv.serial_single_unit")) == frozenset()


def guard(model: CompiledModel, lifecycle: str, to: str) -> Any:
    for transition in model.lifecycles[lifecycle].transitions:
        if transition.to_state == to and transition.guards:
            return transition.guards[0].expr
    raise AssertionError(f"no guarded transition to {to}")


def rule(model: CompiledModel, rule_id: str) -> Any:
    return next(r.expr for r in model.rules if r.id == rule_id)


def test_inventory_guards_and_rules() -> None:
    usable = guard(INV, "inv:ItemLifecycle", "in_use")
    assert holds(usable, {"condition": "good"})
    assert holds(usable, {})
    assert not holds(usable, {"condition": "damaged"})

    no_parts = guard(INV, "inv:ItemLifecycle", "retired")
    item = revision("item", "inv:Item", {"name": "x"})
    part = revision("part", "inv:Item", {"name": "p"}, [edge("inv:part_of", "item")])
    old_part = revision(
        "old", "inv:Item", {"name": "o"}, [edge("inv:part_of", "item")], status="superseded"
    )
    assert not evaluate(no_parts, item, DictSnapshot(item, part, old_part), INV)
    assert evaluate(no_parts, item, DictSnapshot(item, old_part), INV)

    serial = rule(INV, "inv.serial_single_unit")
    assert holds(serial, {"quantity": 5})
    assert holds(serial, {"serial_number": "SN-0001", "quantity": 1})
    assert not holds(serial, {"serial_number": "SN-0001", "quantity": 2})

    not_empty = rule(INV, "inv.location_not_empty")
    room = revision("room", "inv:Location", {"name": "r", "code": "R"})
    shelf = revision(
        "shelf", "inv:Location", {"name": "s", "code": "S"}, [edge("inv:within", "room")]
    )
    assert evaluate(not_empty, room, DictSnapshot(room, shelf), INV)
    assert not evaluate(not_empty, shelf, DictSnapshot(room, shelf), INV)


def test_inventory_parts_within_weight() -> None:
    model = compiled_model("inventory", "2.1.0")
    within = rule(model, "inv.parts_within_weight")

    def judged(weight: Any, *part_weights: Any, retired: Any = None) -> bool:
        attrs = {"label": "x"} if weight is None else {"label": "x", "weight": weight}
        item = revision("item", "inv:Item", attrs)
        parts = [
            revision(
                f"p{i}",
                "inv:Item",
                {"label": "p"} if w is None else {"label": "p", "weight": w},
                [edge("inv:part_of", "item")],
            )
            for i, w in enumerate(part_weights)
        ]
        if retired is not None:
            parts.append(
                revision(
                    "old",
                    "inv:Item",
                    {"label": "o", "weight": retired},
                    [edge("inv:part_of", "item")],
                    status="retracted",
                )
            )
        return evaluate(within, item, DictSnapshot(item, *parts), model)

    assert judged("10.0", "4.5", "5.50")  # exactly the item's weight
    assert not judged("10.0", "4.5", "5.51")  # exceeds
    assert judged("10.0", "4.5", None)  # a part without weight: not applicable
    assert judged(None, "4.5", "9")  # the item without weight: not applicable
    assert judged("1")  # no parts: not applicable
    assert judged("10", "9", retired="5")  # retracted parts do not count


def test_notes_guards_and_rules() -> None:
    body = guard(NAS, "nas:NoteLifecycle", "published")
    assert holds(body, {"body": "text"}, NAS, type="nas:Note")
    assert not holds(body, {"body": ""}, NAS, type="nas:Note")

    cites = rule(NAS, "nas.summary_cites_source")
    published = {"status": "published", "kind": "summary"}
    assert not holds(cites, published, NAS, type="nas:Note")
    assert holds(cites, published, NAS, type="nas:Note", edges=[edge("nas:cites", "s")])
    assert holds(cites, {**published, "kind": "question"}, NAS, type="nas:Note")

    orphan = rule(NAS, "nas.topic_not_orphan")
    topic = revision("t", "nas:Topic", {"label": "t"})
    narrower = revision("u", "nas:Topic", {"label": "u"}, [edge("nas:broader", "t")])
    assert evaluate(orphan, topic, DictSnapshot(topic, narrower), NAS)
    assert not evaluate(orphan, narrower, DictSnapshot(topic, narrower), NAS)

    cited = rule(NAS, "nas.source_cited")
    web = revision("w", "nas:WebPage", {"title": "w", "url": "https://x"})
    note = revision("n", "nas:Note", edges=[edge("nas:cites", "w")])
    assert evaluate(cited, web, DictSnapshot(web, note), NAS)
    assert not evaluate(cited, web, DictSnapshot(web), NAS)


@pytest.mark.parametrize(
    "expr",
    [
        {"xor": []},
        {"and": {}},
        {"implies": [c(1)]},
        {"eq": [{"weird": 1}, c(1)]},
        {"eq": [{"self": "rev"}, c(1)]},
        {"in": [a("name"), c("x")]},
        {"eq": [{"count": {"rel": "inv:part_of", "direction": "up"}}, c(0)]},
        {"eq": [{"sum": {"rel": "inv:part_of", "direction": "in"}}, c(0)]},
        {"eq": [{"sum": {"rel": "inv:part_of", "attr": "w", "where": []}}, c(0)]},
        {"eq": [{"avg": {"rel": "inv:part_of", "attr": "w"}}, c(0)]},
        [],
    ],
)
def test_unknown_nodes_are_internal_errors(expr: Any) -> None:
    with pytest.raises(GraphCliError) as info:
        holds(expr, {"name": "x"})
    assert info.value.code == "internal.error"
