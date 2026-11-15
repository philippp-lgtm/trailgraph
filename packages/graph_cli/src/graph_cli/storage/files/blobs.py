"""FileStore blobs: stored file bytes by hash (contracts section 5, section 4.1 KB0-KB2).

``blobs/sha256/<first 2 hex>/<64 hex>`` holds the bytes of one hash, mode
0o444, and never changes. A blob is staged before the graph lock into
``blobs/.tmp-<random>`` (created exclusively, hashed while written, synced)
and published under the lock with one rename, followed by syncs of both
directories. A blob that is already present is trusted, because its name is
its hash; ``verify(rehash=True)`` finds bit rot. Readers ignore ``.tmp-``
entries. ``blobs/`` is created on first use.

Which blobs are referenced is the union of the ``blobs`` lists of all txs;
storage never needs the model for it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence, Set
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final

from graph_cli.kernel.canonical import HASH_RE, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ports.storage import BlobVerification
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.files.durable import FILE_MODE, Syncer, ensure_dir, write_all
from graph_cli.storage.files.layout import STAGING_PREFIX, GraphLayout
from graph_cli.storage.finding_codes import (
    BLOB_HASH_MISMATCH,
    BLOB_MISSING,
    BLOB_ORPHAN,
    BLOB_STAGING_LEFTOVER,
)

CHUNK_BYTES: Final = 1024 * 1024
BLOB_MODE: Final = 0o444
_ALGORITHM: Final = "sha256"
_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_SHARD_RE: Final = re.compile(r"^[0-9a-f]{2}$")
_E, _I = Severity.ERROR, Severity.INFO


def _unavailable(message: str, exc: OSError) -> GraphCliError:
    return GraphCliError("backend.unavailable", f"{message}: {exc.strerror or exc}")


class FileStagedBlob:
    """A synced staging file below ``blobs/``; the bytes of ``hash``."""

    def __init__(self, path: Path, digest: str, size: int) -> None:
        self.path = path
        self.hash = digest
        self.size = size

    def discard(self) -> None:
        with contextlib.suppress(OSError):  # a leftover is reported by verify, removed by gc
            self.path.unlink(missing_ok=True)


def too_large(max_bytes: int) -> GraphCliError:
    return GraphCliError(
        "file.too_large",
        f"the file is larger than {max_bytes} bytes",
        details={"max_bytes": max_bytes},
    )


def stage(
    layout: GraphLayout,
    chunks: Iterable[bytes],
    *,
    max_bytes: int,
    syncer: Syncer,
    faults: FaultHook | None,
) -> FileStagedBlob:
    """Write ``chunks`` to a new staging file, hashing and counting on the way (KB0)."""
    try:
        ensure_dir(layout.blobs, syncer)
        path = layout.blobs / f"{STAGING_PREFIX}{secrets.token_hex(16)}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
    except OSError as exc:
        raise _unavailable("cannot stage the file", exc) from exc
    digest, size = hashlib.sha256(), 0
    try:
        try:
            for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise too_large(max_bytes)
                digest.update(chunk)
                write_all(fd, chunk)
            syncer.sync_file(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise _unavailable("cannot stage the file", exc) from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    fire(faults, CrashPoint.BLOB_AFTER_STAGE)
    return FileStagedBlob(path, f"{_ALGORITHM}:{digest.hexdigest()}", size)


def publish(
    layout: GraphLayout, staged: object, *, syncer: Syncer, faults: FaultHook | None
) -> BlobInfo:
    """Rename a staging file into place under the graph lock (KB1, KB2)."""
    if not isinstance(staged, FileStagedBlob) or staged.path.parent != layout.blobs:
        raise GraphCliError("internal.error", "the staged blob is not from this store")
    target = layout.blob_path(staged.hash)
    try:
        if target.exists():
            staged.discard()
            return BlobInfo(staged.hash, target.stat().st_size)
        if not staged.path.exists():
            raise GraphCliError("internal.error", "the staged blob was discarded")
        ensure_dir(target.parent, syncer)
        os.chmod(staged.path, BLOB_MODE)
        fire(faults, CrashPoint.BLOB_BEFORE_RENAME)
        os.rename(staged.path, target)
        syncer.sync_dir(target.parent)
        syncer.sync_dir(layout.blobs)
    except OSError as exc:
        raise _unavailable("cannot store the file", exc) from exc
    fire(faults, CrashPoint.BLOB_AFTER_RENAME)
    return BlobInfo(staged.hash, staged.size)


def receive(
    layout: GraphLayout, blob: BlobInfo, chunks: Iterable[bytes], *, syncer: Syncer
) -> BlobInfo:
    """A copy of ``blob`` from the graph's other location (``graph-admin files move``):
    staged like a write's bytes (hashed and counted while written, synced), compared with
    the hash and size, mode 0o444, then renamed into place with both directories synced,
    as ``publish`` does. A file that is present with the same size is kept (its name is its
    hash); one with another size is damaged and replaced by the verified copy. Bytes that
    differ from the hash or the size are ``backend.corrupt``, and nothing is placed."""
    try:
        staged = stage(layout, chunks, max_bytes=blob.size, syncer=syncer, faults=None)
    except GraphCliError as exc:
        if exc.code != "file.too_large":
            raise
        raise _differs(blob.hash) from None
    if staged.hash != blob.hash or staged.size != blob.size:
        staged.discard()
        raise _differs(blob.hash)
    target = layout.blob_path(blob.hash)
    try:
        try:
            present = os.lstat(target)
        except FileNotFoundError:
            present = None
        if present is not None and stat.S_ISREG(present.st_mode) and present.st_size == blob.size:
            staged.discard()
            return blob
        ensure_dir(target.parent, syncer)
        os.chmod(staged.path, BLOB_MODE)
        os.replace(staged.path, target)
        syncer.sync_dir(target.parent)
        syncer.sync_dir(layout.blobs)
    except OSError as exc:
        staged.discard()
        raise _unavailable("cannot store the copied file", exc) from exc
    return blob


def _differs(digest: str) -> GraphCliError:
    return GraphCliError(
        "backend.corrupt",
        "the bytes read for a stored file differ from its hash or size",
        details={"blob": digest},
    )


def info(layout: GraphLayout, digest: str) -> BlobInfo | None:
    if not HASH_RE.match(digest):
        return None
    try:
        status = os.stat(layout.blob_path(digest))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unavailable("cannot read a stored file", exc) from exc
    return BlobInfo(digest, status.st_size) if stat.S_ISREG(status.st_mode) else None


def read(layout: GraphLayout, digest: str) -> Iterator[bytes]:
    """The bytes of a stored blob; opened before the first chunk is asked for."""
    if not HASH_RE.match(digest):
        raise GraphCliError("internal.error", f"not a blob hash: {digest!r}")
    try:
        handle = open(layout.blob_path(digest), "rb")  # noqa: SIM115 - closed by _chunks
    except FileNotFoundError:
        raise GraphCliError(
            "backend.corrupt", "a stored file is missing", details={"blob": digest}
        ) from None
    except OSError as exc:
        raise _unavailable("cannot read a stored file", exc) from exc
    return _chunks(handle)


def _chunks(handle: BinaryIO) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk


def local_path(layout: GraphLayout, digest: str) -> Path | None:
    return layout.blob_path(digest) if info(layout, digest) is not None else None


def listing(layout: GraphLayout) -> Iterator[BlobInfo]:
    """Every stored blob in hash order; stray names are not blobs."""
    base = layout.blobs / _ALGORITHM
    try:
        shards = sorted(n for n in os.listdir(base) if _SHARD_RE.match(n))
    except FileNotFoundError:
        return
    for shard in shards:
        for name in sorted(os.listdir(base / shard)):
            if not _HEX_RE.match(name) or name[:2] != shard:
                continue
            status = os.lstat(base / shard / name)
            if stat.S_ISREG(status.st_mode):
                yield BlobInfo(f"{_ALGORITHM}:{name}", status.st_size)


def staging(layout: GraphLayout) -> Iterator[tuple[str, datetime]]:
    """Staging leftovers with their modification time; the caller's clock judges the age."""
    try:
        names = sorted(n for n in os.listdir(layout.blobs) if n.startswith(STAGING_PREFIX))
    except FileNotFoundError:
        return
    for name in names:
        try:
            mtime = os.lstat(layout.blobs / name).st_mtime
        except FileNotFoundError:
            continue  # published or discarded meanwhile
        yield name, datetime.fromtimestamp(mtime, UTC)


