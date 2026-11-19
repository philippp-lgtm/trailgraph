"""The entries of a compiled view catalog: typed reads, listing and detail shapes."""

import copy

import pytest
from support.views import company_model, function_entries, valid_sources

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kinetic.entries import signature
from graph_cli.kinetic.view_compile import compile_views
from graph_cli.kinetic.view_entries import find_view, load_view_entries
from graph_cli.kinetic.view_format import ViewDef

MODEL = company_model()
DOCUMENT, PROBLEMS = compile_views(valid_sources(), MODEL, function_entries(MODEL))
ENTRIES = load_view_entries(DOCUMENT)
BY_NAME = {e.name: e for e in ENTRIES}


def test_the_catalog_reads_back_in_order() -> None:
    assert PROBLEMS == []
    assert [e.name for e in ENTRIES] == sorted(n.removesuffix(".yaml") for n in valid_sources())
    raw = {e["name"]: e for e in DOCUMENT["views"]}
    for entry in ENTRIES:
        assert entry.hash == raw[entry.name]["hash"]
        assert entry.defn.to_json()["summary"] == raw[entry.name]["summary"]
        assert entry.defn.to_json()["sections"] == raw[entry.name]["sections"]
        assert entry.source.to_json() == raw[entry.name]["source"]


def test_an_entry_keeps_the_normalized_and_the_authored_definition() -> None:
    entry = BY_NAME["position_overview"]
    assert isinstance(entry.defn, ViewDef) and isinstance(entry.source, ViewDef)
    assert entry.defn.sections[1].to_json()["relation"] == "org:in_unit"
    assert entry.source.sections[1].to_json()["relation"] == "in_unit"
    assert entry.module == "organization"
    assert entry.entity and entry.takes == "org:Position"
    assert not BY_NAME["org_catalog"].entity and BY_NAME["org_catalog"].takes is None


def test_an_entry_has_touches() -> None:
    entry = BY_NAME["unit_overview"]
    assert [t.curie for t in entry.types] == ["org:OrgUnit"]
    assert entry.relations[0].curie == "ops:applies_to"
    assert entry.relations[0].module == "operations"
    assert entry.attributes == ("alt_labels", "name", "status", "title", "work_email")
    assert entry.functions == ("members_of_unit",)


def test_the_signature_marks_optional_parameters() -> None:
    assert BY_NAME["unit_overview"].signature == "status?: string"
    assert BY_NAME["members_report"].signature == "unit: ref org:Department"
    assert BY_NAME["position_overview"].signature == ""
    assert BY_NAME["tasks_by_state"].signature == "status?: string, before?: date"
    assert BY_NAME["tasks_by_state"].signature == signature(BY_NAME["tasks_by_state"].defn.params)


def test_summary_shapes() -> None:
    entity = BY_NAME["unit_overview"].summary_json()
    assert list(entity) == ["name", "module", "summary", "takes", "default", "signature", "status"]
    assert (entity["takes"], entity["default"]) == ("org:OrgUnit", True)
    plain = BY_NAME["position_overview"].summary_json()
    assert list(plain) == [
        "name",
        "module",
        "summary",
        "takes",
        "signature",
        "status",
    ]  # no default
    sets = BY_NAME["org_catalog"].summary_json()
    assert list(sets) == ["name", "module", "summary", "signature", "status"]  # no takes
    assert BY_NAME["legacy_unit_overview"].summary_json()["status"] == "deprecated"
    assert BY_NAME["legacy_unit_overview"].deprecated and not BY_NAME["unit_overview"].deprecated


def test_detail_shape() -> None:
    entry = BY_NAME["unit_overview"]
    detail = entry.detail_json()
    assert detail["hash"] == entry.hash
    assert detail["takes"] == "org:OrgUnit" and detail["default"] is True
    assert [s["kind"] for s in detail["sections"]] == [
        "fields",
        "relations",
        "neighbors",
        "rows",
        "files",
        "affordances",
    ]
    assert set(detail["touches"]) == {"types", "relations", "attributes", "functions"}
    assert detail["examples"] == [
        {"question": e.question, "params": dict(e.params)} for e in entry.defn.examples
    ]
    assert detail["params"][0]["name"] == "status"
    assert detail["max_bytes"] == 4096 and detail["page_bytes"] is None
    assert BY_NAME["org_catalog"].detail_json()["page_bytes"] == 65536


def test_find_view_suggests_close_names() -> None:
    assert find_view(ENTRIES, "org_catalog") is BY_NAME["org_catalog"]
    with pytest.raises(GraphCliError) as exc:
        find_view(ENTRIES, "org_catalg")
    assert exc.value.code == "view.not_found"
    assert exc.value.details["did_you_mean"] == ["org_catalog"]
    with pytest.raises(GraphCliError):
        find_view(ENTRIES, "card")  # the generated card is not a catalog entry
    with pytest.raises(GraphCliError) as exc:
        find_view(None, "x")
    assert exc.value.details["did_you_mean"] == []


def test_a_document_of_another_shape_is_refused() -> None:
    broken = copy.deepcopy(DOCUMENT)
    del broken["views"][0]["touches"]
    with pytest.raises(JsonShapeError):
        load_view_entries(broken)
    broken = copy.deepcopy(DOCUMENT)
    broken["views"][0]["sections"][0]["kind"] = "nonsense"
    with pytest.raises(ValueError, match="nonsense"):
        load_view_entries(broken)
