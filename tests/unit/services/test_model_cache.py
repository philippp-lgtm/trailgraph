"""The pinned model cache (contracts B7.6; Q18 stage 1 step 1)."""

from collections.abc import Sequence

import pytest
from support.fixtures import compiled_model

from graph_cli.kernel.canonical import hash_value, pretty_bytes
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead, GraphStoreWrite
from graph_cli.kernel.records import OntologyPin
from graph_cli.services import models
from graph_cli.services.models import ModelCache

GRAPH = GraphId(parse_id("0192f700-0000-7000-8000-000000000001"))


class OneFileCatalog:
    """Serves one ``compiled.json``."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return ()

    def list_graphs(
        self, *, include_archived: bool = False, skip_unreadable: bool = False
    ) -> Sequence[GraphEntry]:
        return ()

    def resolve(self, ref: str) -> GraphEntry:
        raise AssertionError("not used")

    def read_release_file(self, graph: GraphId, version: str, name: str) -> bytes:
        return self.data

    def store_for(self, graph: GraphId) -> tuple[GraphStoreRead, GraphStoreWrite]:
        raise AssertionError("not used")


def older_artifact() -> tuple[bytes, str]:
    """A stored document whose recompile differs: it has a key the writer drops."""
    document = compiled_model("inventory", "1.0.0").to_json()
    document["retired_key"] = None
    return pretty_bytes(document), hash_value(document)


def entry(artifact_hash: str) -> GraphEntry:
    return GraphEntry(
        GRAPH, "g", "active", OntologyPin("inventory", "1.0.0", artifact_hash), {}, ()
    )


def test_cache_hits_an_artifact_whose_recompile_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    data, pinned = older_artifact()
    loads: list[str] = []

    def counting_load(raw: bytes, *, expected_hash: str) -> object:
        loads.append(expected_hash)
        return original(raw, expected_hash=expected_hash)

    original = models.load_compiled
    monkeypatch.setattr(models, "load_compiled", counting_load)
    cache = ModelCache(OneFileCatalog(data))
    first = cache.load(entry(pinned))
    assert first.artifact_hash != pinned
    assert cache.load(entry(pinned)) is first
    assert loads == [pinned]


def test_cache_still_checks_the_pin() -> None:
    data, pinned = older_artifact()
    cache = ModelCache(OneFileCatalog(data))
    cache.load(entry(pinned))
    with pytest.raises(GraphCliError) as caught:
        cache.load(entry("sha256:" + "0" * 64))
    assert caught.value.code == "ontology.pin_mismatch"
