"""Loading the pinned compiled model of a graph (contracts B3.18, B7.6, W.3).

The artifact is read and checked against the pin on every call, so a write
always sees the files as they are (``ontology.pin_mismatch``,
``ontology.artifact_missing``). Parsed models are cached by the raw bytes'
hash: identical bytes give the identical model without parsing again.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import CanonicalError, hash_bytes, parse_json
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.ontology_layer.model import CompiledModel, load_compiled, model_from_json

COMPILED_FILE: Final = "compiled.json"


class ModelCache:
    """``load`` for writes (a pin mismatch refuses); ``load_for_read`` continues with a warning."""

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

    def load_for_read(self, entry: GraphEntry) -> tuple[CompiledModel, tuple[WarningRecord, ...]]:
        """B7.6: reads continue on a mismatching artifact with ``ontology.pin_mismatch_read``."""
        try:
            return self.load(entry), ()
        except GraphCliError as exc:
            if exc.code != "ontology.pin_mismatch":
                raise
        data = self._catalog.read_release_file(entry.id, entry.pin.version, COMPILED_FILE)
        try:
            model = model_from_json(as_object(parse_json(data), ""))
        except (CanonicalError, JsonShapeError) as exc:
            raise GraphCliError(
                "ontology.pin_mismatch", f"compiled ontology is unreadable: {exc}"
            ) from exc
        warning = WarningRecord(
            "ontology.pin_mismatch_read",
            "the pinned ontology artifact does not match its hash; read anyway",
            {"version": entry.pin.version, "expected": entry.pin.artifact_hash},
        )
        return model, (warning,)
