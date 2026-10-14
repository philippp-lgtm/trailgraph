"""Hand-built revisions and a dict-backed ``Snapshot`` for pure unit tests.

Validator and evaluator tests describe a small graph directly instead of
going through a store. Edge and entity ids are derived from short names so
expectations stay readable.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid5

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import WarningRecord
from graph_cli.kernel.ids import EdgeId, EntityId, TermRef, TxId
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.records import (
    EdgeRecord,
    RecordStatus,
    Revision,
    RevisionMeta,
    content_hash,
    content_projection,
    revision_meta,
)

_NS = UUID("0192f000-0000-7000-8000-000000000000")
AT = datetime(2026, 1, 1, tzinfo=UTC)
TX = TxId(UUID("0192f000-0000-7000-8000-0000000000aa"))


def eid(name: str) -> EntityId:
    return EntityId(uuid5(_NS, f"entity:{name}"))


def edge(rel: str, target: str, *, name: str | None = None, **qualifiers: JsonValue) -> EdgeRecord:
    return EdgeRecord(
        edge_id=EdgeId(uuid5(_NS, f"edge:{name or rel + '>' + target}")),
        rel=TermRef(rel),
        target=eid(target),
        qualifiers=dict(qualifiers),
    )


def revision(
    name: str,
    type: str,
    attrs: Mapping[str, JsonValue] | None = None,
    edges: Sequence[EdgeRecord] = (),
    *,
    status: RecordStatus = "active",
    rev: int = 1,
    alias: str | None = None,
) -> Revision:
    attrs = dict(attrs or {})
    projection = content_projection(
        type=type,
        record_status=status,
        alias=alias,
        attrs=attrs,
        edges=tuple(edges),
        valid_from=None,
        valid_to=None,
    )
    return Revision(
        id=eid(name),
        rev=rev,
        prev_rev=rev - 1 if rev > 1 else None,
        type=TermRef(type),
        record_status=status,
        attrs=attrs,
        edges=tuple(edges),
        alias=alias,
        valid_from=None,
        valid_to=None,
        created_at=AT,
        recorded_at=AT,
        tx_id=TX,
        tx_seq=rev,
        ontology_version="1.0.0",
        content_hash=content_hash(projection),
    )


class DictSnapshot:
    """Current revisions only; enough for evaluators and validators."""

    def __init__(
        self,
        *revisions: Revision,
        seq: int = 1,
        former_aliases: Mapping[str, EntityId] | None = None,
    ) -> None:
        self._current = {r.id: r for r in revisions}
        self._seq = seq
        self._former = dict(former_aliases or {})

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return ()

    def get(self, entity: EntityId) -> Revision | None:
        return self._current.get(entity)

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        return {e: self._current[e] for e in entities if e in self._current}

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        current = self._current.get(entity)
        return current if current is not None and current.rev == rev else None

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]:
        current = self._current.get(entity)
        return [] if current is None else [revision_meta(current)]

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        found = [
            (source.id, e)
            for source in self._current.values()
            for e in source.edges
            if e.target == entity and rel in (None, e.rel)
        ]
        return sorted(found, key=lambda pair: (pair[0], pair[1].edge_id))

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        for r in self._current.values():
            if r.alias == alias:
                return r.id, True
        return (self._former[alias], False) if alias in self._former else None

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        return sorted(e for e in self._current if e.hex.endswith(hex_suffix))[:limit]

    # Universe protocol of the reference evaluator
    def current(self, entity: EntityId) -> Revision | None:
        return self._current.get(entity)

    def current_revisions(self) -> list[Revision]:
        return [self._current[e] for e in sorted(self._current)]

    def query(self, q: Query) -> QueryPage:
        return run_query(self, q)

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        return run_neighbors(self, q)
