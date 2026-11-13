"""``WorkingState``: the would-be state of one change set as a ``Snapshot`` (contracts B8.14).

It overlays the new revisions of the touched entities on the base snapshot.
Reads answer exactly as a store would after the commit, including queries
with the reference semantics (``kernel.query_eval``), so the validator can
judge the post-state with the same rules the read path uses.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import OpEffect
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.records import (
    RECORD_STATUSES,
    EdgeRecord,
    Revision,
    RevisionMeta,
    TxRecord,
    revision_meta,
)

_ALL: Query = Query(record_status=frozenset(RECORD_STATUSES), limit=sys.maxsize)


@dataclass(frozen=True, slots=True)
class _ReverseEdges:
    built_from: Mapping[EntityId, Revision]
    index: dict[EntityId, list[tuple[EntityId, EdgeRecord]]]


@dataclass(frozen=True, slots=True)
class WorkingState:
    """Base snapshot plus the new revision of every changed entity."""

    base: Snapshot
    revisions: Mapping[EntityId, Revision]  # first-touch order, changed entities only
    unchanged: Mapping[EntityId, Revision]  # touched, same content: the current revision
    created: frozenset[EntityId]
    locals: Mapping[str, EntityId]  # every local name bound in this request
    alias_allocations: tuple[tuple[EntityId, str], ...]
    resolution: Mapping[str, JsonValue]  # B4.4: {"locals": {...}, "refs": {...}}
    findings: tuple[Finding, ...]  # data.temporal.*
    effects: tuple[OpEffect, ...]
    _cache: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    @property
    def touched(self) -> frozenset[EntityId]:
        return frozenset(self.revisions)

    def local_of(self, entity: EntityId) -> str | None:
        for name, bound in self.locals.items():
            if bound == entity:
                return name
        return None

    # ---- Snapshot ------------------------------------------------------

    @property
    def seq(self) -> int:
        return self.base.seq

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return self.base.warnings

    def get(self, entity: EntityId) -> Revision | None:
        found = self.revisions.get(entity)
        return found if found is not None else self.base.get(entity)

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        found = {}
        for entity in entities:
            revision = self.get(entity)
            if revision is not None:
                found[entity] = revision
        return found

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        found = self.revisions.get(entity)
        if found is not None and found.rev == rev:
            return found
        return self.base.get_revision(entity, rev)

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]:
        metas = list(self.base.history(entity))
        found = self.revisions.get(entity)
        return [*metas, revision_meta(found)] if found is not None else metas

    def tx(self, seq: int) -> TxRecord | None:
        return self.base.tx(seq)

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        found = [
            (source, edge)
            for source, edge in self.base.incoming(entity, rel)
            if source not in self.revisions
        ]
        found += [
            (source, edge)
            for source, edge in self._reverse_edges().get(entity, ())
            if rel in (None, edge.rel)
        ]
        return sorted(found, key=lambda pair: (pair[0], pair[1].edge_id))

    def _reverse_edges(self) -> Mapping[EntityId, list[tuple[EntityId, EdgeRecord]]]:
        """The overlay's edges by target, built on first use (a large overlay, such as a
        migration's, would make a scan per call quadratic). The cache entry remembers the
        ``revisions`` it was built from: ``replace`` copies ``_cache`` along with them."""
        cached = self._cache.get("incoming")
        if isinstance(cached, _ReverseEdges) and cached.built_from is self.revisions:
            return cached.index
        index: dict[EntityId, list[tuple[EntityId, EdgeRecord]]] = {}
        for source, revision in self.revisions.items():
            for edge in revision.edges:
                index.setdefault(edge.target, []).append((source, edge))
        self._cache["incoming"] = _ReverseEdges(self.revisions, index)
        return index

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        for entity, allocated in self.alias_allocations:
            if allocated == alias:
                return entity, self.revisions[entity].alias == alias
        bound = self.base.resolve_alias(alias)
        if bound is None or bound[0] not in self.revisions:
            return bound
        return bound[0], self.revisions[bound[0]].alias == alias

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        suffix = hex_suffix.lower()
        found = set(self.base.match_suffix(suffix, limit=limit))
        found |= {e for e in self.created if e.hex.endswith(suffix)}
        return sorted(found)[:limit]

    def query(self, q: Query) -> QueryPage:
        return run_query(self, q)

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        return run_neighbors(self, q)

    # ---- Universe of the reference evaluator ---------------------------

    def _base_revisions(self, types: frozenset[TermRef]) -> dict[EntityId, Revision]:
        """Current base revisions of ``types`` (every type when empty), read once each."""
        found = self._cache.get("base:")  # every type, when read already
        if found is None:
            key = "base:" + ",".join(sorted(types))
            found = self._cache.get(key)
            if found is None:
                query = replace(_ALL, types=tuple(sorted(types)))
                found = {r.id: r for r in self.base.query(query).items}
                self._cache[key] = found
        assert isinstance(found, dict)
        return found

    def current(self, entity: EntityId) -> Revision | None:
        return self.get(entity)

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        """The base reads only ``types`` (the hint of B13.5); the changed revisions of
        every type are merged in, callers still filter."""
        merged: dict[EntityId, Revision] = {**self._base_revisions(types), **self.revisions}
        for entity in sorted(merged):
            yield merged[entity]
