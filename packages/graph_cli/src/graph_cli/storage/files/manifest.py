"""``manifest.toml``: the per-graph manifest (contracts B12, Decision 17, section 5).

Part of the file installation layout and written only by storage under a
lock (graph create, ``replace_manifest``); the catalog reads it through this
codec. Reading follows V.3/V.4: an unknown ``format_version`` is
``format.unsupported_version``, unknown keys are warnings
(``config.unknown_key``), any other defect is ``config.invalid`` naming the
file and key. Writing uses a fixed key order (C.8).

Format 1 is the manifest of a graph whose stored files are local. Format 2
adds a top-level ``requires``, the storage features a reader must know to
open the graph; a reader that does not know one refuses the graph with
``format.unsupported_version`` naming it. Programs that know only format 1
refuse every format-2 manifest by its top-level ``format_version``. The one
feature so far is ``blobs.s3``: the stored files are in an object store,
named by ``[store.blobs] location = "s3"`` and ``store_id``. A graph whose
files are local is always written as format 1, byte for byte as before.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, get_args

from graph_cli.kernel.canonical import HASH_RE
from graph_cli.kernel.clock import format_timestamp, parse_timestamp
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import (
    ALIAS_PREFIX_RE,
    GRAPH_NAME_RE,
    TERM_REF_RE,
    GraphId,
    TermRef,
    format_id,
    parse_id,
)
from graph_cli.kernel.langtag import normalize_language_tag
from graph_cli.kernel.ports.catalog import (
    BlobLocation,
    GraphEntry,
    GraphState,
    ManifestData,
    StoreInfo,
)
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.tomlw import TomlTable, TomlValue, dumps

MANIFEST_FILE: Final = "manifest.toml"
MANIFEST_FORMAT: Final = 1  # a graph with local stored files
MANIFEST_FORMAT_REQUIRES: Final = 2  # format 1 plus the top-level ``requires``
MANIFEST_FORMATS: Final = (MANIFEST_FORMAT, MANIFEST_FORMAT_REQUIRES)
GRAPH_STATES: Final[tuple[GraphState, ...]] = get_args(GraphState)
FEATURE_BLOBS_S3: Final = "blobs.s3"
KNOWN_FEATURES: Final = frozenset({FEATURE_BLOBS_S3})

# Every documented key per table; anything else is an unknown key (V.4). Format 2 adds
# ``requires`` and ``[store.blobs]`` (``_KEYS_REQUIRES``).
_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "": frozenset({"format_version", "graph", "ontology", "store", "alias", "extensions"}),
    "graph": frozenset({"id", "name", "title", "language", "state", "created_at"}),
    "ontology": frozenset({"bundle", "version", "artifact_hash"}),
    "store": frozenset({"backend", "format_version"}),
    "alias": frozenset({"prefixes"}),
    "extensions": frozenset({"enabled"}),
}
_KEYS_REQUIRES: Final[Mapping[str, frozenset[str]]] = {
    **_KEYS,
    "": _KEYS[""] | {"requires"},
    "store": _KEYS["store"] | {"blobs"},
    "store.blobs": frozenset({"location", "store_id"}),
}


def manifest_features(store: StoreInfo) -> tuple[str, ...]:
    """The features a manifest with ``store`` requires; none means format 1."""
    return (FEATURE_BLOBS_S3,) if store.blob_location == "s3" else ()


def format_manifest(manifest: ManifestData) -> bytes:
    entry = manifest.entry
    graph: list[tuple[str, TomlValue]] = [("id", format_id(entry.id)), ("name", entry.name)]
    if entry.title is not None:
        graph.append(("title", entry.title))
    if entry.language is not None:
        graph.append(("language", entry.language))
    graph += [("state", entry.state), ("created_at", format_timestamp(manifest.created_at))]
    store = manifest.store
    features = manifest_features(store)
    root: list[tuple[str, TomlValue]] = [("format_version", MANIFEST_FORMAT)]
    blobs: list[TomlTable] = []
    if (store.store_id is None) == bool(features):
        raise GraphCliError(
            "internal.error", "a store id belongs to a graph in an object store, and only there"
        )
    if features:
        assert store.store_id is not None
        root = [("format_version", MANIFEST_FORMAT_REQUIRES), ("requires", features)]
        blobs = [("store.blobs", [("location", store.blob_location), ("store_id", store.store_id)])]
    tables: list[TomlTable] = [
        (None, root),
        ("graph", graph),
        (
            "ontology",
            [
                ("bundle", entry.pin.bundle),
                ("version", entry.pin.version),
                ("artifact_hash", entry.pin.artifact_hash),
            ],
        ),
        (
            "store",
            [
                ("backend", store.backend),
                ("format_version", store.format_version),
            ],
        ),
        *blobs,
        ("alias.prefixes", sorted(entry.alias_prefixes.items())),
        ("extensions", [("enabled", tuple(entry.extensions))]),
    ]
    return dumps(tables).encode()


def parse_manifest(data: bytes, *, source: str) -> tuple[ManifestData, tuple[WarningRecord, ...]]:
    """Parse ``data``; ``source`` names the file in errors and warnings."""
    reader = _Reader(source)
    try:
        document = tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise reader.invalid("", f"not valid TOML: {exc}") from None
    version = document.get("format_version")
    if version is None:
        raise reader.invalid("format_version", "is missing")
    if type(version) is not int or version not in MANIFEST_FORMATS:
        raise GraphCliError(
            "format.unsupported_version",
            f"{source}: unsupported format_version {version!r}",
            details={"file": source, "expected": list(MANIFEST_FORMATS), "actual": str(version)},
        )
    if version == MANIFEST_FORMAT_REQUIRES:
        features = reader.features(document)  # before anything else: unknown ones refuse
        reader.keys = _KEYS_REQUIRES
    else:
        features = ()
        for key in ("requires", "store.blobs"):
            if _lookup(document, key) is not None:
                raise reader.invalid(key, f"needs format_version = {MANIFEST_FORMAT_REQUIRES}")
    reader.unknown_keys("", document)
    graph = reader.table(document, "graph")
    ontology = reader.table(document, "ontology")
    store = reader.table(document, "store")
    blobs = reader.table(store, "blobs", required=False, path="store.")
    alias = reader.table(document, "alias", required=False)
    prefixes = reader.table(alias, "prefixes", required=False, path="alias.")
    extensions = reader.table(document, "extensions", required=False)

    name = reader.text(graph, "graph.name")
    if not GRAPH_NAME_RE.match(name):
        raise reader.invalid("graph.name", "is not a valid graph name")
    state = reader.text(graph, "graph.state")
    if state not in GRAPH_STATES:
        raise reader.invalid("graph.state", f"must be one of {', '.join(GRAPH_STATES)}")
    artifact_hash = reader.text(ontology, "ontology.artifact_hash")
    if not HASH_RE.match(artifact_hash):
        raise reader.invalid("ontology.artifact_hash", "is not a sha256 hash")
    alias_prefixes: dict[TermRef, str] = {}
    for curie, prefix in prefixes.items():
        key = f"alias.prefixes.{curie}"
        if not TERM_REF_RE.match(curie) or not isinstance(prefix, str):
            raise reader.invalid(key, "must map a term to a prefix")
        if not ALIAS_PREFIX_RE.match(prefix):
            raise reader.invalid(key, "is not a valid alias prefix")
        alias_prefixes[TermRef(curie)] = prefix
    enabled = extensions.get("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(e, str) for e in enabled):
        raise reader.invalid("extensions.enabled", "must be an array of strings")
    title = graph.get("title")
    if title is not None and not isinstance(title, str):
        raise reader.invalid("graph.title", "must be a string")
    language = graph.get("language")
    if language is not None:
        if not isinstance(language, str):
            raise reader.invalid("graph.language", "must be a string")
        canonical = normalize_language_tag(language)
        if canonical is None:
            raise reader.invalid("graph.language", "is not a well-formed BCP 47 language tag")
        if canonical != language:
            raise reader.invalid("graph.language", f"is not in canonical case ({canonical!r})")
    entry = GraphEntry(
        id=reader.graph_id(graph),
        name=name,
        state=state,
        pin=OntologyPin(
            reader.text(ontology, "ontology.bundle"),
            reader.text(ontology, "ontology.version"),
            artifact_hash,
        ),
        alias_prefixes=alias_prefixes,
        extensions=tuple(enabled),
        title=title,
        language=language,
    )
    location, store_id = reader.blob_location(blobs, features)
    manifest = ManifestData(
        entry=entry,
        created_at=reader.timestamp(graph, "graph.created_at"),
        store=StoreInfo(
            reader.text(store, "store.backend"),
            reader.integer(store, "store.format_version"),
            location,
            store_id,
        ),
    )
    return manifest, tuple(reader.warnings)


def _lookup(document: Mapping[str, Any], dotted: str) -> object:
    value: object = document
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class _Reader:
    def __init__(self, source: str) -> None:
        self.source = source
        self.warnings: list[WarningRecord] = []
        self.keys = _KEYS

    def invalid(self, key: str, message: str) -> GraphCliError:
        where = f"{self.source}: {key} " if key else f"{self.source}: "
        return GraphCliError(
            "config.invalid", where + message, details={"file": self.source, "key": key}
        )

    def features(self, document: Mapping[str, Any]) -> tuple[str, ...]:
        """Format 2's ``requires``: a non-empty array of distinct feature names, each known
        to this reader (else ``format.unsupported_version`` naming the first unknown one)."""
        value = document.get("requires")
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(f, str) and f for f in value)
            or len(set(value)) != len(value)
        ):
            raise self.invalid("requires", "must be a non-empty array of distinct feature names")
        for feature in value:
            if feature not in KNOWN_FEATURES:
                raise GraphCliError(
                    "format.unsupported_version",
                    f"{self.source}: requires the storage feature {feature!r}, which this"
                    " program does not know; use a newer version",
                    details={
                        "file": self.source,
                        "feature": feature,
                        "known": sorted(KNOWN_FEATURES),
                    },
                )
        return tuple(value)

    def blob_location(
        self, blobs: Mapping[str, Any], features: tuple[str, ...]
    ) -> tuple[BlobLocation, str | None]:
        """``StoreInfo``'s ``blob_location`` and ``store_id``; ``requires`` and
        ``[store.blobs]`` must agree."""
        if FEATURE_BLOBS_S3 not in features:
            if blobs:
                raise self.invalid("requires", f"must name {FEATURE_BLOBS_S3!r} with [store.blobs]")
            return "local", None
        if not blobs:
            raise self.invalid("store.blobs", f"is required with requires {FEATURE_BLOBS_S3!r}")
        if blobs.get("location") != "s3":
            raise self.invalid(
                "store.blobs.location", f"must be 's3' with requires {FEATURE_BLOBS_S3!r}"
            )
        text = self.text(blobs, "store.blobs.store_id")
        try:
            store_id = parse_id(text)
        except GraphCliError:
            raise self.invalid("store.blobs.store_id", "is not a UUID") from None
        if format_id(store_id) != text:
            raise self.invalid("store.blobs.store_id", "is not in canonical form")
        return "s3", text

    def unknown_keys(self, path: str, table: Mapping[str, Any]) -> None:
        known = self.keys.get(path)
        if known is None:
            return
        for key in sorted(set(table) - known):
            full = f"{path}.{key}" if path else key
            self.warnings.append(
                WarningRecord(
                    "config.unknown_key",
                    f"{self.source}: unknown key {full!r} is ignored",
                    {"file": self.source, "key": full},
                )
            )

    def table(
        self, parent: Mapping[str, Any], key: str, *, required: bool = True, path: str = ""
    ) -> Mapping[str, Any]:
        value = parent.get(key)
        if value is None and not required:
            return {}
        if not isinstance(value, dict):
            raise self.invalid(path + key, "must be a table")
        self.unknown_keys(path + key, value)
        return value

    def text(self, table: Mapping[str, Any], dotted: str) -> str:
        value = table.get(dotted.rsplit(".", 1)[-1])
        if not isinstance(value, str) or not value:
            raise self.invalid(dotted, "must be a non-empty string")
        return value

    def integer(self, table: Mapping[str, Any], dotted: str) -> int:
        value = table.get(dotted.rsplit(".", 1)[-1])
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.invalid(dotted, "must be an integer")
        return value

    def timestamp(self, table: Mapping[str, Any], dotted: str) -> datetime:
        text = self.text(table, dotted)
        try:
            value = parse_timestamp(text)
        except GraphCliError:
            raise self.invalid(dotted, "is not a timestamp") from None
        if format_timestamp(value) != text:
            raise self.invalid(dotted, "is not in YYYY-MM-DDTHH:MM:SS.sssZ form")
        return value

    def graph_id(self, table: Mapping[str, Any]) -> GraphId:
        text = self.text(table, "graph.id")
        try:
            value = parse_id(text)
        except GraphCliError:
            raise self.invalid("graph.id", "is not a UUID") from None
        if format_id(value) != text:
            raise self.invalid("graph.id", "is not in canonical form")
        return GraphId(value)
