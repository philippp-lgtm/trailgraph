"""The disposable SQLite index of one graph (contracts B4.1, B13.7, W.15).

``data/index/index.sqlite`` holds the state at one seq: the current revision
of every entity, every alias binding, the incoming edges of current
revisions, and where each tx line lies in the log. It is derived from the tx
log and never changed in place: a writer copies it (or builds a new one) into
``index.sqlite.tmp``, applies its txs, syncs it and renames it over the old
file. So readers open it with ``immutable=1``: no locks, no journal, no side
files (reads write nothing), and an open file never changes under a reader.

A reader binds the index to the log as it binds ``head.json``: the tx at the
index seq must carry the recorded hash and end at the recorded position, and
the txs after it are overlaid from the log (``IndexedView``). Anything
unexpected in the file raises ``IndexUnusable``; callers fall back to a
replay, never to a user error.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, GraphId, TermRef
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.records import (
    EdgeRecord,
    Revision,
    TxRecord,
    revision_from_json,
    revision_to_json,
    tx_from_json,
)
from graph_cli.storage.common.chain import GENESIS, ChainError, ChainLink, verify_line
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.view import GraphView, ViewAt
from graph_cli.storage.files.durable import TEMP_SUFFIX, Syncer, ensure_dir
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.segments import LogPosition, SegmentedLog

INDEX_FORMAT: Final = "graph_cli.index/1"
INDEX_ERRORS: Final = (OSError, sqlite3.Error)
"""What a failed index update raises; the commit stands, the index stays behind."""

_SCHEMA: Final = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE entity (id BLOB PRIMARY KEY, type TEXT NOT NULL, body TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX entity_type ON entity (type, id);
CREATE TABLE alias (alias TEXT PRIMARY KEY, entity BLOB NOT NULL, seq INTEGER NOT NULL)
    WITHOUT ROWID;
CREATE TABLE edge_in (
    target BLOB NOT NULL, source BLOB NOT NULL, edge_id BLOB NOT NULL, rel TEXT NOT NULL,
    body TEXT NOT NULL, PRIMARY KEY (target, source, edge_id)
) WITHOUT ROWID;
CREATE INDEX edge_in_source ON edge_in (source);
CREATE TABLE tx (
    seq INTEGER PRIMARY KEY, segment TEXT NOT NULL, start INTEGER NOT NULL,
    end INTEGER NOT NULL, hash TEXT NOT NULL
);
"""
_TABLES: Final = ("entity", "alias", "edge_in", "tx")


class IndexUnusable(Exception):
    """The index file cannot be used; the message says why."""


@dataclass(frozen=True, slots=True)
class TxSpan:
    """Where one tx line lies in the log."""

    start: LogPosition
    end: LogPosition


@dataclass(frozen=True, slots=True)
class IndexMeta:
    graph_id: str
    link: ChainLink  # the last tx the index covers
    position: LogPosition  # the log position after that tx

    @property
    def seq(self) -> int:
        return self.link.seq


# ---- rows: one encoding for building and verifying ---------------------


