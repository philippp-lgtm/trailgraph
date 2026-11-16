"""Loading the pinned compiled model of a graph (contracts B3.18, B7.6, W.3).

The artifact is read and checked against the pin on every call, so a write
always sees the files as they are (``ontology.pin_mismatch``,
``ontology.artifact_missing``). Parsed models are cached by the raw bytes'
hash: identical bytes give the identical model without parsing again. The
cache keeps the pin hash a model was verified against: the pin covers the
stored document, and ``CompiledModel.artifact_hash`` re-serializes with the
current writer, which differs for artifacts written before a key was added.
Reading and checking the artifact is the ``model`` phase of the command's
timings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.kernel.canonical import CanonicalError, hash_bytes, parse_json
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.timing import Timings
from graph_cli.ontology_layer.model import (
    CompiledModel,
    load_compiled,
    model_from_json,
    traces_markings,
)
from graph_cli.ontology_layer.vocabulary import MIGRATION_FILE

COMPILED_FILE: Final = "compiled.json"
RELEASE_FILE: Final = "release.json"


class ModelCache:
    """``load`` for writes (a pin mismatch refuses); ``load_for_read`` continues with a warning."""

    def __init__(self, catalog: Catalog, timings: Timings | None = None) -> None:
        self._catalog = catalog
        self._timings = timings if timings is not None else Timings()
        self._models: dict[str, tuple[str, CompiledModel]] = {}  # bytes hash -> (pin, model)
        self._releases: dict[str, CompiledModel] = {}  # bytes hash -> model, any version

    def load(self, entry: GraphEntry) -> CompiledModel:
        with self._timings.phase("model"):
            data = self._catalog.read_release_file(entry.id, entry.pin.version, COMPILED_FILE)
            return self._verified(entry, data)

    def used_markings(self, entry: GraphEntry) -> frozenset[str]:
        """The markings the pinned model uses (``CompiledModel.used_markings``), decided from
        the raw bytes first: bytes without a trace of markings (``traces_markings``) hold
        none, whether or not they match the pin, and are not parsed. Bytes with a trace are
        loaded as ``load`` does: a pin mismatch or unreadable bytes raise
        ``ontology.pin_mismatch`` (fail closed: only the pin vouches for which attributes an
        artifact marks). Nothing is remembered beyond the verified models, so the answer
        always follows the bytes as they are."""
        with self._timings.phase("model"):
            data = self._catalog.read_release_file(entry.id, entry.pin.version, COMPILED_FILE)
            if not traces_markings(data):
                return frozenset()
            return self._verified(entry, data).used_markings()

    def _verified(self, entry: GraphEntry, data: bytes) -> CompiledModel:
        pin = entry.pin
        key = hash_bytes(data)
        cached = self._models.get(key)
        if cached is not None and cached[0] == pin.artifact_hash:
            return cached[1]
        model = load_compiled(data, expected_hash=pin.artifact_hash)
        self._models[key] = (pin.artifact_hash, model)
        return model

    def load_release(self, graph: GraphId, version: str) -> CompiledModel:
        """The compiled model of an installed release by its version, for reading revisions
        written under it (migration plan M7). Not checked against a pin (an older release is
        no pin any more); parsed once per distinct bytes."""
        with self._timings.phase("model"):
            data = self._catalog.read_release_file(graph, version, COMPILED_FILE)
            key = hash_bytes(data)
            found = self._releases.get(key)
            if found is None:
                try:
                    found = model_from_json(as_object(parse_json(data), ""))
                except (CanonicalError, JsonShapeError) as exc:
                    raise GraphCliError(
                        "backend.corrupt",
                        f"compiled ontology {version} is unreadable: {exc}",
                        details={"version": version},
                    ) from exc
                self._releases[key] = found
            return found

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


def installed_migration(read_file: Callable[[str], bytes]) -> bytes | None:
    """The ``migration.yaml`` an installed release keeps (``read_file`` reads a file of its
    directory); ``None`` for a release without one. The file is checked against the hash
    ``release.json`` lists."""
    try:
        metadata = parse_json(read_file(RELEASE_FILE))
    except CanonicalError as exc:
        raise GraphCliError("backend.corrupt", f"{RELEASE_FILE} is unreadable: {exc}") from exc
    listed = metadata.get("files") if isinstance(metadata, Mapping) else None
    if not isinstance(listed, Mapping) or MIGRATION_FILE not in listed:
        return None
    data = read_file(MIGRATION_FILE)
    if hash_bytes(data) != listed[MIGRATION_FILE]:
        raise GraphCliError(
            "backend.corrupt", f"{MIGRATION_FILE} differs from its hash in {RELEASE_FILE}"
        )
    return data
