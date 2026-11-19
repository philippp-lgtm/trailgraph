"""describe: views in the listings and in ``describe search`` (object views stage 2, V12)."""

from typing import Any

import pytest
from support.skills import graph_entry
from support.views import company_model, compiled_entries, edited

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.view_entries import ViewEntry
from graph_cli.services.describe import describe_view, describe_views
from graph_cli.services.schema_search import search_model

MODEL = company_model()
ENTRY = graph_entry(MODEL)
VIEWS = tuple(compiled_entries().values())


def names(body: dict[str, Any]) -> list[str]:
    return [v["name"] for v in body["views"]]


def search(text: str, **options: Any) -> dict[str, Any]:
    options.setdefault("views", VIEWS)
    body, _ = search_model(ENTRY, MODEL, text, **options)
    return body


def hits(body: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    return [(i["kind"], i["curie"], i["match"]["field"], i["match"]["how"]) for i in body["items"]]


def test_listing_hides_deprecated_and_filters_by_module() -> None:
    body = describe_views(ENTRY, MODEL, VIEWS)
    assert names(body) == [
        "members_report",
        "org_catalog",
        "position_overview",
        "tasks_by_state",
        "unit_overview",
    ]
    assert [v["module"] for v in body["views"]].count("operations") == 1
    assert "module" not in body
    every = describe_views(ENTRY, MODEL, VIEWS, include_deprecated=True)
    assert "legacy_unit_overview" in names(every)
    one = describe_views(ENTRY, MODEL, VIEWS, module="ops")
    assert names(one) == ["tasks_by_state"] and one["module"] == "operations"
    assert names(describe_views(ENTRY, MODEL, VIEWS, module="org", include_deprecated=True)) == [
        "legacy_unit_overview",
        "members_report",
        "org_catalog",
        "position_overview",
        "unit_overview",
    ]
    with pytest.raises(GraphCliError) as info:
        describe_views(ENTRY, MODEL, VIEWS, module="no_such")
    assert info.value.code == "term.unknown"


def test_a_listing_item_says_what_the_view_takes_and_whether_it_is_the_default() -> None:
    by_name = {v["name"]: v for v in describe_views(ENTRY, MODEL, VIEWS)["views"]}
    assert by_name["unit_overview"] == {
        "name": "unit_overview",
        "module": "organization",
        "summary": "A unit with its cost center, its positions in a state and who holds them.",
        "takes": "org:OrgUnit",
        "default": True,
        "signature": "status?: string",
        "status": "active",
    }
    assert "takes" not in by_name["org_catalog"] and "default" not in by_name["org_catalog"]
    assert "default" not in by_name["position_overview"]
    assert by_name["position_overview"]["takes"] == "org:Position"


def test_no_views_lists_nothing() -> None:
    assert describe_views(ENTRY, MODEL, None)["views"] == []
    assert describe_views(ENTRY, MODEL, ())["views"] == []


def test_a_view_is_described_completely() -> None:
    body = describe_view(ENTRY, MODEL, VIEWS, "unit_overview")
    assert body["schema"] == "graph_cli.model/1"
    v = body["view"]
    assert v["takes"] == "org:OrgUnit" and v["default"] is True and v["max_bytes"] == 4096
    assert v["page_bytes"] is None and v["replaced_by"] is None and v["status"] == "active"
    assert v["params"][0]["name"] == "status" and v["params"][0]["default"] == "active"
    assert [(s["kind"], s["id"]) for s in v["sections"]] == [
        ("fields", "fields"),
        ("relations", "relations"),
        ("neighbors", "positions"),
        ("rows", "members"),
        ("files", "files"),
        ("affordances", "affordances"),
    ]
    assert v["examples"][0]["question"].startswith("Which active positions")
    assert v["touches"]["functions"] == ["members_of_unit"]
    assert {"curie": "org:OrgUnit", "module": "organization"} in v["touches"]["types"]
    assert v["hash"].startswith("sha256:") and v["signature"] == "status?: string"
    assert "use_when" in v
    catalog = describe_view(ENTRY, MODEL, VIEWS, "org_catalog")["view"]
    assert catalog["takes"] is None and catalog["default"] is False


def test_replaced_by_names_only_a_view_the_reader_sees() -> None:
    both = describe_view(ENTRY, MODEL, VIEWS, "legacy_unit_overview")["view"]
    assert (both["status"], both["replaced_by"]) == ("deprecated", "unit_overview")
    visible = tuple(v for v in VIEWS if v.name != "unit_overview")
    alone = describe_view(ENTRY, MODEL, visible, "legacy_unit_overview")["view"]
    assert alone["replaced_by"] is None


def test_an_unknown_view_names_the_close_ones_among_the_given_views_only() -> None:
    with pytest.raises(GraphCliError) as info:
        describe_view(ENTRY, MODEL, VIEWS, "unit_overviews")
    assert info.value.code == "view.not_found"
    assert info.value.details["did_you_mean"][0] == "unit_overview"
    hidden = tuple(v for v in VIEWS if v.name != "unit_overview")
    with pytest.raises(GraphCliError) as info:
        describe_view(ENTRY, MODEL, hidden, "unit_overview")
    assert "unit_overview" not in info.value.details["did_you_mean"]
    with pytest.raises(GraphCliError) as info:
        describe_view(ENTRY, MODEL, None, "unit_overview")
    assert info.value.details["did_you_mean"] == []


def test_search_finds_a_view_by_name_summary_purpose_example_and_touched_terms() -> None:
    assert hits(search("org catalog", kinds=("view",)))[0] == (
        "view",
        "org_catalog",
        "name",
        "exact",
    )
    assert hits(search("catalog renderers", kinds=("view",))) == [
        ("view", "org_catalog", "summary", "phrase")
    ]
    # what it is for
    assert hits(search("policies bind", kinds=("view",))) == [
        ("view", "unit_overview", "use_when", "phrase")
    ]
    # an example question
    assert hits(search("tasks are still open this quarter", kinds=("view",))) == [
        ("view", "tasks_by_state", "use_when", "phrase")
    ]
    # the terms the view touches enrich its own text: the summary of a class it reads
    assert "unit_overview" in [i["curie"] for i in search("cost center", kinds=("view",))["items"]]


def test_a_section_title_matches_in_the_last_tier() -> None:
    titled = compiled_entries(
        edited("unit_overview.yaml", "title: Positions", "title: Staffing grid")
    )
    found = search("staffing grid", kinds=("view",), views=tuple(titled.values()))
    assert hits(found) == [("view", "unit_overview", "use_when", "phrase")]
    assert hits(search("staffing grid", kinds=("view",))) == []


def test_a_view_hit_names_its_module_and_the_deprecation() -> None:
    item = search("legacy", kinds=("view",))["items"][0]
    assert item["kind"] == "view" and item["curie"] == "legacy_unit_overview"
    assert item["of"] is None and item["module"] == "organization" and item["deprecated"] is True
    assert item["summary"] == "The old team overview; use unit_overview."


def test_views_rank_after_actions_of_the_same_name_and_only_when_given() -> None:
    body = search("unit overview")
    assert [i["kind"] for i in body["items"] if i["curie"] == "unit_overview"] == ["view"]
    without, _ = search_model(ENTRY, MODEL, "unit overview")
    assert all(i["kind"] != "view" for i in without["items"])
    assert search("unit overview", views=None)["items"] == without["items"]


def test_the_kind_filter_keeps_views_apart() -> None:
    only = search("overview", kinds=("view",))
    assert {i["kind"] for i in only["items"]} == {"view"}
    none = search("overview", kinds=("function",))
    assert all(i["kind"] == "function" for i in none["items"])


def test_only_the_given_views_are_listed_and_found() -> None:
    visible: tuple[ViewEntry, ...] = tuple(v for v in VIEWS if v.entity)
    assert names(describe_views(ENTRY, MODEL, visible)) == ["position_overview", "unit_overview"]
    found = search("overview", views=visible, kinds=("view",))
    assert [i["curie"] for i in found["items"]] == [
        "unit_overview",
        "position_overview",
        "legacy_unit_overview",
    ]
    assert "org_catalog" not in [i["curie"] for i in search("catalog", views=visible)["items"]]
