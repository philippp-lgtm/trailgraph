"""``graph run`` on both backends over the inventory toy ontology (functions plan K3, K5, K8,
K10, K11): a run equals the ad-hoc query of the normalized text with the bound parameters,
references are resolved and class-checked, a catalog that no longer resolves is refused, and
nothing of a parameter value leaves the answer."""

import json
from pathlib import Path
from typing import Any

import pytest
from support.functions import FUNCTIONS
from support.services import RecordingActivity, ServiceEnv

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import FunctionsSlot, functions_hash
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import load_entries
from graph_cli.services.describe import SKILL_FUNCTION_KEYS
from graph_cli.services.read import GraphRead, QuerySpec, ReadService

REQUEST = RequestMeta("run", "sha256:" + "0" * 64)
SOURCES = FUNCTIONS.parent / "inventory" / "valid"


@pytest.fixture(params=("inventory",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class CatalogWithFunctions:
    """The service env's catalog plus a published function catalog (the file backend
    publishes through graph-admin elsewhere; the reader only needs ``read_functions``)."""

    def __init__(self, inner: Any, slot: FunctionsSlot) -> None:
        self._inner = inner
        self._slot = slot

    def read_functions(self, graph: Any, *, sources: bool = False) -> FunctionsSlot:
        return self._slot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def publish(svc: ServiceEnv, compiled: JsonObject | None = None) -> None:
    sources = {p.name: p.read_text() for p in sorted(SOURCES.glob("*.yaml"))}
    if compiled is None:
        compiled, problems = compile_catalog(sources, svc.model)
        assert problems == []
    files = {name: text.encode() for name, text in sources.items()}
    slot = FunctionsSlot(functions_hash(files), hash_value(compiled), svc.entry.pin, compiled, {})
    svc.catalog = CatalogWithFunctions(svc.catalog, slot)  # type: ignore[assignment]


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    assert env.apply_seed().error is None
    publish(env)
    return env


def open_view(svc: ServiceEnv, reader: ReadService) -> GraphRead:
    return reader.open(svc.actor, svc.catalog.resolve(svc.name), "read.function", REQUEST)


def ids_of(svc: ServiceEnv, cls: str) -> list[Any]:
    return [r for r in svc.all_revisions() if r.type == cls and r.record_status == "active"]


def busiest_location(svc: ServiceEnv, reader: ReadService) -> Any:
    """The location with most items, found with an ad-hoc query."""
    view = open_view(svc, reader)
    text = (
        "MATCH (i:Item)-[:located_in]->(l:Location) "
        "RETURN l.id AS id, count(i) AS n ORDER BY n DESC"
    )
    data, *_ = reader.query(view, reader.parse_query(text), QuerySpec({}, False, False, 10, 0))
    first = data["items"][0]  # type: ignore[index]
    assert first["n"] > 1  # type: ignore[index]
    return next(r for r in ids_of(svc, "inv:Location") if str(r.id) == first["id"])  # type: ignore[index]


def test_a_run_equals_the_query_of_its_normalized_text(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    location = busiest_location(svc, reader)
    function = load_entries(svc.catalog.read_functions(svc.entry.id).compiled)[1]
    assert function.name == "items_in_location"
    view = open_view(svc, reader)
    ran, page, warnings = reader.run(
        view, "items_in_location", [("location", str(location.id))], limit=50, offset=0
    )
    expected, expected_page, _ = reader.query(
        view,
        reader.parse_query(function.query),
        QuerySpec({"location": str(location.id)}, False, False, 50, 0),
    )
    assert expected_page is not None and warnings == ()
    assert ran["function"] == {"name": "items_in_location", "hash": function.hash}
    assert {k: v for k, v in ran.items() if k != "function"} == expected
    assert page == expected_page and page.total > 1
    # paging after the function's own ordering
    second, second_page, _ = reader.run(
        view, "items_in_location", [("location", str(location.id))], limit=1, offset=1
    )
    assert second["items"] == ran["items"][1:2]  # type: ignore[index]
    assert second_page.more is None and second_page.total == page.total


def test_references_resolve_by_id_alias_and_trailing_hex(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    location = busiest_location(svc, reader)
    view = open_view(svc, reader)
    answers = [
        reader.run(view, "items_in_location", [("location", ref)], limit=50, offset=0)[0]
        for ref in (str(location.id), location.alias, location.id.hex[-12:])
    ]
    assert answers[0]["items"] and answers[0] == answers[1] == answers[2]


def test_a_reference_of_another_class_is_refused(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    item = ids_of(svc, "inv:Item")[0]
    with pytest.raises(GraphCliError) as info:
        reader.run(view, "items_in_location", [("location", item.alias)], limit=50, offset=0)
    assert info.value.code == "function.parameter_type"
    assert info.value.details["expected"] == "inv:Location"
    assert info.value.details["found"] == "inv:Item"
    for text, code in (
        ("0" * 12, "entity.not_found"),
        ("Main building", "function.parameter_invalid"),
    ):
        with pytest.raises(GraphCliError) as info:
            reader.run(view, "items_in_location", [("location", text)], limit=50, offset=0)
        assert info.value.code == code


def test_parameters_are_read_by_their_declared_type(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    data, _, _ = reader.run(view, "items_by_condition", [("condition", "worn")], limit=50, offset=0)
    assert data["items"] and {row["condition"] for row in data["items"]} == {"worn"}  # type: ignore[attr-defined,union-attr]
    for pairs, code in (
        ([("condition", "mint")], "function.parameter_invalid"),
        ([], "function.parameter_missing"),
        ([("condition", "worn"), ("size", "1")], "function.parameter_unknown"),
        ([("condition", "worn"), ("condition", "new")], "function.parameter_invalid"),
    ):
        with pytest.raises(GraphCliError) as info:
            reader.run(view, "items_by_condition", pairs, limit=50, offset=0)
        assert info.value.code == code
    with pytest.raises(GraphCliError) as info:
        reader.run(view, "items_by_conditions", [], limit=50, offset=0)
    assert info.value.code == "function.not_found"
    assert info.value.details["did_you_mean"][0] == "items_by_condition"


def test_the_activity_signal_names_the_function_and_no_value(svc: ServiceEnv) -> None:
    activity = RecordingActivity()
    reader = svc.make_reader(activity=activity)
    location = busiest_location(svc, reader)
    activity.received.clear()
    view = open_view(svc, reader)
    reader.run(view, "items_in_location", [("location", str(location.id))], limit=50, offset=0)
    (signal,) = activity.received
    assert (signal.command, signal.function) == ("run", "items_in_location")
    assert str(location.id) in {str(i) for i in signal.entities}  # touched, like a query
    assert "items_in_location" not in {str(i) for i in signal.entities}


def test_a_catalog_that_no_longer_resolves_is_stale(svc: ServiceEnv) -> None:
    compiled = svc.catalog.read_functions(svc.entry.id).compiled
    broken: list[JsonValue] = []
    for entry in compiled["functions"]:  # type: ignore[union-attr]
        assert isinstance(entry, dict)
        if entry["name"] == "items_in_location":
            entry = {**entry, "query": entry["query"].replace("inv:located_in", "inv:gone")}
        broken.append(entry)
    location = busiest_location(svc, svc.make_reader())
    publish(svc, {**compiled, "functions": broken})
    reader = svc.make_reader()
    view = open_view(svc, reader)
    with pytest.raises(GraphCliError) as info:
        reader.run(view, "items_in_location", [("location", location.alias)], limit=5, offset=0)
    assert info.value.code == "function.stale"
    assert info.value.details["function"] == "items_in_location"
    assert info.value.details["hash"] and info.value.details["cause"] == "term.unknown"
    assert "tell the maintainer" in info.value.message
    ok, _, _ = reader.run(view, "items_by_condition", [("condition", "new")], limit=5, offset=0)
    assert ok["function"]["name"] == "items_by_condition"  # type: ignore[index]


def test_a_graph_without_a_catalog_has_no_function(svc: ServiceEnv) -> None:
    svc.catalog = svc.catalog._inner  # type: ignore[assignment,union-attr]
    reader = svc.make_reader()
    view = open_view(svc, reader)
    with pytest.raises(GraphCliError) as info:
        reader.run(view, "items_in_location", [], limit=5, offset=0)
    assert (info.value.code, info.value.details["did_you_mean"]) == ("function.not_found", [])


def test_the_skill_model_carries_no_query_text_for_a_reader_without_read_function(
    svc: ServiceEnv,
) -> None:
    """K10: ``read.describe`` alone gets names, summaries and signatures, never the queries."""
    rules: list[JsonValue] = [
        {"id": "no_function", "effect": "deny", "action": ["read.function"], "resource": "graph:*"},
        {"id": "describe", "effect": "allow", "action": ["read.describe"], "resource": "graph:*"},
    ]
    document: JsonValue = {
        "schema": "graph_cli.policy/1",
        "id": "test",
        "rules": rules,
        "obligations": [],
    }
    policy = PolicySet("sha256:" + "1" * 64, ({**document},))  # type: ignore[arg-type]

    class Source:
        def current(self, graph: object) -> PolicySet:
            return policy

    entry = svc.catalog.resolve(svc.name)
    narrow = svc.make_reader(Source())
    with pytest.raises(GraphCliError):
        open_view(svc, narrow)  # running a function is refused
    model, _ = narrow.skill_model(svc.actor, entry, REQUEST)
    full, _ = svc.make_reader().skill_model(svc.actor, entry, REQUEST)
    assert model == full  # the same reduced document for every reader of the description
    functions = model["functions"]
    assert isinstance(functions, dict) and functions["functions"]
    for function in functions["functions"]:  # type: ignore[union-attr]
        assert set(function) <= set(SKILL_FUNCTION_KEYS)  # type: ignore[arg-type]
    text = json.dumps(model)
    assert "located_in" in text  # the model itself stays
    assert "WHERE l.id" not in text and "ORDER BY" not in text and "source_query" not in text
