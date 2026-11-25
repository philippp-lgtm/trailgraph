"""The result types of a read: the opened graph, a page of rows, the graph block, a catalog."""

from __future__ import annotations

from dataclasses import dataclass

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.read_view import UNLIMITED, ReadView
from graph_cli.kernel.records import OntologyPin, Revision
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.names import MappedSnapshot
from graph_cli.services.visibility import VisibleSnapshot


@dataclass(frozen=True, slots=True)
class GraphRead:
    """One graph opened for reading by one actor and command: model and one snapshot."""

    entry: GraphEntry
    model: CompiledModel
    snapshot: Snapshot
    warnings: tuple[WarningRecord, ...]
    actor: ActorContext
    command: str
    # What the actor sees (``kernel.read_view``); when it is ``limited``, ``model`` and
    # ``snapshot`` are the filtered views, else the raw model and snapshot.
    read_view: ReadView = UNLIMITED
    # The mapped layer below the read limits (migration plan M7): old revisions under the
    # pin's names; ``None`` only for views built without ``ReadService.open``.
    mapping: MappedSnapshot | None = None
    # what the request says made it (``RequestMeta.via``), for the denial a later step records
    via: str | None = None

    def envelope_graph(self) -> dict[str, JsonValue]:
        return graph_json(self.entry, self.snapshot.seq)

    def names_mapped(self, revision: Revision) -> bool:
        """Whether ``revision`` was handed out under mapped names (``names_mapped``). For a
        limited reader only when mapping changed something it sees (R4): the stored and the
        mapped revision differ under the same view (its type, the attributes the mapped type
        keeps, the edges it sees, matched by id)."""
        mapping = self.mapping
        if mapping is None or not mapping.mapped(revision.id, revision.rev):
            return False
        snapshot = self.snapshot
        if not isinstance(snapshot, VisibleSnapshot):
            return True
        stored = mapping.raw.get_revision(revision.id, revision.rev)
        mapped = mapping.get_revision(revision.id, revision.rev)
        if stored is None or mapped is None:
            return True
        keep = snapshot.keeps(mapped)
        before, after = snapshot.masked(stored, keep), snapshot.masked(mapped, keep)
        return (
            before.type != after.type
            or before.attrs != after.attrs
            or {e.edge_id: e for e in before.edges} != {e.edge_id: e for e in after.edges}
        )


@dataclass(frozen=True, slots=True)
class Page:
    total: int
    shown: int
    limit: int
    offset: int
    more: bool | None = None  # set when limit and offset apply per group (lookup values)

    def to_json(self) -> dict[str, JsonValue]:
        truncated = self.offset + self.shown < self.total if self.more is None else self.more
        next_offset = self.offset + (self.shown if self.more is None else self.limit)
        return {
            "total": self.total,
            "shown": self.shown,
            "limit": self.limit,
            "offset": self.offset,
            "truncated": truncated,
            "next_offset": next_offset if truncated else None,
        }


def graph_json(entry: GraphEntry, seq: int | None) -> dict[str, JsonValue]:
    """The envelope ``graph`` block (B9.2)."""
    return {
        "id": str(entry.id),
        "name": entry.name,
        "state": entry.state,
        "ontology": {"bundle": entry.pin.bundle, "version": entry.pin.version},
        "seq": seq,
    }


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    """The parsed published action catalog of a graph and what it was published against."""

    entries: tuple[ActionEntry, ...]
    compiled_hash: str
    published_against: OntologyPin
    compiled: JsonObject
