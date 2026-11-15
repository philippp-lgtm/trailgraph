"""Catalog port: graph registry and release access (contracts section 2, B12)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_bytes, hash_value
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId, TermRef
from graph_cli.kernel.migration_marker import MigrationMarker
from graph_cli.kernel.records import OntologyPin

if TYPE_CHECKING:
    from graph_cli.kernel.ports.storage import GraphStoreRead, GraphStoreWrite

GraphState = Literal["active", "readonly", "archived"]


@dataclass(frozen=True, slots=True)
class GraphEntry:
    id: GraphId
    name: str
    state: GraphState
    pin: OntologyPin
    alias_prefixes: Mapping[TermRef, str]
    extensions: tuple[str, ...]
    title: str | None = None  # human-readable purpose, shown by `graph graphs` (P1.5)
    language: str | None = None  # BCP 47 default language of the values (Q18 stage 2)
    # A data migration whose tx is committed while the manifest still names the old version
    # (a release running, or one that crashed before its pin switch). Set only by the app's
    # catalog, which then already pins the entry to the target version (``pin``); ``None``
    # otherwise, also when only the marker file is left after the switch.
    migration: MigrationMarker | None = None


BlobLocation = Literal["local", "s3"]


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """The manifest's ``[store]``: the backend, its layout version and where the graph keeps
    its stored files. ``blob_location`` ``"s3"`` names the object store by ``store_id``;
    a local graph has no ``store_id``."""

    backend: str
    format_version: int
    blob_location: BlobLocation = "local"
    store_id: str | None = None


@dataclass(frozen=True, slots=True)
class ManifestData:
    """Typed ``manifest.toml`` content (B12). Written only by graph-admin."""

    entry: GraphEntry
    created_at: datetime
    store: StoreInfo


SKILL_SOURCE_SCHEMA: Final = "graph_cli.skill_source/1"


def skill_hash(files: Mapping[str, bytes]) -> str:
    """C.2 hash of a skill tree: the hash of ``{path: sha256 of the bytes}``."""
    return hash_value({name: hash_bytes(files[name]) for name in sorted(files)})


@dataclass(frozen=True, slots=True)
class SkillSlot:
    """The published skill of a graph (D15 Q2), written only by graph-admin.

    ``kind`` says how the files become a skill: ``static`` files are the
    skill as is. ``published_against`` is the graph's ontology pin at publish
    time.
    """

    kind: str
    hash: str
    published_against: OntologyPin
    files: Mapping[str, bytes]

    def summary(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "hash": self.hash,
            "published_against": self.published_against.to_json(),
        }

    def source_json(self) -> dict[str, JsonValue]:
        """The slot's ``source.json`` document."""
        return {"schema": SKILL_SOURCE_SCHEMA, **self.summary()}


FUNCTION_SLOT_SCHEMA: Final = "graph_cli.function_slot/1"
ACTION_SLOT_SCHEMA: Final = "graph_cli.action_slot/1"

SlotKind = Literal["functions", "actions"]


@dataclass(frozen=True, slots=True)
class KindSpec:
    """What differs between the kinds of kinetic slot (``functions/``, ``actions/``): the
    schema names, the list key of the compiled document and the damaged-slot error code.
    Everything else about a slot is one implementation."""

    kind: SlotKind
    slot_schema: str
    catalog_schema: str  # kinetic.format / kinetic.action_format CATALOG_SCHEMA; pinned by tests
    list_key: str
    invalid_code: str


KINDS: Final[Mapping[str, KindSpec]] = {
    "functions": KindSpec(
        "functions",
        FUNCTION_SLOT_SCHEMA,
        "graph_cli.functions/1",
        "functions",
        "functions.slot_invalid",
    ),
    "actions": KindSpec(
        "actions",
        ACTION_SLOT_SCHEMA,
        "graph_cli.action_catalog/1",
        "actions",
        "actions.slot_invalid",
    ),
}


def sources_hash(files: Mapping[str, bytes]) -> str:
    """C.2 hash of kinetic sources: the hash of ``{file name: sha256 of the bytes}``."""
    return hash_value({name: hash_bytes(files[name]) for name in sorted(files)})


