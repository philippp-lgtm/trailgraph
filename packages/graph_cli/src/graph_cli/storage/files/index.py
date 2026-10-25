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

A writer trusts the index only after the SHA-256 digest of the log bytes up
to the index position matches the one recorded with it (``log_digest``):
equal bytes give the result of the full verification that the writer of
the index ran or inherited, so only the tail after it needs verifying (plan
"Incremental writer checks"). ``full_verified_seq`` names the last full
replay behind the index; writers replay fully again after
``FULL_CHECK_EVERY`` txs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
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
    revision_content_hash,
    revision_from_json,
    revision_to_json,
    tx_from_json,
)
from graph_cli.storage.common.chain import GENESIS, ChainError, ChainLink, verify_line
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.common.view import GraphView, ViewAt, alias_parts
from graph_cli.storage.files.durable import TEMP_SUFFIX, Syncer, ensure_dir
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.segments import LogPosition, SegmentedLog

INDEX_FORMAT: Final = "graph_cli.index/2"
READ_FORMATS: Final = frozenset({"graph_cli.index/1", INDEX_FORMAT})
"""Readers also use an index of format 1 (no digest); writers need format 2."""
FULL_CHECK_EVERY: Final = 200
"""Txs after the last full replay before a writer replays the whole log again."""

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
CREATE TABLE idempotency (
    principal TEXT NOT NULL, key TEXT NOT NULL, seq INTEGER NOT NULL,
    PRIMARY KEY (principal, key)
) WITHOUT ROWID;
"""
_TABLES: Final = ("entity", "alias", "edge_in", "tx", "idempotency")


class IndexUnusable(Exception):
    """The index file cannot be used; the message says why."""


INDEX_ERRORS: Final = (OSError, sqlite3.Error, IndexUnusable)
"""What a failed index update raises; the commit stands, the index stays behind."""


@dataclass(frozen=True, slots=True)
class TxSpan:
    """Where one tx line lies in the log."""

    start: LogPosition
    end: LogPosition


@dataclass(frozen=True, slots=True)
class SegmentDigest:
    """SHA-256 of the first ``size`` bytes of one log segment."""

    segment: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class IndexMeta:
    graph_id: str
    link: ChainLink  # the last tx the index covers
    position: LogPosition  # the log position after that tx
    format: str = INDEX_FORMAT
    digest: tuple[SegmentDigest, ...] | None = None  # the log up to ``position`` (format 2)
    full_verified_seq: int | None = None  # the last full replay behind the index (format 2)

    @property
    def seq(self) -> int:
        return self.link.seq


def log_digest(
    txlog: SegmentedLog, position: LogPosition, known: Sequence[SegmentDigest] = ()
) -> tuple[SegmentDigest, ...]:
    """The digest of the log up to ``position``: every segment before it whole, its own
    segment up to the offset. ``known`` digests of segments that were already sealed
    (not the last of ``known``) are reused, since a sealed segment never changes."""
    sealed = {d.segment: d for d in known[:-1]}
    found = []
    for _, name in txlog.segments():
        if name == position.segment:
            found.append(_hash_segment(txlog, name, position.offset))
            return tuple(found)
        found.append(sealed.get(name) or _hash_segment(txlog, name, None))
    raise IndexUnusable(f"the log has no segment {position.segment}")


def _hash_segment(txlog: SegmentedLog, segment: str, size: int | None) -> SegmentDigest:
    digest = hashlib.sha256()
    read = 0
    with open(txlog.path(segment), "rb") as handle:
        while size is None or read < size:
            chunk = handle.read(1 << 20 if size is None else min(1 << 20, size - read))
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
    if size is not None and read != size:
        raise IndexUnusable(f"segment {segment} is shorter than the index says")
    return SegmentDigest(segment, read, digest.hexdigest())


def _digest_json(digest: Sequence[SegmentDigest]) -> str:
    return json.dumps([[d.segment, d.size, d.sha256] for d in digest], separators=(",", ":"))


def _parse_digest(raw: str) -> tuple[SegmentDigest, ...]:
    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("log_digest is not a list")
    found = []
    for item in items:
        segment, size, sha = item
        if not (isinstance(segment, str) and isinstance(size, int) and isinstance(sha, str)):
            raise ValueError("a log_digest entry has a bad shape")
        found.append(SegmentDigest(segment, size, sha))
    return tuple(found)


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
    txs = [view.tx(seq) for seq in range(1, view.seq + 1)]
    rows["tx"] = {_tx_row(tx, spans[tx.seq - 1]) for tx in txs}
    first: dict[tuple[str, str], int] = {}
    for tx in txs:
        key = _idempotency_key(tx)
        if key is not None:
            first.setdefault(key[:2], key[2])  # the first use of a key wins (B3.12)
    rows["idempotency"] = {(principal, key, seq) for (principal, key), seq in first.items()}
    return rows


def _idempotency_key(tx: TxRecord) -> tuple[str, str, int] | None:
    if tx.idempotency_key is None:
        return None
    return tx.actor.principal.id or "", tx.idempotency_key, tx.seq


def _meta_rows(meta: IndexMeta) -> list[tuple[str, str]]:
    assert meta.digest is not None and meta.full_verified_seq is not None
    return [
        ("format", INDEX_FORMAT),
        ("graph_id", meta.graph_id),
        ("seq", str(meta.link.seq)),
        ("hash", meta.link.hash),
        ("segment", meta.position.segment),
        ("offset", str(meta.position.offset)),
        ("log_digest", _digest_json(meta.digest)),
        ("full_verified_seq", str(meta.full_verified_seq)),
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
                found = values.get("format")
                if found not in READ_FORMATS:
                    raise IndexUnusable(f"the index format is not {INDEX_FORMAT}")
                current = found == INDEX_FORMAT
                meta = IndexMeta(
                    graph_id=values["graph_id"],
                    link=ChainLink(int(values["seq"]), values["hash"]),
                    position=LogPosition(values["segment"], int(values["offset"])),
                    format=found,
                    digest=_parse_digest(values["log_digest"]) if current else None,
                    full_verified_seq=int(values["full_verified_seq"]) if current else None,
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

    def alias_counters(self) -> dict[str, int]:
        """Per prefix, the highest number bound (aliases are never removed, B1.8)."""
        counters: dict[str, int] = {}
        with _guard():
            for (alias,) in self._rows("SELECT alias FROM alias"):
                prefix, number = alias_parts(str(alias))
                counters[prefix] = max(counters.get(prefix, 0), number)
        return counters

    def idempotency(self, principal_id: str, key: str) -> int | None:
        """The seq of the tx that used ``key`` for this principal (format 2 only)."""
        rows = self._rows(
            "SELECT seq FROM idempotency WHERE principal = ? AND key = ?", principal_id, key
        )
        if not rows:
            return None
        seq = rows[0][0]
        if not isinstance(seq, int):
            raise IndexUnusable("an idempotency seq is not an integer")
        return seq

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
    """The graph as of ``seq``: the index plus the txs after it (``tail``), read on demand.

    A writer extends it with its own commits (``add``) and asks what the commit
    checks need besides scans (aliases, idempotency, ``confirm``). With
    ``check_content``, every revision ``current`` decodes from the index must
    match its ``content_hash`` (a writer copies attributes from it).
    """

    def __init__(
        self,
        reader: IndexReader,
        seq: int,
        tail: Sequence[TxRecord],
        txlog: SegmentedLog,
        *,
        check_content: bool = False,
    ) -> None:
        self.seq = reader.meta.seq
        self._reader = reader
        self._txlog = txlog
        self._check_content = check_content
        self._tail: dict[int, TxRecord] = {}
        self._changed: dict[EntityId, Revision] = {}
        self._aliases: dict[str, tuple[EntityId, int]] = {}
        self._decoded: dict[EntityId, Revision | None] = {}
        for tx in tail:
            self.add(tx)
        self.seq = seq

    def add(self, tx: TxRecord) -> None:
        """Overlay the next tx (verified or just committed)."""
        if tx.seq != self.seq + 1:
            raise ValueError(f"tx seq {tx.seq} does not follow {self.seq}")
        self.seq = tx.seq
        self._tail[tx.seq] = tx
        for revision in tx.revisions:
            self._changed[revision.id] = revision
        for entity, alias in tx.alias_allocations:
            self._aliases.setdefault(alias, (entity, tx.seq))

    def current(self, entity: EntityId) -> Revision | None:
        changed = self._changed.get(entity)
        if changed is not None:
            return changed
        if entity not in self._decoded:
            revision = self._reader.revision(entity)
            if (
                self._check_content
                and revision is not None
                and revision_content_hash(revision) != revision.content_hash
            ):
                raise IndexUnusable(f"the index row of {entity} does not match its content hash")
            self._decoded[entity] = revision
        return self._decoded[entity]

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        found = {}
        for revision in self._reader.revisions(types):
            if revision.id not in self._changed:
                if not self._check_content:  # scans are not checked; point reads are
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

    # writer side: only on an index whose log digest was checked

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        found = {}
        for entity in entities:
            current = self.current(entity)
            if current is not None:
                found[entity] = current
        return found

    def alias_bound(self, alias: str) -> bool:
        return self.resolve_alias(alias) is not None

    def alias_counters(self) -> dict[str, int]:
        counters = self._reader.alias_counters()
        for alias in self._aliases:
            prefix, number = alias_parts(alias)
            counters[prefix] = max(counters.get(prefix, 0), number)
        return dict(sorted(counters.items()))

    def lookup_idempotency(self, key: str, principal_id: str) -> TxRecord | None:
        """The first tx with ``key`` for this principal: the index, then the tail (B3.12)."""
        seq = self._reader.idempotency(principal_id, key)
        if seq is not None:
            return self.tx(seq)
        for tx in self._tail.values():
            if tx.idempotency_key == key and (tx.actor.principal.id or "") == principal_id:
                return tx
        return None

    def confirm(self, entity: EntityId) -> None:
        """Check the current revision the index holds for ``entity`` against the log.

        The revision must be the one its tx line carries; that line is read at the
        recorded position and checked against the chain, and its bytes are covered by
        the log digest. Raises ``IndexUnusable`` on any difference.
        """
        if entity in self._changed:
            return  # from the verified tail
        revision = self.current(entity)
        if revision is None:
            return
        with _guard():
            tx = self._reader.tx(revision.tx_seq, self._txlog)
            if revision not in tx.revisions:
                raise IndexUnusable(f"the index row of {entity} differs from its tx")


# ---- writing -----------------------------------------------------------


def _bound(meta: IndexMeta, graph_id: GraphId, view: GraphView, spans: Sequence[TxSpan]) -> bool:
    """The index describes a prefix of this log (writer side of the binding)."""
    seq = meta.seq
    if meta.graph_id != str(graph_id) or not 1 <= seq <= view.seq:
        return False
    return view.at().tx(seq).hash == meta.link.hash and spans[seq - 1].end == meta.position


def rebuild_index(
    layout: GraphLayout,
    graph_id: GraphId,
    view: GraphView,
    spans: Sequence[TxSpan],
    txlog: SegmentedLog,
    *,
    syncer: Syncer,
    faults: FaultHook | None,
) -> IndexMeta | None:
    """Build the index for ``view.seq`` from a full replay of the log; return its meta.

    The caller holds the graph lock and replayed and verified the whole log, so
    the new index records ``full_verified_seq = view.seq``. Raises one of
    ``INDEX_ERRORS`` on failure; the old index then stays in place.
    """
    seq = view.seq
    if seq == 0:
        return None
    at = view.at()
    end = spans[seq - 1].end
    meta = IndexMeta(str(graph_id), ChainLink(seq, at.tx(seq).hash), end)
    meta = replace(meta, digest=log_digest(txlog, end), full_verified_seq=seq)

    def fill(db: sqlite3.Connection) -> None:
        _fill(db, at, spans)
        _finish(db, _meta_rows(meta))

    _publish(layout, fill, copy=False, syncer=syncer, faults=faults)
    return meta


def extend_index(
    layout: GraphLayout,
    graph_id: GraphId,
    base: IndexMeta,
    txs: Sequence[tuple[TxRecord, TxSpan]],
    txlog: SegmentedLog,
    *,
    syncer: Syncer,
    faults: FaultHook | None,
) -> IndexMeta:
    """Apply ``txs``, the ones after ``base``, to a copy of the index ``base`` describes;
    return the new meta.

    The caller holds the graph lock and checked ``base``'s log digest and the txs
    after it. Raises one of ``INDEX_ERRORS`` on failure; the old index stays.
    """
    if not txs:
        return base
    assert base.digest is not None and base.full_verified_seq is not None
    last, span = txs[-1]
    meta = replace(
        base,
        link=ChainLink(last.seq, last.hash),
        position=span.end,
        digest=log_digest(txlog, span.end, base.digest),
    )

    def apply(db: sqlite3.Connection) -> None:
        _apply(db, txs)
        _finish(db, _meta_rows(meta))

    _publish(layout, apply, copy=True, syncer=syncer, faults=faults)
    return meta


def _publish(
    layout: GraphLayout,
    write: Callable[[sqlite3.Connection], None],
    *,
    copy: bool,
    syncer: Syncer,
    faults: FaultHook | None,
) -> None:
    """Write a new index into a temp file (a copy of the old one, or empty), sync it and
    rename it over the old one."""
    path = layout.index
    ensure_dir(path.parent, syncer)
    temp = path.with_name(path.name + TEMP_SUFFIX)
    temp.unlink(missing_ok=True)
    if copy:
        shutil.copyfile(path, temp)
    with closing(_open_for_writing(temp, create=not copy)) as db:
        write(db)
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
    db.executemany("INSERT INTO idempotency VALUES (?, ?, ?)", rows["idempotency"])


def _apply(db: sqlite3.Connection, txs: Sequence[tuple[TxRecord, TxSpan]]) -> None:
    for tx, span in txs:
        for revision in tx.revisions:
            db.execute("INSERT OR REPLACE INTO entity VALUES (?, ?, ?)", _entity_row(revision))
            db.execute("DELETE FROM edge_in WHERE source = ?", (revision.id.bytes,))
            db.executemany("INSERT INTO edge_in VALUES (?, ?, ?, ?, ?)", _edge_rows(revision))
        db.executemany(
            "INSERT OR IGNORE INTO alias VALUES (?, ?, ?)",
            [(alias, entity.bytes, tx.seq) for entity, alias in tx.alias_allocations],
        )
        db.execute("INSERT INTO tx VALUES (?, ?, ?, ?, ?)", _tx_row(tx, span))
        key = _idempotency_key(tx)
        if key is not None:
            db.execute("INSERT OR IGNORE INTO idempotency VALUES (?, ?, ?)", key)


def _finish(db: sqlite3.Connection, meta: Sequence[tuple[str, str]]) -> None:
    db.execute("DELETE FROM meta")
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta)
    db.execute("COMMIT")


# ---- verifying ---------------------------------------------------------


def index_difference(
    path: Path, graph_id: GraphId, view: GraphView, spans: Sequence[TxSpan], txlog: SegmentedLog
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
        tables: tuple[str, ...] = _TABLES
        if meta.format == INDEX_FORMAT:
            assert meta.digest is not None and meta.full_verified_seq is not None
            if log_digest(txlog, meta.position) != meta.digest:
                return "the log digest of the index does not match the tx log"
            if not 1 <= meta.full_verified_seq <= meta.seq:
                return f"full_verified_seq {meta.full_verified_seq} is outside 1..{meta.seq}"
        else:
            tables = tuple(name for name in _TABLES if name != "idempotency")
        expected = expected_rows(view.at(meta.seq), spans)
        for name in tables:
            actual = reader.table(name)
            if actual != expected[name]:
                missing, extra = len(expected[name] - actual), len(actual - expected[name])
                return f"table {name} differs: {missing} rows missing, {extra} unexpected"
        return None
    except (IndexUnusable, OSError) as exc:
        return str(exc)
    finally:
        reader.close()