def rehash(layout: GraphLayout, digest: str) -> str:
    hasher = hashlib.sha256()
    for chunk in read(layout, digest):
        hasher.update(chunk)
    return f"{_ALGORITHM}:{hasher.hexdigest()}"


def relative(layout: GraphLayout, digest: str) -> str:
    return str(layout.blob_path(digest).relative_to(layout.root))


def staging_findings(layout: GraphLayout) -> list[Finding]:
    """``blob_staging_leftover`` for every staging file (any blob backend stages here)."""
    return [
        Finding(
            _I,
            BLOB_STAGING_LEFTOVER,
            {"path": f"blobs/{name}"},
            "a staged file was never published",
        )
        for name, _ in staging(layout)
    ]


def verify(
    layout: GraphLayout, referenced: Set[str] | None, *, rehash_all: bool
) -> BlobVerification:
    """Blob findings and the stored blobs; ``referenced`` is ``None`` when the tx log could
    not be read whole."""
    findings: list[Finding] = []
    stored: list[BlobInfo] = []

    def add(
        code: str,
        path: str,
        message: str,
        severity: Severity,
        expected: JsonValue = None,
        actual: JsonValue = None,
    ) -> None:
        findings.append(Finding(severity, code, {"path": path}, message, expected, actual))

    try:
        for digest in sorted(referenced or ()):
            if info(layout, digest) is None:
                add(BLOB_MISSING, relative(layout, digest), "a referenced file is missing", _E)
        for blob in listing(layout):
            stored.append(blob)
            path = relative(layout, blob.hash)
            if referenced is not None and blob.hash not in referenced:
                add(BLOB_ORPHAN, path, "no tx references this file", _I)
            if rehash_all:
                actual = rehash(layout, blob.hash)
                if actual != blob.hash:
                    message = "the stored bytes differ from their hash"
                    add(BLOB_HASH_MISMATCH, path, message, _E, blob.hash, actual)
        findings += staging_findings(layout)
    except OSError as exc:
        raise _unavailable("cannot read the stored files", exc) from exc
    return BlobVerification(tuple(findings), tuple(stored))


