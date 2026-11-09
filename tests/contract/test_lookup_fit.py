"""``lookup --for`` / ``--for-source`` on both backends over the notes-and-sources toy
ontology (context-aware lookup, step 1): matches of a class that cannot fill the slot are
not candidates but are counted per text, a function ``ref`` parameter is a use too, and the
function form needs ``read.function``."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.services import ServiceEnv

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import FunctionsSlot, functions_hash
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.query_ast import AsOf
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.query.lookup import LookupSpec
from graph_cli.services.read import GraphRead, ReadService

REQUEST = RequestMeta.of("lookup", {})

FUNCTION = """\
schema: graph_cli.function/1
name: notes_citing
summary: Notes that cite a source.
use_when: >-
  Which notes cite a given source.
params:
  source:
    type: ref
    class: nas:Source
    summary: The source that is cited.
  kind:
    type: enum
    enum: nas:NoteKind
    summary: The kind of note.
query: |
  MATCH (n:Note)-[:cites]->(s:Source)
  WHERE s.id = $source AND n.kind = $kind
  RETURN n.title AS note
  ORDER BY note
returns:
  - column: note
    summary: Title of the note.
examples:
  - question: Which observations cite the atoms page?
"""


@pytest.fixture(params=("notes_and_sources",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class CatalogWithFunctions:
    def __init__(self, inner: Any, slot: FunctionsSlot) -> None:
        self._inner = inner
        self._slot = slot

    def read_functions(self, graph: Any, *, sources: bool = False) -> FunctionsSlot:
        return self._slot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    assert env.apply_seed().error is None
    sources = {"notes_citing.yaml": FUNCTION}
    compiled: JsonObject
    compiled, problems = compile_catalog(sources, env.model)
    assert problems == []
    files = {name: text.encode() for name, text in sources.items()}
    slot = FunctionsSlot(functions_hash(files), hash_value(compiled), env.entry.pin, compiled, {})
    env.catalog = CatalogWithFunctions(env.catalog, slot)  # type: ignore[assignment]
    return env


def open_view(svc: ServiceEnv, reader: ReadService, **changes: Any) -> GraphRead:
    return reader.open(svc.actor, svc.catalog.resolve(svc.name), "read.find", REQUEST, **changes)


def counts(data: dict[str, JsonValue]) -> dict[str, tuple[int, int, int]]:
    return {
        t["text"]: (t["total"], t["exact"], t["excluded"])  # type: ignore[index, misc]
        for t in data["texts"]  # type: ignore[union-attr]
    }


def types_of(data: dict[str, JsonValue]) -> list[str]:
    return [i["type"] for i in data["items"]]  # type: ignore[index, union-attr]


def test_without_a_use_nothing_is_excluded(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    data, page = reader.lookup(
        open_view(svc, reader), LookupSpec(text="orbital", match="prefix"), ["orbital"]
    )
    assert data["fit"] is None
    assert counts(data) == {"orbital": (3, 0, 0)}
    assert page.total == 3


def test_for_a_relation_keeps_the_candidates_of_its_range(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    data, page = reader.lookup(
        view, LookupSpec(text="orbital", match="prefix"), ["orbital"], for_use="nas:cites"
    )
    assert types_of(data) == ["nas:WebPage"]
    assert counts(data) == {"orbital": (1, 0, 2)}  # two notes do not fit
    assert page.total == 1
    assert data["fit"] == {
        "for": "nas:cites",
        "for_source": None,
        "types": ["nas:Book", "nas:Source", "nas:WebPage"],
    }


def test_exact_counts_candidates_that_fit_only(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    # "Orbitals" is a web page; "Orbitals, restated" and "Orbital shapes" are notes.
    spec = LookupSpec(text="Orbitals", match="prefix")
    data, _ = reader.lookup(view, spec, ["Orbitals"], for_use="about")
    assert types_of(data) == []
    assert counts(data) == {"Orbitals": (0, 0, 2)}  # the page (exact) and a note (prefix)
    data, _ = reader.lookup(view, spec, ["Orbitals"], for_use="cites")
    assert counts(data) == {"Orbitals": (1, 1, 1)}


def test_excluded_is_counted_per_text_of_a_batch(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    texts = ["Physics", "Lectures on physics", "chemistry"]
    data, page = reader.lookup(view, LookupSpec(text="x"), texts, for_use="about")
    assert counts(data) == {
        "Physics": (1, 1, 0),
        "Lectures on physics": (0, 0, 1),  # a book is no topic
        "chemistry": (1, 1, 0),
    }
    assert page.total == 2 and page.shown == 2


def test_for_source_filters_by_domain_and_intersects_with_for(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    spec = LookupSpec(text="orbital", match="prefix")
    data, _ = reader.lookup(view, spec, for_source="cites")
    assert set(types_of(data)) == {"nas:Note"}
    assert counts(data) == {"orbital": (2, 0, 1)}
    assert data["fit"] == {"for": None, "for_source": "cites", "types": ["nas:Note"]}
    # target of "about" and source of "broader" are both Topic
    data, _ = reader.lookup(
        view, LookupSpec(text="phys", match="prefix"), for_use="about", for_source="broader"
    )
    assert data["fit"]["types"] == ["nas:Topic"]  # type: ignore[index]
    assert types_of(data) == ["nas:Topic"]
    # no class is both a target of "about" and a source of "cites"
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, LookupSpec(text="x"), for_use="about", for_source="cites")
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details["argument"] == "--for"


def test_an_any_class_use_filters_nothing_but_is_reported(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    spec = LookupSpec(text="orbital", match="prefix")
    data, _ = reader.lookup(view, spec, for_use="core:derived_from")
    assert counts(data) == {"orbital": (3, 0, 0)}
    assert data["fit"] == {"for": "core:derived_from", "for_source": None, "types": None}


def test_paging_applies_after_the_filter(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    spec = LookupSpec(text="orbital", match="prefix", limit=1)
    first, page = reader.lookup(view, spec, for_source="cites")
    second, last = reader.lookup(view, replace(spec, offset=1), for_source="cites")
    assert page.total == last.total == 2 and page.more is True and last.more is False
    assert counts(first) == counts(second) == {"orbital": (2, 0, 1)}
    ids = [i["id"] for d in (first, second) for i in d["items"]]  # type: ignore[index, union-attr]
    assert len(set(ids)) == 2


def test_an_unknown_relation_is_refused(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, LookupSpec(text="x"), for_use="cited")
    assert info.value.code == "term.unknown" and info.value.details["kind"] == "relation"
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, LookupSpec(text="x"), for_source="nope")
    assert info.value.code == "term.unknown"


def test_for_a_function_ref_parameter_uses_its_class_with_subclasses(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    data, _ = reader.lookup(
        view,
        LookupSpec(text="orbital", match="prefix"),
        for_use="notes_citing.source",
    )
    assert types_of(data) == ["nas:WebPage"]
    assert counts(data) == {"orbital": (1, 0, 2)}
    assert data["fit"] == {
        "for": "notes_citing.source",
        "for_source": None,
        "types": ["nas:Book", "nas:Source", "nas:WebPage"],
    }


def test_function_form_errors(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    spec = LookupSpec(text="x")
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, spec, for_use="notes_citin.source")
    assert info.value.code == "function.not_found"
    assert info.value.details["did_you_mean"] == ["notes_citing"]
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, spec, for_use="notes_citing.sourc")
    assert info.value.code == "function.parameter_unknown"
    assert info.value.details["did_you_mean"] == ["source"]
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, spec, for_use="notes_citing.kind")
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details["argument"] == "--for"
    with pytest.raises(GraphCliError) as info:
        reader.lookup(
            view, LookupSpec(text="x", types=("nas:Topic",)), for_use="notes_citing.source"
        )
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details["types"] == ["nas:Book", "nas:Source", "nas:WebPage"]


def test_a_function_the_actor_may_not_read_is_not_found(svc: ServiceEnv) -> None:
    rules: list[JsonValue] = [
        {"id": "no_function", "effect": "deny", "action": ["read.function"], "resource": "graph:*"},
        {"id": "find", "effect": "allow", "action": ["read.find"], "resource": "graph:*"},
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

    reader = svc.make_reader(Source())
    view = open_view(svc, reader)
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, LookupSpec(text="x"), for_use="notes_citing.source")
    assert info.value.code == "function.not_found"
    # relations need no function right
    data, _ = reader.lookup(view, LookupSpec(text="Physics"), for_use="about")
    assert counts(data) == {"Physics": (1, 1, 0)}


def test_as_of_still_applies(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    spec = LookupSpec(text="Orbitals", match="prefix")
    now, _ = reader.lookup(open_view(svc, reader), spec, for_use="cites")
    assert counts(now) == {"Orbitals": (1, 1, 1)}
    early = AsOf(tx_seq=1)  # before the sources and notes exist
    then, _ = reader.lookup(open_view(svc, reader, as_of=early), spec, for_use="cites")
    assert counts(then) == {"Orbitals": (0, 0, 0)}
    assert then["fit"] == now["fit"]
