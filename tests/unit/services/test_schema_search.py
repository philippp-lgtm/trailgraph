"""describe search: ranking, kinds, module filter, folding and paging over the compiled model
(plan ``modules_for_agents`` step 4)."""

import json
from typing import Any

import pytest
from support.fixtures import GOLDENS, assert_golden, compiled_model
from support.skills import graph_entry

from graph_cli.kernel.errors import GraphCliError
from graph_cli.services.schema_search import SEARCH_TERM_KINDS, search_model


def search(name: str, version: str, text: str, **options: Any) -> tuple[dict[str, Any], int]:
    model = compiled_model(name, version)
    body, total = search_model(graph_entry(model), model, text, **options)
    return body, total


def curies(body: dict[str, Any]) -> list[str]:
    return [item["curie"] for item in body["items"]]


def test_an_exact_name_comes_first_and_names_how_it_matched() -> None:
    body, total = search("company", "1.1.0", "position")
    first = body["items"][0]
    assert first["curie"] == "org:Position" and first["kind"] == "type"
    assert first["module"] == "organization" and first["of"] is None
    assert first["match"] == {"field": "name", "how": "exact"}
    assert first["deprecated"] is False and first["summary"]
    assert total == len(body["items"]) == 12
    assert all(i["match"] == {"field": "summary", "how": "phrase"} for i in body["items"][1:11])


def test_tiers_are_exact_prefix_word_contains_summary_description() -> None:
    body, _ = search("company", "1.1.0", "report")
    assert curies(body) == ["org:reports_to"]
    assert body["items"][0]["match"] == {"field": "name", "how": "prefix"}
    body, _ = search("company", "1.1.0", "center")
    first = body["items"][0]
    assert first["curie"] == "org:CostCenter" and first["match"]["how"] == "word"
    body, _ = search("company", "1.1.0", "ostcen")
    assert body["items"][0]["match"] == {"field": "name", "how": "contains"}
    body, _ = search("company", "1.1.0", "accounting bucket")
    assert curies(body) == ["org:CostCenter"]
    assert body["items"][0]["match"] == {"field": "summary", "how": "phrase"}
    assert any(
        i["match"]["field"] == "description"
        for i in search("company", "1.1.0", "position")[0]["items"]
    )


def test_a_name_is_read_as_words_and_without_spaces() -> None:
    for text in ("cost center", "CostCenter", "cost_center", "COST-CENTER"):
        body, _ = search("company", "1.1.0", text)
        assert body["items"][0]["curie"] == "org:CostCenter", text
        assert body["items"][0]["match"] == {"field": "name", "how": "exact"}, text
    body, _ = search("company", "1.1.0", "reports to")
    assert body["items"][0]["curie"] == "org:reports_to"


def test_a_curie_is_matched_whole_or_by_prefix() -> None:
    body, _ = search("company", "1.1.0", "org:Position")
    assert body["items"][0]["curie"] == "org:Position"
    assert body["items"][0]["match"] == {"field": "curie", "how": "exact"}
    body, _ = search("company", "1.1.0", "org:Pos")
    assert curies(body)[0] == "org:Position"
    assert all(i["match"]["field"] == "curie" for i in body["items"])
    assert "org:PositionLifecycle.planned" in curies(body)
    # a bare module word is not a CURIE: it does not list the whole module
    assert search("company", "1.1.0", "org")[1] < 10


def test_every_kind_is_found_and_names_its_owner() -> None:
    body, _ = search("inventory", "1.1.0", "refurbished")
    assert body["items"][0] == {
        "kind": "enum_value",
        "curie": "inv:Condition.refurbished",
        "of": "inv:Condition",
        "module": "inventory",
        "summary": "Repaired to working order.",
        "deprecated": False,
        "match": {"field": "name", "how": "exact"},
    }
    state = search("inventory", "1.1.0", "in_stock")[0]["items"][0]
    assert (state["kind"], state["curie"], state["of"]) == (
        "state",
        "inv:ItemLifecycle.in_stock",
        "inv:ItemLifecycle",
    )
    attribute = search("inventory", "1.1.0", "serial_number")[0]["items"][0]
    assert (attribute["kind"], attribute["curie"], attribute["of"]) == (
        "attribute",
        "inv:Item.serial_number",
        "inv:Item",
    )
    relation = search("inventory", "1.1.0", "located_in")[0]["items"][0]
    assert (relation["kind"], relation["curie"]) == ("relation", "inv:located_in")


