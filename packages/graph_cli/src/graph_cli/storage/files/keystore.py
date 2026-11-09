"""The key store of one sealed graph (plan T16): its data keys, wrapped, in one SQLite file.

The store lives outside the data root (``<keys dir>/<graph id>.sqlite``), so a backup of
the data root holds ciphertext only and the keys follow a backup schedule of their own.
Every data key (DEK) is a random 256-bit key with a ``Kid`` (UUIDv7), wrapped with AES-KW
under the key-encryption key (KEK) from a secret file. ``KeyStore`` is the ``KeyRing`` of a
sealed graph.

- **Scopes**: one key per entity and one per tx (unique per subject, created on first
  use), one per stored file (no subject; the blob header names its kid).
- **Durability**: rollback journal and ``synchronous = FULL``. A new key is committed before
  ``entity_key``, ``tx_key`` or ``file_key`` returns it, so it is durable before the record
  that uses it. A key that no record uses is harmless; ``check`` reports it as an orphan.
- **Shredding**: ``shred`` drops the wrapped key and keeps the row (kid, scope, subject,
  purge event) as evidence, commits, then runs ``VACUUM``. ``secure_delete`` zeroes the
  freed bytes at the commit; ``temp_store = MEMORY`` keeps the copy ``VACUUM`` makes off the
  disk. What the file system keeps of a deleted rollback journal is a wrapped key, worthless
  once the KEK that wrapped it is rotated away.
- **Caching**: the wrapped keys are read in one query on first use, each key is unwrapped
  on its first use, and both are kept in memory. ``refresh`` and any use
  more than ``CACHE_CHECK_S`` after the last check look at ``PRAGMA data_version``; when
  another connection has shredded or removed keys since, the cache is dropped, so a
  long-running process stops decrypting what a purge elsewhere destroyed. Not on every use:
  the check reads the file header under a shared lock, which costs as much as dozens of
  decryptions.
- **Concurrency**: a new key is created in a ``BEGIN IMMEDIATE`` transaction that looks for
  the subject's key first, so processes racing for one subject get the same key.

Imports the cipher primitives, so import it only where a graph is sealed.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Final
from uuid import UUID

from graph_cli.kernel.clock import Clock, format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, GraphId, IdProvider, TxId
from graph_cli.kernel.sealing import KeyScope, KeyUnavailable, Kid, Sealed
from graph_cli.storage.common import crypto
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.files.durable import OsSyncer, Syncer

KEYSTORE_FORMAT: Final = "graph_cli.keystore/1"
BUSY_TIMEOUT_S: Final = 30.0
CACHE_CHECK_S: Final = 1.0
"""How long cached keys are used before checking for shreds by other connections."""
SECRET_FILE_MODE: Final = 0o600
SECRET_DIR_MODE: Final = 0o700
_KEK_FILE_FORBIDDEN: Final = 0o037  # group write or execute, anything for others
_KEK_TEXT: Final = re.compile(r"[0-9a-f]{64}\n?")
_KEK_ID_INFO: Final = b"graph_cli.kek-id/1"

_SCHEMA: Final = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE kek (kek_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, retired_at TEXT)
    WITHOUT ROWID;
CREATE TABLE dek (
    kid BLOB PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('entity', 'tx', 'file')),
    subject BLOB,
    kek_id TEXT NOT NULL REFERENCES kek (kek_id),
    wrapped BLOB,
    created_at TEXT NOT NULL,
    shredded_at TEXT,
    purge_event BLOB,
    CHECK ((wrapped IS NULL) = (shredded_at IS NOT NULL)),
    CHECK ((shredded_at IS NULL) = (purge_event IS NULL)),
    CHECK ((subject IS NULL) = (scope = 'file'))
) WITHOUT ROWID;
CREATE UNIQUE INDEX dek_subject ON dek (scope, subject) WHERE subject IS NOT NULL;
"""
_SHREDS: Final = "shreds"  # meta: bumped by every shred and collect, read by other processes


@dataclass(frozen=True, slots=True)
class Kek:
    """A key-encryption key and its id, a digest that names the key without revealing it."""

    kek_id: str
    key: bytes = field(repr=False)

    @classmethod
    def of(cls, key: bytes) -> Kek:
        if len(key) != crypto.KEY_BYTES:
            raise ValueError(f"a key-encryption key has {crypto.KEY_BYTES} bytes, got {len(key)}")
        return cls(crypto.hkdf_sha256(key, salt=None, info=_KEK_ID_INFO, length=16).hex(), key)


