"""describe: functions in the listings and in ``describe search`` (plan K8, K9, K10)."""

import json
from typing import Any

import pytest
from support.fixtures import GOLDENS, assert_golden, compiled_model
from support.functions import valid_sources
from support.skills import graph_entry

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.services.describe import (
    describe_function,
    describe_functions,
    describe_module,
    describe_modules,
)
from graph_cli.services.schema_search import SEARCH_TERM_KINDS, search_model

MODEL = compiled_model("company", "1.1.0")
ENTRY = graph_entry(MODEL)


def entries() -> tuple[FunctionEntry, ...]:
    document, problems = compile_catalog(valid_sources(), MODEL)
    assert problems == []
    return load_entries(document)


FUNCTIONS = entries()


def names(body: dict[str, Any]) -> list[str]:
    return [f["name"] for f in body["functions"]]


def search(text: str, **options: Any) -> dict[str, Any]:
    options.setdefault("functions", FUNCTIONS)
    body, _ = search_model(ENTRY, MODEL, text, **options)
    return body


def hits(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(i["curie"], i["match"]["field"], i["match"]["how"]) for i in body["items"]]


def test_listing_hides_deprecated_and_filters_by_module() -> None:
    body = describe_functions(ENTRY, MODEL, FUNCTIONS)
    assert names(body) == [
        "guidelines_in_effect",
        "members_of_unit",
        "missing_required_trainings",
        "people_by_email_text",
        "training_completions",
        "vacant_positions",
    ]
    assert "module" not in body
    every = describe_functions(ENTRY, MODEL, FUNCTIONS, include_deprecated=True)
    assert "unit_members" in names(every)
    one = describe_functions(ENTRY, MODEL, FUNCTIONS, module="org", include_deprecated=True)
    assert names(one) == ["members_of_unit", "unit_members", "vacant_positions"]
    assert one["module"] == "organization"
    first = one["functions"][0]
    assert first == {
        "name": "members_of_unit",
        "module": "organization",
        "summary": "People holding a position in a unit or in any unit below it.",
        "signature": "unit: ref org:OrgUnit",
        "status": "active",
    }
    with pytest.raises(GraphCliError) as info:
        describe_functions(ENTRY, MODEL, FUNCTIONS, module="no_such")
    assert info.value.code == "term.unknown"


def test_signature_marks_optional_and_list_parameters() -> None:
    by_name = {f["name"]: f for f in describe_functions(ENTRY, MODEL, FUNCTIONS)["functions"]}
    assert by_name["training_completions"]["signature"] == ("titles: list of string, since?: date")
    assert by_name["guidelines_in_effect"]["signature"] == "unit: ref org:OrgUnit, date: date"


def test_no_functions_lists_nothing() -> None:
    assert describe_functions(ENTRY, MODEL, None)["functions"] == []


def test_a_function_is_described_completely() -> None:
    body = describe_function(ENTRY, MODEL, FUNCTIONS, "members_of_unit")
    assert body["schema"] == "graph_cli.model/1"
    f = body["function"]
    assert f["status"] == "active" and f["featured"] is True and f["replaced_by"] is None
    assert f["params"][0]["class"] == "org:OrgUnit"
    assert [r["column"] for r in f["returns"]] == ["person", "position", "unit"]
    assert f["examples"][0]["question"].startswith("Who works")
    assert f["touches"]["relations"] == [
        {"curie": "org:holds", "module": "organization"},
        {"curie": "org:in_unit", "module": "organization"},
        {"curie": "org:part_of", "module": "organization"},
    ]
    assert "`org:OrgUnit`" in f["query"] and f["hash"].startswith("sha256:")
    assert "use_when" in f and "signature" in f
    deprecated = describe_function(ENTRY, MODEL, FUNCTIONS, "unit_members")["function"]
    assert (deprecated["status"], deprecated["replaced_by"]) == ("deprecated", "members_of_unit")


def test_an_unknown_function_names_the_close_ones() -> None:
    with pytest.raises(GraphCliError) as info:
        describe_function(ENTRY, MODEL, FUNCTIONS, "member_of_unit")
    assert info.value.code == "function.not_found"
    assert info.value.details["did_you_mean"][0] == "members_of_unit"
    with pytest.raises(GraphCliError) as info:
        describe_function(ENTRY, MODEL, None, "members_of_unit")
    assert info.value.details["did_you_mean"] == []


