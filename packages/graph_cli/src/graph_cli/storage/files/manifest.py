"""``manifest.toml``: the per-graph manifest (contracts B12, Decision 17, section 5).

Part of the file installation layout and written only by storage under a
lock (graph create, ``replace_manifest``); the catalog reads it through this
codec. Reading follows V.3/V.4: an unknown ``format_version`` is
``format.unsupported_version``, unknown keys are warnings
(``config.unknown_key``), any other defect is ``config.invalid`` naming the
file and key. Writing uses a fixed key order (C.8).
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
from graph_cli.kernel.ports.catalog import GraphEntry, GraphState, ManifestData, StoreInfo
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.keyrings import SEALED_FORMAT
from graph_cli.storage.files.tomlw import TomlTable, TomlValue, dumps

MANIFEST_FILE: Final = "manifest.toml"
MANIFEST_FORMAT: Final = 1
GRAPH_STATES: Final[tuple[GraphState, ...]] = get_args(GraphState)

# Every documented key per table; anything else is an unknown key (V.4).
_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "": frozenset({"format_version", "graph", "ontology", "store", "alias", "extensions"}),
    "graph": frozenset({"id", "name", "title", "language", "state", "created_at"}),
    "ontology": frozenset({"bundle", "version", "artifact_hash"}),
    "store": frozenset({"backend", "format_version"}),
    "alias": frozenset({"prefixes"}),
    "extensions": frozenset({"enabled"}),
}


def format_manifest(manifest: ManifestData) -> bytes:
    entry = manifest.entry
    graph: list[tuple[str, TomlValue]] = [("id", format_id(entry.id)), ("name", entry.name)]
    if entry.title is not None:
        graph.append(("title", entry.title))
    if entry.language is not None:
        graph.append(("language", entry.language))
    graph += [("state", entry.state), ("created_at", format_timestamp(manifest.created_at))]
    tables: list[TomlTable] = [
        (None, [("format_version", MANIFEST_FORMAT)]),
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
                ("backend", manifest.store.backend),
                ("format_version", manifest.store.format_version),
            ],
        ),
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
    if version != MANIFEST_FORMAT:
        raise GraphCliError(
            "format.unsupported_version",
            f"{source}: unsupported format_version {version!r}",
            details={"file": source, "expected": MANIFEST_FORMAT, "actual": str(version)},
        )
    reader.unknown_keys("", document)
    graph = reader.table(document, "graph")
    ontology = reader.table(document, "ontology")
    store = reader.table(document, "store")
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
    store_info = StoreInfo(
        reader.text(store, "store.backend"), reader.integer(store, "store.format_version")
    )
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
        sealed=store_info.format_version == SEALED_FORMAT,
    )
    manifest = ManifestData(
        entry=entry,
        created_at=reader.timestamp(graph, "graph.created_at"),
        store=store_info,
    )
    return manifest, tuple(reader.warnings)


class _Reader:
    def __init__(self, source: str) -> None:
        self.source = source
        self.warnings: list[WarningRecord] = []

    def invalid(self, key: str, message: str) -> GraphCliError:
        where = f"{self.source}: {key} " if key else f"{self.source}: "
        return GraphCliError(
            "config.invalid", where + message, details={"file": self.source, "key": key}
        )

    def unknown_keys(self, path: str, table: Mapping[str, Any]) -> None:
        known = _KEYS.get(path)
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
