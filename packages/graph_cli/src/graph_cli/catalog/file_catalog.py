"""The graph registry of a data root: ``graphs/*/manifest.toml`` (contracts B12.4, B12.8).

There is no shared registry file: the catalog scans the graph directories
on every call, so a graph published by graph-admin is visible to the next
read. Staging directories (``.tmp-*``) and anything that is not a canonical
graph id are ignored. Reads create nothing; a missing data root is
``backend.unavailable``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId, format_id, parse_id
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry, ManifestData, SkillSlot
from graph_cli.kernel.ports.storage import GraphStoreRead, GraphStoreWrite
from graph_cli.storage.common.faults import FaultHook
from graph_cli.storage.files.durable import Durability
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import read_manifest, read_release_file
from graph_cli.storage.files.segments import OpLog
from graph_cli.storage.files.skill_slot import read_skill
from graph_cli.storage.files.store import FileStore

FILES_BACKEND = "files"
FILES_FORMAT = 1  # [store] format_version of the FileStore layout (section 5)
UNREADABLE = frozenset({"config.invalid", "format.unsupported_version", "backend.unavailable"})


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """``config.toml [storage]`` (B12.7)."""

    lock_timeout_s: float = 10.0
    durability: Durability = "full"


class FileCatalog:
    def __init__(
        self,
        data_root: Path,
        *,
        settings: StorageSettings | None = None,
        oplog: OpLog | None = None,
        faults: FaultHook | None = None,
    ) -> None:
        self.data_root = data_root
        self.settings = settings or StorageSettings()
        self.oplog = oplog
        self.faults = faults  # crash tests only; production passes None (K.d)
        self._warnings: dict[tuple[str, str], WarningRecord] = {}

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        """Unknown manifest keys (once per file and key) and unreadable graphs seen so far."""
        return tuple(self._warnings.values())

    @property
    def graphs_dir(self) -> Path:
        return self.data_root / "graphs"

    def graph_ids(self) -> list[GraphId]:
        if not self.data_root.is_dir():
            raise GraphCliError(
                "backend.unavailable",
                "the data root does not exist",
                details={"data_root": str(self.data_root)},
            )
        try:
            names = os.listdir(self.graphs_dir)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise GraphCliError(
                "backend.unavailable", f"cannot list graphs: {exc.strerror or exc}"
            ) from exc
        return sorted(GraphId(parse_id(name)) for name in names if _is_graph_id(name))

    def manifest(self, graph: GraphId) -> ManifestData:
        data, warnings = read_manifest(self._layout(graph))
        for warning in warnings:
            self._warnings[(str(warning.details["file"]), str(warning.details["key"]))] = warning
        return data

    def manifests(
        self, *, include_archived: bool = True, skip_unreadable: bool = False
    ) -> list[ManifestData]:
        found = []
        for graph in self.graph_ids():
            try:
                found.append(self.manifest(graph))
            except GraphCliError as exc:
                if not skip_unreadable or exc.code not in UNREADABLE:
                    raise
                self._warnings[(str(graph), "")] = WarningRecord(
                    "graph.unreadable",
                    f"graph {graph} has an unreadable manifest and is not listed",
                    {"graph_id": str(graph), "cause": exc.code},
                )
        if not include_archived:
            found = [m for m in found if m.entry.state != "archived"]
        return sorted(found, key=lambda m: (m.entry.name, m.entry.id))

    # ---- Catalog port ----------------------------------------------------

    def list_graphs(
        self, *, include_archived: bool = False, skip_unreadable: bool = False
    ) -> Sequence[GraphEntry]:
        manifests = self.manifests(
            include_archived=include_archived, skip_unreadable=skip_unreadable
        )
        return [m.entry for m in manifests]

    def resolve(self, ref: str) -> GraphEntry:
        """Name or id, archived graphs included (names are unique across all, B1.11)."""
        if _is_uuid(ref):
            graph = GraphId(parse_id(ref))
            if graph in self.graph_ids():
                return self.manifest(graph).entry
        else:
            for entry in self.list_graphs(include_archived=True):
                if entry.name == ref:
                    return entry
        raise GraphCliError("graph.not_found", f"no graph {ref!r}", details={"ref": ref})

    def read_release_file(self, graph: GraphId, version: str, name: str) -> bytes:
        return read_release_file(self._layout(graph), version, name)

    def read_skill(self, graph: GraphId) -> SkillSlot | None:
        return read_skill(self._layout(graph))

    def store_for(self, graph: GraphId) -> tuple[GraphStoreRead, GraphStoreWrite]:
        store = self.file_store(graph)
        return store, store

    def file_store(self, graph: GraphId) -> FileStore:
        info = self.manifest(graph).store
        if (info.backend, info.format_version) != (FILES_BACKEND, FILES_FORMAT):
            raise GraphCliError(
                "format.unsupported_version",
                f"graph {graph} uses store {info.backend}/{info.format_version}",
                details={
                    "expected": f"{FILES_BACKEND}/{FILES_FORMAT}",
                    "actual": f"{info.backend}/{info.format_version}",
                },
            )
        return FileStore(
            self._layout(graph).root,
            graph,
            durability=self.settings.durability,
            oplog=self.oplog,
            faults=self.faults,
        )

    def _layout(self, graph: GraphId) -> GraphLayout:
        return GraphLayout(self.graphs_dir / str(graph))


def _is_uuid(text: str) -> bool:
    try:
        parse_id(text)
    except GraphCliError:
        return False
    return True


def _is_graph_id(name: str) -> bool:
    return _is_uuid(name) and format_id(parse_id(name)) == name


if TYPE_CHECKING:

    def _conforms(catalog: FileCatalog) -> Catalog:
        return catalog
