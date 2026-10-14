"""Expression evaluator against the AST nodes and every fixture guard and rule (B7.15)."""

from typing import Any

import pytest
from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, revision

from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.evaluate import evaluate
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
        [],
    ],
)
def test_unknown_nodes_are_internal_errors(expr: Any) -> None:
    with pytest.raises(GraphCliError) as info:
        holds(expr, {"name": "x"})
    assert info.value.code == "internal.error"