def _body(value: JsonValue) -> str:
    """Compact JSON with sorted keys, so the same state always gives the same rows."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _entity_row(revision: Revision) -> tuple[bytes, str, str]:
    return revision.id.bytes, revision.type, _body(revision_to_json(revision))


def _edge_rows(revision: Revision) -> Iterator[tuple[bytes, bytes, bytes, str, str]]:
    for edge in revision.edges:
        body = _body(edge.to_json())
        yield edge.target.bytes, revision.id.bytes, edge.edge_id.bytes, edge.rel, body


def _tx_row(tx: TxRecord, span: TxSpan) -> tuple[int, str, int, int, str]:
    return tx.seq, span.start.segment, span.start.offset, span.end.offset, tx.hash


def expected_rows(view: ViewAt, spans: Sequence[TxSpan]) -> dict[str, set[tuple[object, ...]]]:
    """The rows of every table for the state ``view`` (``spans`` cover its txs)."""
    rows: dict[str, set[tuple[object, ...]]] = {name: set() for name in _TABLES}
    for revision in view.current_revisions():
        rows["entity"].add(_entity_row(revision))
        rows["edge_in"].update(_edge_rows(revision))
    rows["alias"] = {(a, e.bytes, s) for a, e, s in view.alias_bindings()}
    rows["tx"] = {_tx_row(view.tx(seq), spans[seq - 1]) for seq in range(1, view.seq + 1)}
    return rows


def _meta_rows(graph_id: GraphId, link: ChainLink, span: TxSpan) -> list[tuple[str, str]]:
    return [
        ("format", INDEX_FORMAT),
        ("graph_id", str(graph_id)),
        ("seq", str(link.seq)),
        ("hash", link.hash),
        ("segment", span.end.segment),
        ("offset", str(span.end.offset)),
    ]


# ---- reading -----------------------------------------------------------


@contextmanager
def _guard() -> Iterator[None]:
    """Turn anything a damaged or foreign file can raise into ``IndexUnusable``."""
    try:
        yield
    except IndexUnusable:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyError, JsonShapeError, GraphCliError) as exc:
        raise IndexUnusable(f"the index is unreadable: {exc}") from None


def _uuid(raw: object) -> EntityId:
    if not isinstance(raw, bytes) or len(raw) != 16:
        raise ValueError("an id column is not 16 bytes")
    return EntityId(UUID(bytes=raw))


class IndexReader:
    """One open index file, read-only and immutable."""

    def __init__(self, connection: sqlite3.Connection, meta: IndexMeta) -> None:
        self._db = connection
        self.meta = meta

    @classmethod
    def open(cls, path: Path) -> IndexReader | None:
        """The index at ``path``; ``None`` when there is none."""
        if not path.exists():
            return None
        with _guard():
            uri = f"{path.absolute().as_uri()}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
            try:
                values = dict(connection.execute("SELECT key, value FROM meta").fetchall())
                if values.get("format") != INDEX_FORMAT:
                    raise IndexUnusable(f"the index format is not {INDEX_FORMAT}")
                meta = IndexMeta(
                    graph_id=values["graph_id"],
                    link=ChainLink(int(values["seq"]), values["hash"]),
                    position=LogPosition(values["segment"], int(values["offset"])),
                )
            except BaseException:
                connection.close()
                raise
        return cls(connection, meta)

    def close(self) -> None:
        self._db.close()

    def _rows(self, sql: str, *args: object) -> list[tuple[object, ...]]:
        with _guard():
            return self._db.execute(sql, args).fetchall()

    def revision(self, entity: EntityId) -> Revision | None:
        rows = self._rows("SELECT body FROM entity WHERE id = ?", entity.bytes)
        return _decode(rows[0][0], entity) if rows else None

    def revisions(self, types: frozenset[TermRef]) -> Iterator[Revision]:
        """Current revisions in id order, only of ``types`` unless it is empty."""
        if types:
            marks = ",".join("?" * len(types))
            sql = f"SELECT id, body FROM entity WHERE type IN ({marks}) ORDER BY id"
            rows = self._rows(sql, *sorted(types))
        else:
            rows = self._rows("SELECT id, body FROM entity ORDER BY id")
        with _guard():
            for raw, body in rows:
                yield _decode(body, _uuid(raw))

    def entity_ids(self) -> list[EntityId]:
        with _guard():
            return [_uuid(raw) for (raw,) in self._rows("SELECT id FROM entity ORDER BY id")]

    def incoming(self, target: EntityId, rel: TermRef | None) -> list[tuple[EntityId, EdgeRecord]]:
        if rel is None:
            rows = self._rows("SELECT source, body FROM edge_in WHERE target = ?", target.bytes)
        else:
            sql = "SELECT source, body FROM edge_in WHERE target = ? AND rel = ?"
            rows = self._rows(sql, target.bytes, rel)
        with _guard():
            return [
                (_uuid(raw), EdgeRecord.from_json(as_object(json.loads(str(body)), "edge")))
                for raw, body in rows
            ]

    def alias(self, alias: str) -> tuple[EntityId, int] | None:
        rows = self._rows("SELECT entity, seq FROM alias WHERE alias = ?", alias)
        if not rows:
            return None
        with _guard():
            raw, seq = rows[0]
            if not isinstance(seq, int):
                raise ValueError("an alias seq is not an integer")
            return _uuid(raw), seq

    def tx(self, seq: int, txlog: SegmentedLog) -> TxRecord:
        """Tx ``seq`` read from its place in the log and checked against the chain."""
        rows = self._rows(
            "SELECT seq, segment, start, end, hash FROM tx WHERE seq IN (?, ?) ORDER BY seq",
            seq - 1,
            seq,
        )
        with _guard():
            if not rows or rows[-1][0] != seq or (seq > 1 and len(rows) != 2):
                raise IndexUnusable(f"the index has no position for tx {seq}")
            prev = GENESIS if seq == 1 else ChainLink(seq - 1, str(rows[0][4]))
            _, segment, start, end, digest = rows[-1]
            assert isinstance(start, int) and isinstance(end, int)
            with open(txlog.path(str(segment)), "rb") as handle:
                handle.seek(start)
                line = handle.read(end - start)
            try:
                document = verify_line(line, prev)
            except ChainError as exc:
                raise IndexUnusable(f"tx {seq} is not where the index says: {exc}") from None
            if document.get("hash") != digest:
                raise IndexUnusable(f"tx {seq} has another hash than the index says")
            return tx_from_json(document)

    def table(self, name: str) -> set[tuple[object, ...]]:
        assert name in _TABLES
        return set(self._rows(f"SELECT * FROM {name}"))


def _decode(body: object, entity: EntityId) -> Revision:
    revision = revision_from_json(as_object(json.loads(str(body)), "revision"))
    if revision.id != entity:
        raise IndexUnusable("an index row does not match its id")
    return revision


class ScanView(Protocol):
    """What ``FileSnapshot`` scans read: a replay ``ViewAt`` or an ``IndexedView``."""

    @property
    def seq(self) -> int: ...

    def current(self, entity: EntityId) -> Revision | None: ...

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterable[Revision]: ...

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]: ...

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None: ...

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]: ...

    def tx(self, seq: int) -> TxRecord: ...


class IndexedView:
    """The graph as of ``seq``: the index plus the txs after it (``tail``), read on demand."""

    def __init__(
        self, reader: IndexReader, seq: int, tail: Sequence[TxRecord], txlog: SegmentedLog
    ) -> None:
        self.seq = seq
        self._reader = reader
        self._txlog = txlog
        self._tail = {tx.seq: tx for tx in tail}
        self._changed: dict[EntityId, Revision] = {}
        self._aliases: dict[str, tuple[EntityId, int]] = {}
        for tx in tail:
            for revision in tx.revisions:
                self._changed[revision.id] = revision
            for entity, alias in tx.alias_allocations:
                self._aliases.setdefault(alias, (entity, tx.seq))
        self._decoded: dict[EntityId, Revision | None] = {}

    def current(self, entity: EntityId) -> Revision | None:
        changed = self._changed.get(entity)
        if changed is not None:
            return changed
        if entity not in self._decoded:
            self._decoded[entity] = self._reader.revision(entity)
        return self._decoded[entity]

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        found = {}
        for revision in self._reader.revisions(types):
            if revision.id not in self._changed:
                self._decoded[revision.id] = revision
                found[revision.id] = revision
        for entity, revision in self._changed.items():
            if not types or revision.type in types:
                found[entity] = revision
        for entity in sorted(found):
            yield found[entity]

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> list[tuple[EntityId, EdgeRecord]]:
        found = [
            (source, edge)
            for source, edge in self._reader.incoming(entity, rel)
            if source not in self._changed
        ]
        for source, revision in self._changed.items():
            found += [
                (source, edge)
                for edge in revision.edges
                if edge.target == entity and (rel is None or edge.rel == rel)
            ]
        return sorted(found, key=lambda pair: (pair[0], pair[1].edge_id))

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        """Bindings in the index are older than those of the tail, so they win (B1.8)."""
        bound = self._reader.alias(alias) or self._aliases.get(alias)
        if bound is None:
            return None
        entity = bound[0]
        current = self.current(entity)
        return entity, current is not None and current.alias == alias

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> list[EntityId]:
        suffix = hex_suffix.lower()
        entities = set(self._reader.entity_ids()) | set(self._changed)
        return sorted(e for e in entities if e.hex.endswith(suffix))[:limit]

    def tx(self, seq: int) -> TxRecord:
        if not 1 <= seq <= self.seq:
            raise ValueError(f"seq {seq} is outside 1..{self.seq}")
        tx = self._tail.get(seq)
        return tx if tx is not None else self._reader.tx(seq, self._txlog)


# ---- writing -----------------------------------------------------------


def _bound(meta: IndexMeta, graph_id: GraphId, view: GraphView, spans: Sequence[TxSpan]) -> bool:
    """The index describes a prefix of this log (writer side of the binding)."""
    seq = meta.seq
    if meta.graph_id != str(graph_id) or not 1 <= seq <= view.seq:
        return False
    return view.at().tx(seq).hash == meta.link.hash and spans[seq - 1].end == meta.position


def update_index(
    layout: GraphLayout,
    graph_id: GraphId,
    view: GraphView,
    spans: Sequence[TxSpan],
    *,
    syncer: Syncer,
    faults: FaultHook | None,
) -> None:
    """Bring the index to ``view.seq``: apply the missing txs to a copy, or rebuild it.

    The caller holds the graph lock. Raises one of ``INDEX_ERRORS`` on failure;
    the old index then stays in place (still bound, only further behind).
    """
    seq = view.seq
    if seq == 0:
        return
    path = layout.index
    meta = None
    try:
        reader = IndexReader.open(path)
    except IndexUnusable:
        reader = None
    if reader is not None:
        meta = reader.meta
        reader.close()
    if meta is not None and _bound(meta, graph_id, view, spans) and meta.seq == seq:
        return
    ensure_dir(path.parent, syncer)
    temp = path.with_name(path.name + TEMP_SUFFIX)
    temp.unlink(missing_ok=True)
    at = view.at()
    if meta is not None and _bound(meta, graph_id, view, spans):
        shutil.copyfile(path, temp)
        with closing(_open_for_writing(temp, create=False)) as db:
            _apply(db, [at.tx(s) for s in range(meta.seq + 1, seq + 1)], spans)
            _finish(db, graph_id, at, spans)
    else:
        with closing(_open_for_writing(temp, create=True)) as db:
            _fill(db, at, spans)
            _finish(db, graph_id, at, spans)
    fd = os.open(temp, os.O_RDONLY | os.O_CLOEXEC)
    try:
        syncer.sync_file(fd)
    finally:
        os.close(fd)
    fire(faults, CrashPoint.INDEX_BEFORE_RENAME)
    os.replace(temp, path)
    fire(faults, CrashPoint.INDEX_AFTER_RENAME)
    syncer.sync_dir(path.parent)


def _open_for_writing(path: Path, *, create: bool) -> sqlite3.Connection:
    """A private temp file: no journal, no syncs; the caller syncs it before the rename."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    if create:
        db.executescript(_SCHEMA)  # commits on its own, so before the transaction
    db.execute("BEGIN")
    return db