functions_hash = sources_hash


@dataclass(frozen=True, slots=True)
class KineticSlot:
    """The published function or action catalog of a graph (K6, actions plan A8), written
    only by graph-admin.

    ``hash`` is the tree hash of the authored sources (``<name>.yaml``), the one
    the audit chain records; ``compiled`` is the catalog document
    (``graph_cli.functions/1`` or ``graph_cli.action_catalog/1``) checked against
    ``published_against``, the graph's ontology pin at publish time, and
    ``compiled_hash`` its C.2 hash. ``files`` holds the sources when the maintenance
    port read them; the catalog port, which the app uses, leaves it empty because the
    app reads only the compiled document. An empty catalog is a published slot without
    entries. ``kind`` is ``functions`` unless stated.
    """

    hash: str
    compiled_hash: str
    published_against: OntologyPin
    compiled: JsonObject
    files: Mapping[str, bytes] = field(default_factory=dict)
    kind: SlotKind = "functions"

    @property
    def spec(self) -> KindSpec:
        return KINDS[self.kind]

    @property
    def count(self) -> int:
        entries = self.compiled.get(self.spec.list_key)
        return len(entries) if isinstance(entries, list) else 0

    def summary(self) -> dict[str, JsonValue]:
        return {
            "count": self.count,
            "hash": self.hash,
            "compiled_hash": self.compiled_hash,
            "published_against": self.published_against.to_json(),
        }

    def slot_json(self) -> dict[str, JsonValue]:
        """The slot's ``slot.json`` document."""
        return {
            "schema": self.spec.slot_schema,
            "hash": self.hash,
            "compiled_hash": self.compiled_hash,
            "published_against": self.published_against.to_json(),
        }


FunctionsSlot = KineticSlot  # the name existing code uses for ``kind="functions"``


def graph_not_found(ref: str) -> GraphCliError:
    """The one ``graph.not_found``: a graph the caller may not see reads exactly like one
    that does not exist (B6.15)."""
    return GraphCliError("graph.not_found", f"no graph {ref!r}", details={"ref": ref})


class Catalog(Protocol):
    def list_graphs(
        self, *, include_archived: bool = False, skip_unreadable: bool = False
    ) -> Sequence[GraphEntry]:
        """``skip_unreadable`` leaves out graphs whose manifest cannot be read and
        records warning ``graph.unreadable`` in ``warnings`` (P1 item 6b)."""
        ...

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        """Notices collected while reading manifests (unknown keys, unreadable graphs)."""
        ...

    def resolve(self, ref: str) -> GraphEntry:
        """Name or id; raises ``graph.not_found``."""
        ...

    def read_release_file(self, graph: GraphId, version: str, name: str) -> bytes: ...

    def read_skill(self, graph: GraphId) -> SkillSlot | None:
        """The graph's published skill, or ``None`` when none was published (D15 Q2)."""
        ...

    def read_functions(self, graph: GraphId, *, sources: bool = False) -> FunctionsSlot | None:
        """The graph's published function catalog, or ``None`` when none was published (K6).
        The sources (``files``) are read only on request: the app does not need them,
        ``graph pull`` does."""
        ...

    def read_actions(self, graph: GraphId, *, sources: bool = False) -> KineticSlot | None:
        """The graph's published action catalog, or ``None`` when none was published
        (actions plan A8); ``sources`` as for ``read_functions``."""
        ...

    def store_for(self, graph: GraphId) -> tuple[GraphStoreRead, GraphStoreWrite]: ...


__all__ = [
    "ACTION_SLOT_SCHEMA",
    "FUNCTION_SLOT_SCHEMA",
    "KINDS",
    "SKILL_SOURCE_SCHEMA",
    "BlobLocation",
    "Catalog",
    "FunctionsSlot",
    "GraphEntry",
    "GraphState",
    "KindSpec",
    "KineticSlot",
    "ManifestData",
    "OntologyPin",
    "SkillSlot",
    "SlotKind",
    "StoreInfo",
    "functions_hash",
    "skill_hash",
    "sources_hash",
]
