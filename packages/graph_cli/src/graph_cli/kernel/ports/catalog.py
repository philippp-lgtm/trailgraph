"""Catalog port: graph registry and release access (contracts section 2, B12)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from graph_cli.kernel.errors import WarningRecord
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


@dataclass(frozen=True, slots=True)
class StoreInfo:
    backend: str
    format_version: int


@dataclass(frozen=True, slots=True)
class ManifestData:
    """Typed ``manifest.toml`` content (B12). Written only by graph-admin."""

    entry: GraphEntry
    title: str | None
    created_at: datetime
    store: StoreInfo


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

    def store_for(self, graph: GraphId) -> tuple[GraphStoreRead, GraphStoreWrite]: ...


__all__ = ["Catalog", "GraphEntry", "GraphState", "ManifestData", "OntologyPin", "StoreInfo"]
