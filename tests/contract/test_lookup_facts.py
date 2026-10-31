"""``lookup`` facts on both backends over the company toy ontology (context-aware lookup,
step 2): a name with several candidates gets up to three out-edges of functional relations
per candidate, required ones first; a name with one candidate gets none."""

from pathlib import Path
from typing import Any

import pytest
from support.services import ServiceEnv

from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.lookup import LookupSpec
from graph_cli.services.read import GraphRead, ReadService

REQUEST = RequestMeta("lookup", "sha256:" + "0" * 64)


@pytest.fixture(params=("company",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    assert env.apply_seed().error is None
    # Two teams and two positions of the same name, told apart by where they belong.
    env.apply(
        {
            "op": "create",
            "type": "org:CostCenter",
            "local": "cc",
            "attrs": {"code": "CC-900", "name": "Field costs"},
        },
        {"op": "create", "type": "org:Department", "local": "north", "attrs": {"name": "North"}},
        {"op": "create", "type": "org:Department", "local": "south", "attrs": {"name": "South"}},
        {
            "op": "create",
            "type": "org:Team",
            "local": "support_north",
            "attrs": {"name": "Field support"},
            "edges": [
                {"rel": "org:part_of", "target": "@north"},
                {"rel": "org:charged_to", "target": "@cc"},
            ],
        },
        {
            "op": "create",
            "type": "org:Team",
            "local": "support_south",
            "attrs": {"name": "Field support"},
            "edges": [{"rel": "org:part_of", "target": "@south"}],
        },
        {
            "op": "create",
            "type": "org:Position",
            "local": "lead_north",
            "attrs": {"title": "Field lead"},
            "edges": [{"rel": "org:in_unit", "target": "@support_north"}],
        },
        {
            "op": "create",
            "type": "org:Position",
            "local": "lead_south",
            "attrs": {"title": "Field lead"},
            "edges": [
                {"rel": "org:reports_to", "target": "@lead_north"},
                {"rel": "org:in_unit", "target": "@support_south"},
            ],
        },
    )
    return env


def open_view(svc: ServiceEnv, reader: ReadService, **changes: Any) -> GraphRead:
    return reader.open(svc.actor, svc.catalog.resolve(svc.name), "read.find", REQUEST, **changes)


def facts(item: Any) -> list[tuple[str, str | None]] | None:
    found = item["facts"]
    return None if found is None else [(f["rel"], f["label"]) for f in found]


def test_candidates_of_one_name_are_told_apart(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    data, _ = reader.lookup(open_view(svc, reader), LookupSpec(text="Field support"))
    assert sorted(facts(i) for i in data["items"]) == [  # type: ignore[union-attr, type-var]
        [("org:charged_to", "Field costs"), ("org:part_of", "North")],
        [("org:part_of", "South")],
    ]
    for item in data["items"]:  # type: ignore[union-attr]
        for fact in item["facts"]:  # type: ignore[index]
            assert fact["alias"] and fact["id"]  # type: ignore[index]


def test_required_relations_come_first(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    data, _ = reader.lookup(open_view(svc, reader), LookupSpec(text="Field lead"))
    assert sorted(facts(i) for i in data["items"]) == [  # type: ignore[union-attr, type-var]
        [("org:in_unit", "Field support")],
        [("org:in_unit", "Field support"), ("org:reports_to", "Field lead")],
    ]


def test_a_name_with_one_candidate_has_no_facts(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    data, _ = reader.lookup(view, LookupSpec(text="North"))
    assert [facts(i) for i in data["items"]] == [None]  # type: ignore[union-attr]
    # one call, two names: only the ambiguous one gets facts
    data, _ = reader.lookup(view, LookupSpec(text="x"), ["North", "Field support"])
    by_text = {(i["text"], facts(i) is None) for i in data["items"]}  # type: ignore[union-attr, index]
    assert by_text == {("North", True), ("Field support", False)}


def test_the_filter_decides_whether_a_name_is_ambiguous(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    data, _ = reader.lookup(view, LookupSpec(text="Field", match="prefix"), for_use="org:in_unit")
    assert {i["type"] for i in data["items"]} == {"org:Team"}  # type: ignore[union-attr, index]
    assert all(i["facts"] is not None for i in data["items"])  # type: ignore[union-attr, index]


def test_facts_follow_as_of(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    seq = svc.head_seq()
    found, _ = reader.lookup(open_view(svc, reader), LookupSpec(text="North"))
    north = found["items"][0]["id"]  # type: ignore[index]
    svc.apply({"op": "update", "id": north, "set": {"name": "Northern region"}})
    data, _ = reader.lookup(
        open_view(svc, reader, as_of=AsOf(tx_seq=seq)), LookupSpec(text="Field support")
    )
    labels = {f["label"] for i in data["items"] for f in i["facts"]}  # type: ignore[union-attr, index]
    assert "North" in labels and "Northern region" not in labels
    data, _ = reader.lookup(open_view(svc, reader), LookupSpec(text="Field support"))
    labels = {f["label"] for i in data["items"] for f in i["facts"]}  # type: ignore[union-attr, index]
    assert "Northern region" in labels