def _fill(db: sqlite3.Connection, view: ViewAt, spans: Sequence[TxSpan]) -> None:
    rows = expected_rows(view, spans)
    db.executemany("INSERT INTO entity VALUES (?, ?, ?)", rows["entity"])
    db.executemany("INSERT INTO alias VALUES (?, ?, ?)", rows["alias"])
    db.executemany("INSERT INTO edge_in VALUES (?, ?, ?, ?, ?)", rows["edge_in"])
    db.executemany("INSERT INTO tx VALUES (?, ?, ?, ?, ?)", rows["tx"])


def _apply(db: sqlite3.Connection, txs: Sequence[TxRecord], spans: Sequence[TxSpan]) -> None:
    for tx in txs:
        for revision in tx.revisions:
            db.execute("INSERT OR REPLACE INTO entity VALUES (?, ?, ?)", _entity_row(revision))
            db.execute("DELETE FROM edge_in WHERE source = ?", (revision.id.bytes,))
            db.executemany("INSERT INTO edge_in VALUES (?, ?, ?, ?, ?)", _edge_rows(revision))
        db.executemany(
            "INSERT OR IGNORE INTO alias VALUES (?, ?, ?)",
            [(alias, entity.bytes, tx.seq) for entity, alias in tx.alias_allocations],
        )
        db.execute("INSERT INTO tx VALUES (?, ?, ?, ?, ?)", _tx_row(tx, spans[tx.seq - 1]))


