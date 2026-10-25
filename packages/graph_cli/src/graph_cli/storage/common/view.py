"""Tx-log replay into a read view (contracts B1.8, B1.9, B3.12, B4.6, B4.10).

``GraphView`` is an append-only index over tx records. Nothing in it is ever
changed or removed, and every entry remembers the seq that added it, so one
view answers questions **as of any seq** up to its end: ``view.at(seq)``.
That gives snapshot isolation for free: a commit only appends entries with a
greater seq, which every older ``ViewAt`` filters out.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.records import EdgeRecord, Revision, RevisionMeta, TxRecord, revision_meta


def alias_parts(alias: str) -> tuple[str, int]:
    """``ITEM-0007`` -> ``("ITEM", 7)``; the grammar (``ALIAS_RE``) allows one hyphen."""
    prefix, _, number = alias.rpartition("-")
    return prefix, int(number)


class StateView(Protocol):
    """The graph at ``seq`` as snapshots and commit checks read it: a ``ViewAt`` or a
    writer's view over the index (``storage.files.store.WriterView``)."""

    @property
    def seq(self) -> int: ...

    def current(self, entity: EntityId) -> Revision | None: ...

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]: ...

    def revision(self, entity: EntityId, rev: int) -> Revision | None: ...

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]: ...

    def tx(self, seq: int) -> TxRecord: ...

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterable[Revision]: ...

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]: ...

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None: ...

    def alias_bound(self, alias: str) -> bool: ...

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]: ...


@dataclass(frozen=True, slots=True)
class _InEdge:
    source: EntityId
    source_rev: int
    edge: EdgeRecord


class GraphView:
    """Append-only replay of a tx log. Feed it txs in seq order with ``apply``."""

    def __init__(self) -> None:
        self._txs: list[TxRecord] = []
        self._history: dict[EntityId, list[Revision]] = {}
        self._edges_in: dict[EntityId, list[_InEdge]] = {}
        self._aliases: dict[str, tuple[EntityId, int]] = {}  # alias -> (entity, tx seq)
        self._idempotency: dict[tuple[str, str], TxRecord] = {}  # (principal, key)

    @property
    def seq(self) -> int:
        return len(self._txs)

    def apply(self, tx: TxRecord) -> None:
        """Add the next tx. Commit checks ran before; this only indexes."""
        if tx.seq != self.seq + 1:
            raise ValueError(f"tx seq {tx.seq} does not follow {self.seq}")
        self._txs.append(tx)
        for revision in tx.revisions:
            self._history.setdefault(revision.id, []).append(revision)
            for edge in revision.edges:
                self._edges_in.setdefault(edge.target, []).append(
                    _InEdge(revision.id, revision.rev, edge)
                )
        for entity, alias in tx.alias_allocations:
            self._aliases.setdefault(alias, (entity, tx.seq))
        if tx.idempotency_key is not None:
            key = (tx.actor.principal.id or "", tx.idempotency_key)
            self._idempotency.setdefault(key, tx)

    def at(self, seq: int | None = None) -> ViewAt:
        target = self.seq if seq is None else seq
        if not 0 <= target <= self.seq:
            raise ValueError(f"seq {target} is outside 0..{self.seq}")
        return ViewAt(self, target)


class ViewAt:
    """The graph exactly as of ``seq``: txs with a greater seq are invisible."""

    def __init__(self, view: GraphView, seq: int) -> None:
        self._view = view
        self.seq = seq

    def _visible(self, entity: EntityId) -> list[Revision]:
        history = self._view._history.get(entity, [])
        return history[: bisect_right(history, self.seq, key=lambda r: r.tx_seq)]

    def current(self, entity: EntityId) -> Revision | None:
        visible = self._visible(entity)
        return visible[-1] if visible else None

    def revision(self, entity: EntityId, rev: int) -> Revision | None:
        visible = self._visible(entity)
        return visible[rev - 1] if 1 <= rev <= len(visible) else None

    def history(self, entity: EntityId) -> list[RevisionMeta]:
        return [revision_meta(r) for r in self._visible(entity)]

    def entity_ids(self) -> list[EntityId]:
        """Every entity that exists at ``seq``, in id order."""
        return sorted(
            entity
            for entity, history in self._view._history.items()
            if history[0].tx_seq <= self.seq
        )

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        for entity in self.entity_ids():
            current = self.current(entity)
            assert current is not None
            if not types or current.type in types:
                yield current

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> list[tuple[EntityId, EdgeRecord]]:
        """Edges of current revisions that point at ``entity``, by source id then edge id."""
        found = []
        for item in self._view._edges_in.get(entity, []):
            if rel is not None and item.edge.rel != rel:
                continue
            current = self.current(item.source)
            if current is not None and current.rev == item.source_rev:
                found.append((item.source, item.edge))
        return sorted(found, key=lambda pair: (pair[0], pair[1].edge_id))

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        """``(id, is_current_alias)``; former aliases stay bound forever (B1.8)."""
        bound = self._view._aliases.get(alias)
        if bound is None or bound[1] > self.seq:
            return None
        entity = bound[0]
        current = self.current(entity)
        return entity, current is not None and current.alias == alias

    def alias_bindings(self) -> Iterator[tuple[str, EntityId, int]]:
        """Every alias bound up to ``seq``: ``(alias, entity, seq of the binding)``."""
        for alias, (entity, seq) in self._view._aliases.items():
            if seq <= self.seq:
                yield alias, entity, seq

    def alias_bound(self, alias: str) -> bool:
        return self.resolve_alias(alias) is not None

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> list[EntityId]:
        """Entities whose id ends with ``hex_suffix`` (the random part, B1.9), in id order."""
        suffix = hex_suffix.lower()
        matches = [e for e in self.entity_ids() if e.hex.endswith(suffix)]
        return matches[:limit]

    def alias_counters(self) -> dict[str, int]:
        """Per prefix, the highest number allocated up to ``seq`` (B1.8)."""
        counters: dict[str, int] = {}
        for tx in self._view._txs[: self.seq]:
            for _, alias in tx.alias_allocations:
                prefix, number = alias_parts(alias)
                counters[prefix] = max(counters.get(prefix, 0), number)
        return dict(sorted(counters.items()))

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None:
        """The tx that used ``key`` for this principal (scope: graph, principal; B3.12)."""
        tx = self._view._idempotency.get((principal_id, key))
        return tx if tx is not None and tx.seq <= self.seq else None

    def tx(self, seq: int) -> TxRecord:
        if not 1 <= seq <= self.seq:
            raise ValueError(f"seq {seq} is outside 1..{self.seq}")
        return self._view._txs[seq - 1]

    def txs_since(self, seq: int, *, limit: int) -> Sequence[TxRecord]:
        """Txs with a greater seq, in order, at most ``limit`` (B4.9)."""
        start = max(seq, 0)
        return tuple(self._view._txs[start : min(self.seq, start + max(limit, 0))])

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        found = {}
        for entity in entities:
            current = self.current(entity)
            if current is not None:
                found[entity] = current
        return found
