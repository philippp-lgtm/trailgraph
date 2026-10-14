"""Loading the pinned compiled model of a graph (contracts B3.18, B7.6, W.3).

The artifact is read and checked against the pin on every call, so a write
always sees the files as they are (``ontology.pin_mismatch``,
``ontology.artifact_missing``). Parsed models are cached by the raw bytes'
hash: identical bytes give the identical model without parsing again.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import hash_bytes
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.ontology_layer.model import CompiledModel, load_compiled

COMPILED_FILE: Final = "compiled.json"


class ModelCache:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._models: dict[str, CompiledModel] = {}

    def load(self, entry: GraphEntry) -> CompiledModel:
        pin = entry.pin
        data = self._catalog.read_release_file(entry.id, pin.version, COMPILED_FILE)
        key = hash_bytes(data)
        model = self._models.get(key)
        if model is None or model.artifact_hash != pin.artifact_hash:
            model = load_compiled(data, expected_hash=pin.artifact_hash)
            self._models[key] = model
        return model