def _finish(
    db: sqlite3.Connection, graph_id: GraphId, view: ViewAt, spans: Sequence[TxSpan]
) -> None:
    last = view.tx(view.seq)
    db.execute("DELETE FROM meta")
    db.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        _meta_rows(graph_id, ChainLink(last.seq, last.hash), spans[view.seq - 1]),
    )
    db.execute("COMMIT")


# ---- verifying ---------------------------------------------------------


def index_difference(
    path: Path, graph_id: GraphId, view: GraphView, spans: Sequence[TxSpan]
) -> str | None:
    """Why the index at ``path`` does not describe this log; ``None`` when it does or is absent."""
    try:
        reader = IndexReader.open(path)
    except IndexUnusable as exc:
        return str(exc)
    if reader is None:
        return None
    try:
        meta = reader.meta
        if meta.graph_id != str(graph_id):
            return "the index belongs to another graph"
        if not _bound(meta, graph_id, view, spans):
            return f"the index does not match the tx log at seq {meta.seq}"
        expected = expected_rows(view.at(meta.seq), spans)
        for name in _TABLES:
            actual = reader.table(name)
            if actual != expected[name]:
                missing, extra = len(expected[name] - actual), len(actual - expected[name])
                return f"table {name} differs: {missing} rows missing, {extra} unexpected"
        return None
    except IndexUnusable as exc:
        return str(exc)
    finally:
        reader.close()
