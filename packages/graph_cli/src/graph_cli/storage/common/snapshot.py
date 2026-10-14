"""A ``Snapshot`` over a replay view (contracts B4.10, B13.5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.records import EdgeRecord, Revision, RevisionMeta
from graph_cli.storage.common.view import ViewAt


class ViewSnapshot:
    """Every read answered from one ``ViewAt``; isolated by construction."""

    def __init__(self, view: ViewAt, warnings: tuple[WarningRecord, ...] = ()) -> None:
        self._view = view
        self._warnings = warnings

    @property
    def seq(self) -> int:
        return self._view.seq

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return self._warnings

    def get(self, entity: EntityId) -> Revision | None:
        return self._view.current(entity)

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        return self._view.get_many(entities)

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        return self._view.revision(entity, rev)

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]:
        return self._view.history(entity)

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self._view.incoming(entity, rel)

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        return self._view.resolve_alias(alias)

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        return self._view.match_suffix(hex_suffix, limit=limit)

    def query(self, q: Query) -> QueryPage:
        return run_query(self._view, q)

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        return run_neighbors(self._view, q)


def resolve_at_seq(at_seq: int | None, head_seq: int) -> int:
    """The seq a snapshot is taken at; ``usage.invalid_argument`` outside ``0..head``."""
    if at_seq is None:
        return head_seq
    if not 0 <= at_seq <= head_seq:
        raise GraphCliError(
            "usage.invalid_argument",
            f"seq {at_seq} is outside 0..{head_seq}",
            details={"at_seq": at_seq, "head_seq": head_seq},
        )
    return at_seq