def missing(layout: GraphLayout, referenced: Set[str]) -> list[str]:
    """Referenced hashes without a stored blob, sorted."""
    return [digest for digest in sorted(referenced) if info(layout, digest) is None]


def remove_copies(layout: GraphLayout, hashes: Sequence[str], *, syncer: Syncer) -> int:
    """Delete the files of ``hashes``, referenced or not: misplaced copies of a graph whose
    stored files are elsewhere, after the caller confirmed that location holds each. How
    many went."""
    for digest in hashes:
        if not HASH_RE.match(digest):
            raise GraphCliError("internal.error", f"refusing to remove blob {digest!r}")
    directories: set[Path] = set()
    removed = 0
    try:
        for digest in hashes:
            path = layout.blob_path(digest)
            if path.exists():
                path.unlink()
                directories.add(path.parent)
                removed += 1
        for directory in sorted(directories):
            syncer.sync_dir(directory)
    except OSError as exc:
        raise _unavailable("cannot remove stored files", exc) from exc
    return removed


def remove(
    layout: GraphLayout,
    hashes: Sequence[str],
    names: Sequence[str],
    *,
    referenced: Set[str],
    syncer: Syncer,
) -> Mapping[str, int]:
    """Delete unreferenced blobs and staging leftovers; returns how many of each went."""
    for digest in hashes:
        if not HASH_RE.match(digest) or digest in referenced:
            raise GraphCliError("internal.error", f"refusing to remove blob {digest!r}")
    for name in names:
        if not name.startswith(STAGING_PREFIX) or "/" in name or name in (".", ".."):
            raise GraphCliError("internal.error", f"not a staging name: {name!r}")
    directories: set[Path] = set()
    counts = {"blobs": 0, "staging": 0}
    try:
        for digest in hashes:
            path = layout.blob_path(digest)
            if path.exists():
                path.unlink()
                directories.add(path.parent)
                counts["blobs"] += 1
        for name in names:
            path = layout.blobs / name
            if path.exists():
                path.unlink()
                directories.add(layout.blobs)
                counts["staging"] += 1
        for directory in sorted(directories):
            syncer.sync_dir(directory)
    except OSError as exc:
        raise _unavailable("cannot remove stored files", exc) from exc
    return counts
