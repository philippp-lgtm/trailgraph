"""Catalog port: graph registry and release access (contracts section 2, B12)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

from graph_cli.kernel.canonical import JsonValue, hash_bytes, hash_value
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId, TermRef
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


@dataclass(frozen=True, slots=True)
class StoreInfo:
    backend: str
    format_version: int


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

    def store_for(self, graph: GraphId) -> tuple[GraphStoreRead, GraphStoreWrite]: ...


__all__ = [
    "SKILL_SOURCE_SCHEMA",
    "Catalog",
    "GraphEntry",
    "GraphState",
    "ManifestData",
    "OntologyPin",
    "SkillSlot",
    "StoreInfo",
    "skill_hash",
]
