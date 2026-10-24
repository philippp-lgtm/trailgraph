"""find and neighbors planning (contracts B13.13, B8.13; plan A1)."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from support.fixtures import compiled_model

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.query_ast import And, Cmp, NeighborQuery, Not, OrderKey, RelConstraint
from graph_cli.query.plan import FindSpec, parse_rel_flag, plan_find, plan_neighbors

INV = compiled_model("inventory")
NAS = compiled_model("notes_and_sources")
ID = EntityId(UUID("0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"))


def where(model: Any, *texts: str, types: tuple[str, ...] = ()) -> Any:
    return plan_find(model, FindSpec(types=types, where=texts)).where


def test_types_expand_to_subclasses() -> None:
    query = plan_find(NAS, FindSpec(types=("nas:Source",)))
    assert query.types == ("nas:Book", "nas:Source", "nas:WebPage")
    assert query.record_status == {"active"} and query.limit == 50
    assert plan_find(NAS, FindSpec()).types == ()
    assert plan_find(NAS, FindSpec(include_inactive=True)).record_status == {
        "active",
        "superseded",
        "retracted",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("quantity=3", Cmp("quantity", "eq", 3)),
        ("quantity:ge=2", Cmp("quantity", "ge", 2)),
        ("name:ne=Desk", Cmp("name", "ne", "Desk")),
        ("name:prefix=De", Cmp("name", "prefix", "De")),
        ("name:contains=es", Cmp("name", "contains", "es")),
        ("condition:in=new,good", Cmp("condition", "in", ["new", "good"])),
        ("serial_number:exists=false", Cmp("serial_number", "exists", False)),
        ("acquired_on:lt=2026-01-02", Cmp("acquired_on", "lt", "2026-01-02")),
        ("record_status=superseded", Cmp("record_status", "eq", "superseded")),
        ("rev:gt=1", Cmp("rev", "gt", 1)),
        ("name=a=b", Cmp("name", "eq", "a=b")),
    ],
)
def test_where_ops_and_coercion(text: str, expected: Cmp) -> None:
    assert where(INV, text, types=("inv:Item",)) == expected


def test_several_where_are_anded() -> None:
    assert where(INV, "quantity=1", "name=x") == And(
        (Cmp("quantity", "eq", 1), Cmp("name", "eq", "x"))
    )


def test_decimal_and_many_valued_fields() -> None:
    cls = INV.classes["inv:Item"]
    price = replace(cls.attributes["name"], type="decimal", many=False)
    tags = replace(cls.attributes["name"], many=True)
    model = replace(
        INV,
        classes={
            **INV.classes,
            "inv:Item": replace(cls, attributes={**cls.attributes, "price": price, "tags": tags}),
        },
    )
    assert where(model, "price:gt=9.50") == Cmp("price", "gt", "9.50", numeric=True)
    assert where(model, "tags=red") == Cmp("tags", "contains", "red")
    assert where(model, "tags:ne=red") == Not(Cmp("tags", "contains", "red"))
    sort = plan_find(model, FindSpec(sort=("price:desc", "name"))).order
    assert sort == (OrderKey("price", True, True), OrderKey("name"))


@pytest.mark.parametrize(
    ("spec", "code", "kind"),
    [
        (FindSpec(types=("inv:Iten",)), "term.unknown", "type"),
        (FindSpec(where=("colour=red",)), "term.unknown", "field"),
        (FindSpec(types=("inv:Location",), where=("quantity=1",)), "term.unknown", "field"),
        (FindSpec(rels=(("inv:stored_in", ()),)), "term.unknown", "relation"),
        (FindSpec(where=("quantity:approx=1",)), "usage.invalid_argument", None),
        (FindSpec(where=("quantity=many",)), "usage.invalid_argument", None),
        (FindSpec(where=("quantity",)), "usage.invalid_argument", None),
        (FindSpec(where=("serial_number:exists=maybe",)), "usage.invalid_argument", None),
        (FindSpec(sort=("name:sideways",)), "usage.invalid_argument", None),
        (FindSpec(limit=501), "usage.invalid_argument", None),
        (FindSpec(limit=0), "usage.invalid_argument", None),
        (FindSpec(offset=-1), "usage.invalid_argument", None),
    ],
)
def test_errors(spec: FindSpec, code: str, kind: str | None) -> None:
    with pytest.raises(GraphCliError) as info:
        plan_find(INV, spec)
    assert info.value.code == code
    if kind is not None:
        assert info.value.details["kind"] == kind
        assert isinstance(info.value.details["did_you_mean"], list)


def test_did_you_mean() -> None:
    with pytest.raises(GraphCliError) as info:
        plan_find(INV, FindSpec(types=("inv:Iten",)))
    assert info.value.details["did_you_mean"][0] == "inv:Item"  # type: ignore[index]


def test_a_unique_local_name_stands_for_its_curie() -> None:
    assert (
        plan_find(NAS, FindSpec(types=("Source",))).types
        == plan_find(NAS, FindSpec(types=("nas:Source",))).types
    )
    query = plan_find(INV, FindSpec(rels=(("located_in", (ID,)),)))
    assert query.rels[0].rel == TermRef("inv:located_in")
    assert plan_neighbors(INV, ID, rels=("located_in",)).rels == (TermRef("inv:located_in"),)


def test_an_ambiguous_local_name_lists_the_candidates() -> None:
    model = replace(NAS, classes={**NAS.classes, "provenance:Note": NAS.classes["nas:Note"]})
    with pytest.raises(GraphCliError) as info:
        plan_find(model, FindSpec(types=("Note",)))
    assert info.value.code == "query.ambiguous_term"
    assert info.value.details["candidates"] == ["nas:Note", "provenance:Note"]
    assert len(plan_find(model, FindSpec(types=("provenance:Note",))).types) == 1


def test_relations() -> None:
    query = plan_find(
        INV, FindSpec(rels=(("inv:located_in", (ID,)),), in_rels=(("inv:part_of", ()),))
    )
    assert query.rels == (
        RelConstraint("inv:located_in", "out", (ID,)),  # type: ignore[arg-type]
        RelConstraint("inv:part_of", "in", ()),  # type: ignore[arg-type]
    )
    assert parse_rel_flag("inv:part_of=ITEM-0001", "--rel") == ("inv:part_of", ("ITEM-0001",))
    assert parse_rel_flag("inv:part_of=ITEM-0001,ITEM-0002", "--rel") == (
        "inv:part_of",
        ("ITEM-0001", "ITEM-0002"),
    )
    assert parse_rel_flag("inv:part_of", "--rel") == ("inv:part_of", ())
    for bad in ("inv:part_of=", "inv:part_of=ITEM-0001,", "inv:part_of=,ITEM-0001", "=ITEM-0001"):
        with pytest.raises(GraphCliError):
            parse_rel_flag(bad, "--rel")


def test_neighbors() -> None:
    assert plan_neighbors(INV, ID) == NeighborQuery(ID, None, "both", 1)
    query = plan_neighbors(INV, ID, rels=["inv:part_of"], direction="in", depth=3)
    assert query == NeighborQuery(ID, ("inv:part_of",), "in", 3)  # type: ignore[arg-type]
    for kwargs in ({"depth": 4}, {"direction": "up"}, {"rels": ["inv:nope"]}):
        with pytest.raises(GraphCliError):
            plan_neighbors(INV, ID, **kwargs)  # type: ignore[arg-type]


NAS_LANG = compiled_model("notes_and_sources", "1.2.0")
GLOSSARY = ("nas:GlossaryEntry",)


def test_language_tagged_fields() -> None:
    assert where(NAS_LANG, "usage_hint=selten", types=GLOSSARY) == Cmp(
        "usage_hint", "eq", "selten", tagged=True
    )
    assert where(NAS_LANG, 'usage_hint="selten"@DE', types=GLOSSARY) == Cmp(
        "usage_hint", "eq", {"lang": "de", "value": "selten"}, tagged=True
    )
    assert where(NAS_LANG, "usage_hint:contains=sel@de", types=GLOSSARY) == Cmp(
        "usage_hint", "contains", "sel@de", tagged=True
    )
    assert where(NAS_LANG, "heading=SuK@de", types=GLOSSARY) == Cmp(
        "heading", "contains", {"lang": "de", "value": "SuK"}, tagged=True
    )
    assert where(NAS_LANG, "heading:lang=de-CH", types=GLOSSARY) == Cmp("heading", "lang", "de-CH")
    assert where(NAS_LANG, "heading:lang=*", types=GLOSSARY) == Cmp("heading", "lang", "*")
    sort = plan_find(NAS_LANG, FindSpec(types=GLOSSARY, sort=("usage_hint:desc",))).order
    assert sort == (OrderKey("usage_hint", descending=True, tagged=True),)


@pytest.mark.parametrize("text", ["heading:lang=de_CH", "heading:lang=", "usage_hint:lang=x-foo"])
def test_lang_needs_a_language_range(text: str) -> None:
    with pytest.raises(GraphCliError) as info:
        where(NAS_LANG, text, types=GLOSSARY)
    assert info.value.code == "usage.invalid_argument"


def test_lang_needs_a_language_tagged_field() -> None:
    with pytest.raises(GraphCliError) as info:
        where(NAS, "label:lang=de", types=("nas:Topic",))
    assert "not language-tagged" in info.value.message