def read_kek_file(path: Path) -> Kek:
    """The KEK in ``path``: 64 lowercase hex digits and an optional newline, in a regular file
    (a symlink to one, as in container secret mounts, is fine) that nobody but its owner may
    write and only its owner and group may read."""
    shown = {"file": str(path)}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            status = os.fstat(fd)
            data = os.read(fd, 128) if stat.S_ISREG(status.st_mode) else b""
        finally:
            os.close(fd)
    except OSError as exc:
        raise GraphCliError(
            "config.invalid",
            f"the key-encryption key file {path} cannot be read: {exc.strerror or exc}",
            details=shown,
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        raise GraphCliError(
            "config.invalid", f"the key-encryption key file {path} is not a file", details=shown
        )
    if status.st_mode & _KEK_FILE_FORBIDDEN:
        raise GraphCliError(
            "config.invalid",
            f"the key-encryption key file {path} may be read or written by others; "
            f"chmod 600 {path}",
            details=shown,
        )
    text = data.decode("ascii", errors="replace")
    if _KEK_TEXT.fullmatch(text) is None:
        raise GraphCliError(
            "config.invalid",
            f"the key-encryption key file {path} does not hold 64 lowercase hex digits",
            details=shown,
        )
    return Kek.of(bytes.fromhex(text[:64]))


def create_kek_file(path: Path, *, syncer: Syncer | None = None) -> Kek:
    """Write a new random KEK to ``path`` (mode 0600, new parent directories 0700, synced).

    Refuses an existing file: replacing a KEK makes every key wrapped under it unusable.
    """
    syncer = syncer or OsSyncer()
    path.parent.mkdir(mode=SECRET_DIR_MODE, parents=True, exist_ok=True)
    kek = Kek.of(crypto.new_key())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, SECRET_FILE_MODE)
    except FileExistsError:
        raise GraphCliError(
            "config.invalid",
            f"the key-encryption key file {path} already exists; it is never replaced",
            details={"file": str(path)},
        ) from None
    try:
        os.write(fd, kek.key.hex().encode("ascii") + b"\n")
        syncer.sync_file(fd)
    finally:
        os.close(fd)
    syncer.sync_dir(path.parent)
    return kek


@dataclass(frozen=True, slots=True)
class KeyCheck:
    """What ``KeyStore.check`` found, each sorted by kid."""

    missing: tuple[Kid, ...]  # referenced by a record, absent from the store
    shredded_unrecorded: tuple[Kid, ...]  # shredded, but its purge event does not name it
    orphans: tuple[Kid, ...]  # live, referenced by no record


@dataclass(frozen=True, slots=True)
class _Key:
    dek: bytes = field(repr=False)
    enc: bytes = field(repr=False)
    mac: bytes = field(repr=False)