def test_modules_and_module_carry_functions_only_when_given() -> None:
    plain = describe_modules(ENTRY, MODEL)
    assert all("functions" not in m for m in plain["modules"])
    assert "functions" not in describe_module(ENTRY, MODEL, "org")["module"]
    counted = {
        m["id"]: m["functions"] for m in describe_modules(ENTRY, MODEL, FUNCTIONS)["modules"]
    }
    assert counted == {"company": 0, "core": 0, "operations": 3, "organization": 2, "people": 1}
    section = describe_module(ENTRY, MODEL, "org", FUNCTIONS)["module"]["functions"]
    assert section == [
        {
            "name": "members_of_unit",
            "summary": "People holding a position in a unit or in any unit below it.",
            "signature": "unit: ref org:OrgUnit",
        },
        {
            "name": "vacant_positions",
            "summary": "Positions in a unit or below it that nobody holds.",
            "signature": "unit: ref org:OrgUnit",
        },
    ]
    assert describe_module(ENTRY, MODEL, "company", FUNCTIONS)["module"]["functions"] == []


def test_function_is_the_last_search_kind() -> None:
    assert SEARCH_TERM_KINDS[-1] == "function"  # existing tie-breaks stay


def test_name_tiers_split_words_on_underscores() -> None:
    assert hits(search("members of unit", kinds=("function",)))[0] == (
        "members_of_unit",
        "name",
        "exact",
    )
    assert hits(search("members", kinds=("function",)))[0] == ("members_of_unit", "name", "prefix")
    assert hits(search("unit members", kinds=("function",)))[0] == (
        "unit_members",
        "name",
        "exact",
    )
    assert ("people_by_email_text", "name", "word") in hits(search("email", kinds=("function",)))
    assert hits(search("byemail", kinds=("function",)))[0] == (
        "people_by_email_text",
        "name",
        "contains",
    )


def test_summary_is_tier_four_and_purpose_text_tier_five() -> None:
    found = hits(search("work email", kinds=("function",)))
    assert found == [("people_by_email_text", "summary", "phrase")]
    found = hits(search("headcount", kinds=("function",)))
    assert found == [("members_of_unit", "use_when", "phrase")]
    # example questions and parameter names count as the purpose text
    assert hits(search("infrastructure department", kinds=("function",))) == [
        ("members_of_unit", "use_when", "phrase")
    ]
    assert ("training_completions", "use_when", "phrase") in hits(
        search("titles", kinds=("function",))
    )


def test_names_labels_and_summaries_of_touched_terms_match() -> None:
    # nothing in the function's own text says "org chart"; the summary of org:Position does
    body = search("slot in the org chart", kinds=("function",))
    assert body["items"]
    assert {i["match"]["field"] for i in body["items"]} == {"touches"}
    assert "vacant_positions" in [i["curie"] for i in body["items"]]
    # the local name of a touched relation
    assert ("members_of_unit", "touches", "phrase") in hits(search("holds", kinds=("function",)))


def test_a_function_hit_has_the_shape_of_the_others() -> None:
    item = search("headcount", kinds=("function",))["items"][0]
    assert item == {
        "kind": "function",
        "curie": "members_of_unit",
        "of": None,
        "module": "organization",
        "summary": "People holding a position in a unit or in any unit below it.",
        "deprecated": False,
        "match": {"field": "use_when", "how": "phrase"},
    }
    old = search("unit members", kinds=("function",))["items"][0]
    assert old["deprecated"] is True


def test_functions_follow_the_module_filter_and_come_after_terms_of_a_tier() -> None:
    body = search("unit", module="people")
    assert [i["kind"] for i in body["items"]] == [] or all(
        i["module"] == "people" for i in body["items"]
    )
    mixed = search("position")
    kinds = [i["kind"] for i in mixed["items"]]
    assert kinds.index("type") < kinds.index("function")
    org = search("holder", kinds=("function",), module="organization")
    assert all(i["module"] == "organization" for i in org["items"])


def test_searching_without_functions_is_unchanged() -> None:
    without, total = search_model(ENTRY, MODEL, "position")
    assert all(i["kind"] != "function" for i in without["items"])
    assert search("position", functions=None)["items"] == without["items"]
    assert search("headcount", functions=None)["items"] == []
    assert total == len(without["items"])


def test_goldens() -> None:
    def golden(name: str, document: dict[str, Any]) -> None:
        data = json.loads(json.dumps(document))
        del data["graph"]["id"]
        assert_golden(
            GOLDENS / "describe" / f"{name}.json", (json.dumps(data, indent=2) + "\n").encode()
        )

    golden("functions_company_1.1.0", describe_functions(ENTRY, MODEL, FUNCTIONS))
    golden(
        "function_company_1.1.0_members_of_unit",
        describe_function(ENTRY, MODEL, FUNCTIONS, "members_of_unit"),
    )
    golden("modules_company_1.1.0_functions", describe_modules(ENTRY, MODEL, FUNCTIONS))
    golden(
        "module_company_organization_1.1.0_functions",
        describe_module(ENTRY, MODEL, "organization", FUNCTIONS),
    )
    golden("search_company_1.1.0_headcount", search("headcount"))