def test_an_inherited_attribute_is_listed_once_under_its_declaring_class() -> None:
    body, _ = search("company", "1.1.0", "alt_labels")
    declared = [c for c in curies(body) if c.endswith(".alt_labels")]
    assert "org:OrgUnit.alt_labels" in declared and "org:Position.alt_labels" in declared
    assert "org:Team.alt_labels" not in declared and "org:Department.alt_labels" not in declared
    assert len(declared) == len(set(declared))


def test_kind_and_module_narrow_the_search() -> None:
    body, total = search("company", "1.1.0", "name", kinds=("attribute",))
    assert {i["kind"] for i in body["items"]} == {"attribute"} and total == len(body["items"])
    body, _ = search("company", "1.1.0", "name", kinds=("type", "relation"))
    assert {i["kind"] for i in body["items"]} <= {"type", "relation"}
    body, _ = search("company", "1.1.0", "name", module="ppl")
    assert body["module"] == "people"
    assert {i["module"] for i in body["items"]} == {"people"}
    both, _ = search("company", "1.1.0", "position", module="operations")
    assert {i["module"] for i in both["items"]} == {"operations"}
    assert "org:Position" not in curies(both)
    assert "module" not in search("company", "1.1.0", "name")[0]


def test_case_and_accents_do_not_matter() -> None:
    plain = search("inventory", "1.1.0", "item")[0]["items"]
    for text in ("ITEM", "Ítem", "iTém"):
        assert search("inventory", "1.1.0", text)[0]["items"] == plain, text


def test_no_hits() -> None:
    body, total = search("company", "1.1.0", "zzzqqq")
    assert body["items"] == [] and total == 0
    assert search("company", "1.1.0", "position", module="people", kinds=("state",))[1] == 0


def test_paging_is_stable_and_complete() -> None:
    everything, total = search("company", "1.1.0", "position", limit=500)
    assert total == len(everything["items"])
    pages: list[str] = []
    for offset in range(0, total, 5):
        body, again = search("company", "1.1.0", "position", limit=5, offset=offset)
        assert again == total and len(body["items"]) <= 5
        pages += curies(body)
    assert pages == curies(everything)
    assert search("company", "1.1.0", "position", offset=total)[0]["items"] == []


def test_a_single_module_graph_has_one_module_in_every_hit() -> None:
    body, _ = search("notes_and_sources", "1.0.0", "note", limit=500)
    assert body["items"] and {i["module"] for i in body["items"]} == {"notes_and_sources"}


@pytest.mark.parametrize(
    ("text", "options"),
    [("a", {}), (" - ", {}), ("name", {"limit": 0}), ("name", {"offset": -1})],
)
def test_bad_arguments_are_usage_errors(text: str, options: dict[str, Any]) -> None:
    with pytest.raises(GraphCliError) as caught:
        search("company", "1.1.0", text, **options)
    assert caught.value.code == "usage.invalid_argument"


def test_unknown_kind_and_module() -> None:
    with pytest.raises(GraphCliError) as caught:
        search("company", "1.1.0", "name", kinds=("field",))
    assert caught.value.code == "usage.invalid_argument"
    with pytest.raises(GraphCliError) as unknown:
        search("company", "1.1.0", "name", module="orgs")
    assert unknown.value.code == "term.unknown" and unknown.value.details["kind"] == "module"


def test_the_kinds_are_the_declared_ones() -> None:
    assert SEARCH_TERM_KINDS == ("type", "relation", "attribute", "enum_value", "state")


@pytest.mark.parametrize(
    ("name", "version", "text"),
    [("company", "1.1.0", "position"), ("inventory", "1.1.0", "condition")],
)
def test_search_goldens(name: str, version: str, text: str) -> None:
    body, _ = search(name, version, text, limit=500)
    data = json.loads(json.dumps(body))
    del data["graph"]["id"]
    assert_golden(
        GOLDENS / "describe" / f"search_{name}_{version}_{text}.json",
        (json.dumps(data, indent=2) + "\n").encode(),
    )