class KeyStore:
    """The ``KeyRing`` of a sealed graph, backed by its key store file.

    ``open`` raises ``CryptoError`` for an envelope that does not authenticate under its
    key and ``aad``. A key wrapped under a KEK that is not configured is ``config.invalid``,
    a wrapped key that does not unwrap is ``backend.corrupt``, a failing SQLite call is
    ``backend.unavailable``. Thread-safe; one instance per graph and process.
    """

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        keks: Sequence[Kek],
        *,
        clock: Clock,
        ids: IdProvider,
        faults: FaultHook | None,
    ) -> None:
        self.path = path
        self._conn = connection
        self._current = keks[0]
        self._keks = {kek.kek_id: kek for kek in keks}
        self._clock = clock
        self._ids = ids
        self._faults = faults
        self._lock = threading.RLock()
        self._keys: dict[Kid, _Key] = {}
        self._wrapped: dict[Kid, tuple[str, bytes | None]] | None = None
        self._subjects: dict[tuple[KeyScope, UUID], Kid] = {}
        self._data_version = self._pragma_data_version()
        self._checked_at = time.monotonic()
        self._shreds = self._shred_count()

    @classmethod
    def create(
        cls,
        path: Path,
        graph_id: GraphId,
        kek: Kek,
        *,
        clock: Clock,
        ids: IdProvider,
        faults: FaultHook | None = None,
        syncer: Syncer | None = None,
    ) -> KeyStore:
        """A new, empty key store at ``path`` (mode 0600); refuses an existing file."""
        if path.exists() or path.is_symlink():
            raise GraphCliError(
                "config.invalid",
                f"the key store {path} already exists",
                details={"file": str(path)},
            )
        syncer = syncer or OsSyncer()
        path.parent.mkdir(mode=SECRET_DIR_MODE, parents=True, exist_ok=True)
        temp = _new_private_file(path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp"))
        try:
            with _sqlite_errors(path), closing(_connect(temp)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [("format", KEYSTORE_FORMAT), ("graph_id", str(graph_id)), (_SHREDS, "0")],
                )
                conn.execute("COMMIT")
            os.replace(temp, path)
            syncer.sync_dir(path.parent)
        finally:
            temp.unlink(missing_ok=True)
        return cls.load(path, graph_id, (kek,), clock=clock, ids=ids, faults=faults)

    @classmethod
    def load(
        cls,
        path: Path,
        graph_id: GraphId,
        keks: Sequence[Kek],
        *,
        clock: Clock,
        ids: IdProvider,
        faults: FaultHook | None = None,
    ) -> KeyStore:
        """The key store at ``path``. ``keks`` lists the current KEK first, which wraps new
        keys, then earlier ones still needed to unwrap keys during a rotation."""
        if not keks:
            raise ValueError("a key store needs at least one key-encryption key")
        if not path.is_file():
            raise GraphCliError(
                "config.invalid",
                f"the key store {path} does not exist; the data of graph {graph_id} cannot be "
                "read without it (restore it from a key store backup)",
                details={"file": str(path)},
            )
        with _sqlite_errors(path):
            conn = _connect(path)
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                _check_meta(path, meta, graph_id)
                return cls(path, conn, keks, clock=clock, ids=ids, faults=faults)
            except BaseException:
                conn.close()
                raise

    def close(self) -> None:
        with self._lock:
            self._keys.clear()
            self._wrapped = None
            self._conn.close()

    def __enter__(self) -> KeyStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # KeyRing

    def entity_key(self, entity: EntityId) -> Kid:
        return self._subject_key("entity", entity)

    def tx_key(self, tx: TxId) -> Kid:
        return self._subject_key("tx", tx)

    def file_key(self) -> Kid:
        with self._lock, _sqlite_errors(self.path):
            return self._mint("file", None)

    def mac_key(self, kid: Kid | None) -> bytes | None:
        if kid is None:
            return None
        with self._lock, _sqlite_errors(self.path):
            return self._key(kid).mac

    def seal(self, kid: Kid, plaintext: bytes, aad: bytes) -> Sealed:
        with self._lock, _sqlite_errors(self.path):
            key = self._key(kid).enc
        nonce, ct = crypto.seal(key, plaintext, aad)
        return Sealed(kid, nonce, ct)

    def open(self, sealed: Sealed, aad: bytes) -> bytes:
        with self._lock, _sqlite_errors(self.path):
            key = self._key(sealed.kid).enc
        return crypto.open_sealed(key, sealed.nonce, sealed.ct, aad)

    def is_shredded(self, kid: Kid) -> bool:
        """Whether ``kid`` is shredded; ``False`` for a live or a missing key."""
        with self._lock, _sqlite_errors(self.path):
            row = self._conn.execute(
                "SELECT wrapped IS NULL FROM dek WHERE kid = ?", (kid.bytes,)
            ).fetchone()
        return row is not None and bool(row[0])

    def shred(self, kids: Iterable[Kid], purge_event: EventId) -> None:
        """Destroy the wrapped keys of ``kids`` and record ``purge_event``, then ``VACUUM``.

        All or nothing: a kid absent from the store raises ``KeyUnavailable`` before
        anything changes. A kid shredded before keeps its first purge event, so an
        interrupted purge can run again.
        """
        targets = sorted(set(kids))
        with self._lock, _sqlite_errors(self.path):
            now = format_timestamp(self._clock.now())
            with self._write() as conn:
                for kid in targets:
                    if (
                        conn.execute("SELECT 1 FROM dek WHERE kid = ?", (kid.bytes,)).fetchone()
                        is None
                    ):
                        raise KeyUnavailable(kid, "missing")
                conn.executemany(
                    "UPDATE dek SET wrapped = NULL, shredded_at = ?, purge_event = ? "
                    "WHERE kid = ? AND wrapped IS NOT NULL",
                    [(now, purge_event.bytes, kid.bytes) for kid in targets],
                )
                self._bump_shreds(conn)
                fire(self._faults, CrashPoint.SHRED_BEFORE_COMMIT)
            self._forget()
            fire(self._faults, CrashPoint.SHRED_BEFORE_VACUUM)
            self._conn.execute("VACUUM")

    def refresh(self) -> None:
        """Pick up shreds and removals by other connections now, e.g. when a read begins."""
        with self._lock, _sqlite_errors(self.path):
            self._sync_cache(now=True)

    # Maintenance

    def check(self, referenced: Iterable[Kid], purges: Mapping[EventId, Iterable[Kid]]) -> KeyCheck:
        """Compare the store with the kids the graph's records reference and with the kids
        each recorded purge event names (the logic behind ``keys verify``)."""
        named = {event: frozenset(kids) for event, kids in purges.items()}
        wanted = set(referenced)
        with self._lock, _sqlite_errors(self.path):
            rows = self._conn.execute(
                "SELECT kid, wrapped IS NULL, purge_event FROM dek ORDER BY kid"
            ).fetchall()
        stored: set[Kid] = set()
        unrecorded: list[Kid] = []
        orphans: list[Kid] = []
        for raw_kid, shredded, raw_event in rows:
            kid = Kid(UUID(bytes=raw_kid))
            stored.add(kid)
            if shredded:
                if kid not in named.get(EventId(UUID(bytes=raw_event)), ()):
                    unrecorded.append(kid)
            elif kid not in wanted:
                orphans.append(kid)
        return KeyCheck(tuple(sorted(wanted - stored)), tuple(unrecorded), tuple(orphans))

    def collect(self, orphans: Iterable[Kid], *, created_before: datetime) -> int:
        """Delete the live keys among ``orphans`` created before ``created_before``, then
        ``VACUUM``; returns how many were deleted (the logic behind ``keys verify --gc``).

        The caller holds the graph lock and took ``orphans`` from a ``check`` against the
        whole log. The cutoff spares the key of a file staged before the lock whose tx is
        not written yet. Shredded rows stay as evidence.
        """
        cutoff = format_timestamp(created_before)
        with self._lock, _sqlite_errors(self.path):
            with self._write() as conn:
                deleted = 0
                for kid in sorted(set(orphans)):
                    deleted += conn.execute(
                        "DELETE FROM dek WHERE kid = ? AND wrapped IS NOT NULL AND created_at < ?",
                        (kid.bytes, cutoff),
                    ).rowcount
                self._bump_shreds(conn)
            self._forget()
            self._conn.execute("VACUUM")
        return deleted

    def backup(self, dest: Path, *, syncer: Syncer | None = None) -> None:
        """Write a consistent copy of the store to ``dest`` (mode 0600, synced) with the
        SQLite backup API; refuses an existing ``dest``."""
        if dest.exists() or dest.is_symlink():
            raise GraphCliError(
                "usage.invalid_argument", f"{dest} already exists", details={"file": str(dest)}
            )
        syncer = syncer or OsSyncer()
        temp = _new_private_file(dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.tmp"))
        try:
            with self._lock, _sqlite_errors(self.path), closing(_connect(temp)) as target:
                self._conn.backup(target)
            fd = os.open(temp, os.O_RDONLY | os.O_CLOEXEC)
            try:
                syncer.sync_file(fd)
            finally:
                os.close(fd)
            os.replace(temp, dest)
            syncer.sync_dir(dest.parent)
        finally:
            temp.unlink(missing_ok=True)

    # Internals; callers hold ``self._lock`` and map SQLite errors.

    def _subject_key(self, scope: KeyScope, subject: UUID) -> Kid:
        with self._lock, _sqlite_errors(self.path):
            self._sync_cache()
            kid = self._subjects.get((scope, subject))
            if kid is None:
                kid = self._find(scope, subject) or self._mint(scope, subject)
                self._subjects[(scope, subject)] = kid
            return kid

    def _find(self, scope: KeyScope, subject: UUID) -> Kid | None:
        row = self._conn.execute(
            "SELECT kid FROM dek WHERE scope = ? AND subject = ?", (scope, subject.bytes)
        ).fetchone()
        return None if row is None else Kid(UUID(bytes=row[0]))

    def _mint(self, scope: KeyScope, subject: UUID | None) -> Kid:
        kek = self._current
        with self._write() as conn:
            # Another process may have created the subject's key since the lookup.
            found = None if subject is None else self._find(scope, subject)
            if found is not None:
                return found
            kid = Kid(self._ids.new_id())
            dek = crypto.new_key()
            now = format_timestamp(self._clock.now())
            conn.execute(
                "INSERT OR IGNORE INTO kek (kek_id, created_at) VALUES (?, ?)", (kek.kek_id, now)
            )
            conn.execute(
                "INSERT INTO dek (kid, scope, subject, kek_id, wrapped, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    kid.bytes,
                    scope,
                    None if subject is None else subject.bytes,
                    kek.kek_id,
                    crypto.wrap_key(kek.key, dek),
                    now,
                ),
            )
            fire(self._faults, CrashPoint.KEYS_BEFORE_COMMIT)
        fire(self._faults, CrashPoint.KEYS_AFTER_COMMIT)
        self._keys[kid] = _derive(dek)
        return kid

    def _key(self, kid: Kid) -> _Key:
        self._sync_cache()
        cached = self._keys.get(kid)
        if cached is not None:
            return cached
        if self._wrapped is None:  # one query for all, instead of one per key
            self._wrapped = {
                Kid(UUID(bytes=raw)): (kek_id, wrapped)
                for raw, kek_id, wrapped in self._conn.execute(
                    "SELECT kid, kek_id, wrapped FROM dek"
                )
            }
        row = self._wrapped.get(kid)
        if row is None:  # created by another connection since
            row = self._conn.execute(
                "SELECT kek_id, wrapped FROM dek WHERE kid = ?", (kid.bytes,)
            ).fetchone()
            if row is None:
                raise KeyUnavailable(kid, "missing")
            self._wrapped[kid] = row
        kek_id, wrapped = row
        if wrapped is None:
            raise KeyUnavailable(kid, "shredded")
        kek = self._keks.get(kek_id)
        if kek is None:
            raise GraphCliError(
                "config.invalid",
                f"key {kid} of the key store {self.path} is wrapped under the key-encryption "
                f"key {kek_id}, which is not configured",
                details={"file": str(self.path), "kek_id": kek_id},
            )
        try:
            key = _derive(crypto.unwrap_key(kek.key, wrapped))
        except crypto.CryptoError:
            raise GraphCliError(
                "backend.corrupt",
                f"the key store {self.path} is damaged: key {kid} does not unwrap",
                details={"file": str(self.path)},
            ) from None
        self._keys[kid] = key
        return key

    def _sync_cache(self, *, now: bool = False) -> None:
        """Drop the caches when another connection shredded or removed keys; checks at most
        every ``CACHE_CHECK_S`` unless ``now``."""
        checked_at = time.monotonic()
        if not now and checked_at - self._checked_at < CACHE_CHECK_S:
            return
        self._checked_at = checked_at
        version = self._pragma_data_version()
        if version == self._data_version:
            return
        self._data_version = version
        shreds = self._shred_count()
        if shreds != self._shreds:
            self._shreds = shreds
            self._keys.clear()
            self._wrapped = None
            self._subjects.clear()

    def _forget(self) -> None:
        self._keys.clear()
        self._wrapped = None
        self._subjects.clear()
        self._shreds = self._shred_count()

    def _bump_shreds(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = ?", (_SHREDS,))

    def _shred_count(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (_SHREDS,)).fetchone()
        return int(row[0])

    def _pragma_data_version(self) -> int:
        return int(self._conn.execute("PRAGMA data_version").fetchone()[0])

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")


def _derive(dek: bytes) -> _Key:
    return _Key(dek, crypto.subkey(dek, "enc"), crypto.subkey(dek, "mac"))


def _connect(path: Path) -> sqlite3.Connection:
    """A connection to an existing file (``mode=rw`` never creates one) with the pragmas of
    the key store."""
    conn = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw",
        uri=True,
        timeout=BUSY_TIMEOUT_S,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        for pragma in (
            "journal_mode = DELETE",
            "synchronous = FULL",
            "fullfsync = ON",
            "secure_delete = ON",
            "temp_store = MEMORY",
            "foreign_keys = ON",
        ):
            conn.execute(f"PRAGMA {pragma}").fetchall()
    except BaseException:
        conn.close()
        raise
    return conn


def _check_meta(path: Path, meta: Mapping[str, str], graph_id: GraphId) -> None:
    shown = {"file": str(path)}
    if meta.get("format") != KEYSTORE_FORMAT:
        raise GraphCliError(
            "format.unsupported_version",
            f"the key store {path} has the format {meta.get('format')!r}, "
            f"expected {KEYSTORE_FORMAT!r}",
            details=shown,
        )
    if meta.get("graph_id") != str(graph_id):
        raise GraphCliError(
            "config.invalid",
            f"the key store {path} belongs to graph {meta.get('graph_id')}, not {graph_id}",
            details=shown,
        )
    if not meta.get(_SHREDS, "").isdigit():
        raise GraphCliError(
            "backend.corrupt", f"the key store {path} is damaged: no shred counter", details=shown
        )


def _new_private_file(path: Path) -> Path:
    """A new, empty file at ``path`` (mode 0600)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    os.close(os.open(path, flags, SECRET_FILE_MODE))
    return path


@contextmanager
def _sqlite_errors(path: Path) -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as exc:
        raise GraphCliError(
            "backend.unavailable",
            f"the key store {path} cannot be used: {exc}",
            details={"file": str(path)},
        ) from exc
