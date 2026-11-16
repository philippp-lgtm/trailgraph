"""``ModelCache.used_markings``: the markings a pin uses, decided from the raw bytes first
(field visibility plan F4; review fixes of stage 4).

Bytes without a trace of markings are answered without parsing a model, whatever they are;
bytes with a trace must be the pinned artifact, or the answer fails closed. Nothing outlives
the bytes it came from.
"""

import json

import pytest
from support.fixtures import compiled_model

from graph_cli.kernel.canonical import canonical_bytes, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import OntologyPin
from graph_cli.ontology_layer.model import traces_markings
from graph_cli.services import models as models_module
from graph_cli.services.models import ModelCache

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
MARKED = compiled_model("staff").to_json()
PLAIN = compiled_model("notes_and_sources", "1.1.0").to_json()


class Files:
    """The one catalog method ``ModelCache`` reads: the bytes of ``compiled.json``."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.reads = 0

    def read_release_file(self, graph: GraphId, version: str, name: str) -> bytes:
        self.reads += 1
        return self.data


def entry(document: object) -> GraphEntry:
    pin = OntologyPin("staff", "1.0.0", hash_value(document))  # type: ignore[arg-type]
    return GraphEntry(EXAMPLE, "example_graph", "active", pin, {}, ())


def cache(data: bytes) -> tuple[ModelCache, Files]:
    files = Files(data)
    return ModelCache(files), files  # type: ignore[arg-type]


def test_the_markings_of_a_pinned_marked_model() -> None:
    models, _ = cache(canonical_bytes(MARKED))
    assert models.used_markings(entry(MARKED)) == {"stf:personal", "stf:pay"}


def test_bytes_without_a_trace_parse_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("bytes without a trace of markings are not parsed")

    monkeypatch.setattr(models_module, "load_compiled", refuse)
    monkeypatch.setattr("graph_cli.ontology_layer.model.parse_json", refuse)
    models, _ = cache(canonical_bytes(PLAIN))
    assert models.used_markings(entry(PLAIN)) == frozenset()
    # also when they do not match the pin, and when they are no JSON at all
    assert models.used_markings(entry(MARKED)) == frozenset()
    models, _ = cache(b"\x00 not json")
    assert models.used_markings(entry(PLAIN)) == frozenset()


def test_bytes_with_a_trace_that_do_not_match_the_pin_fail_closed() -> None:
    models, _ = cache(canonical_bytes(MARKED).replace(b'"summary":"', b'"summary":"X', 1))
    with pytest.raises(GraphCliError) as info:
        models.used_markings(entry(MARKED))
    assert info.value.code == "ontology.pin_mismatch"
    models, _ = cache(b'{"schema": "graph_cli.compiled_ontology/2", "trunc')  # unreadable
    with pytest.raises(GraphCliError) as info:
        models.used_markings(entry(MARKED))
    assert info.value.code == "ontology.pin_mismatch"


def test_the_answer_follows_the_bytes_as_they_are() -> None:
    """A mismatching artifact without markings, then the pinned one: the second answer is not
    the remembered first one (the fail-open case of an answer cached per pin)."""
    models, files = cache(canonical_bytes(PLAIN))
    pinned = entry(MARKED)
    assert models.used_markings(pinned) == frozenset()
    files.data = canonical_bytes(MARKED)
    assert models.used_markings(pinned) == {"stf:personal", "stf:pay"}
    assert models.used_markings(pinned) == {"stf:personal", "stf:pay"}  # parsed once, verified
    assert files.reads == 3


def test_traces_of_markings() -> None:
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology/2"}')
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology/1", "markings": {}}')
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology/2", "trunc')
    assert not traces_markings(b'{"schema": "graph_cli.compiled_ontology/1"}')
    assert not traces_markings(b"\x00 not json")
    assert not traces_markings(canonical_bytes(PLAIN))
    assert traces_markings(canonical_bytes(MARKED))
    # the words spelled through JSON escapes are parsed and found all the same
    assert traces_markings(b'{"mark\\u0069ngs": {}}')
    assert traces_markings(b'{"schema": "graph_cli.compiled_ontology\\/2"}')
    assert not traces_markings(b'{"schema": "graph_cli.compiled_ontology/1", "x": "\\u0041"}')
    # a word in a text is no key
    assert not traces_markings(json.dumps({"summary": "requires markings"}).encode())
