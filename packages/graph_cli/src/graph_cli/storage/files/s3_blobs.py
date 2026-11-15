"""Stored file bytes in an S3-compatible object store (object storage O1, O5, O6, O8, O9).

``S3Blobs`` serves a graph whose manifest names ``blobs.s3``. The bytes of one hash are
the object ``graphs/<graph id>/blobs/sha256/<xx>/<64 hex>`` below the configured prefix
(the client applies the prefix); the store sees bytes, sizes and hashes only.

- Building the backend does no I/O and checks no settings, so capabilities, file-free
  reads and file-free writes never need the object store. Every remote call first
  refuses unusable settings (none, a configuration error, incomplete credentials) and
  checks the bucket's store marker against the manifest's ``store_id``
  (``store_marker``); both are ``backend.unavailable`` naming the graph.
- Staging stays local (``blobs/.tmp-*`` of the graph directory, ``storage.files.blobs``).
- ``promote`` (before the lock, once the write is authorized) uploads each staged blob
  the store lacks, or holds with another size, or holds with a ``LastModified`` older
  than ``PROMOTE_REFRESH`` by the store's clock: half the gc grace period, so a gc cannot
  delete the object between the promote and the commit. Uploads are signed with the
  real hash, so the store verifies the bytes against the content address.
- ``publish`` (under the lock) confirms the object by ``HEAD`` (present, same size) and
  uploads it from the staging file only when it is missing (a gc raced the promote, or
  the graph moved meanwhile); then it removes the staging file, as the local rename does.
  A gc also takes the graph lock, so nothing removes the object before the commit.
- ``missing`` (the commit check) answers from the hashes this instance published or
  confirmed, else by ``HEAD``; above ``LISTING_THRESHOLD`` unconfirmed hashes one paged
  listing replaces the HEADs, and every hash the listing lacks is confirmed by ``HEAD``
  before it is reported (listings may lag).
- ``read`` streams the object verified against its hash; the last chunk is held back
  until the hash matches. A referenced object that is absent or whose bytes differ is
  ``backend.corrupt``, like a missing or damaged local blob, once the store marker was
  checked again.
- ``collectable`` and ``remove`` judge an orphan's age by the store's clock (the listing
  answer's ``Date``): only objects older than ``GC_GRACE`` go.

Crash windows (contracts section 4.1, amended): KB0 is the staging file as for local
blobs. ``blob.after_upload`` follows every upload: after the promote (KB0s) or the
publish fallback (KB1s) the object is present and no intent names it, an orphan until a
retry commits or gc removes it. ``blob.before_staging_removal`` follows the publish
confirmation: the object and the staging file are both present, no intent.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence, Set
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from graph_cli.kernel.canonical import HASH_RE, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ports.catalog import BlobLocation, ManifestData
from graph_cli.kernel.ports.storage import StagedBlob
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.files import blobs
from graph_cli.storage.files.durable import Syncer
from graph_cli.storage.files.layout import STAGING_PREFIX, GraphLayout
from graph_cli.storage.files.store_format import unusable_object_store
from graph_cli.storage.files.store_marker import check_store_identity
from graph_cli.storage.finding_codes import (
    BLOB_HASH_MISMATCH,
    BLOB_MISSING,
    BLOB_ORPHAN,
    BLOB_STAGING_LEFTOVER,
)
from graph_cli.storage.s3.client import ObjectInfo, ObjectSourceError, ObjectStoreClient
from graph_cli.storage.s3.errors import ObjectContentMismatch, ObjectNotFound
from graph_cli.storage.s3.settings import ObjectStoreSettings

PROMOTE_REFRESH: Final = timedelta(hours=12)
GC_GRACE: Final = timedelta(hours=24)
LISTING_THRESHOLD: Final = 64
_SHA256_PREFIX: Final = "sha256:"
_ENTRY_RE: Final = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{64})$")
_E, _I = Severity.ERROR, Severity.INFO

ClientFactory = Callable[[ObjectStoreSettings], ObjectStoreClient]


def _age(info: ObjectInfo) -> timedelta | None:
    """How old the object is by the store's own clock; ``None`` without its ``Date``."""
    if info.response_date is None:
        return None
    return info.response_date - info.last_modified


def _corrupt(message: str, digest: str) -> GraphCliError:
    return GraphCliError("backend.corrupt", message, details={"blob": digest})


class S3Blobs:
    """``graphs/<graph id>/blobs/sha256/<xx>/<hex>`` of one object store bucket."""

    location: BlobLocation = "s3"
    local_paths = False

    def __init__(
        self,
        layout: GraphLayout,
        manifest: ManifestData,
        *,
        settings: ObjectStoreSettings | None,
        settings_problem: GraphCliError | None,
        syncer: Syncer,
        faults: FaultHook | None,
        client_factory: ClientFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.layout = layout
        self.manifest = manifest
        self.settings = settings
        self.settings_problem = settings_problem
        self.syncer = syncer
        self.faults = faults
        self._client_factory: ClientFactory = client_factory or ObjectStoreClient
        self._monotonic = monotonic
        self._client: ObjectStoreClient | None = None
        self._base = f"graphs/{manifest.entry.id}/blobs/sha256/"
        # hashes this instance published or found stored (a session's commit check)
        self._confirmed: set[str] = set()

    # ---- the store ------------------------------------------------------

    def _store(self) -> ObjectStoreClient:
        """The client, after the settings and the store marker were checked."""
        if self.settings is None:
            raise unusable_object_store(self.manifest, object_store_problem=self.settings_problem)
        credentials = self.settings.credentials
        missing = []
        if credentials is None or not credentials.access_key_id:
            missing.append("AWS_ACCESS_KEY_ID")
        if credentials is None or not credentials.secret_access_key:
            missing.append("AWS_SECRET_ACCESS_KEY")
        if missing:
            raise unusable_object_store(
                self.manifest,
                cause=f"the object store credentials are not set ({', '.join(missing)})",
                extra={"missing": list(missing)},
            )
        if self._client is None:
            self._client = self._client_factory(self.settings)
        self._check_identity(force=False)
        return self._client

    def _check_identity(self, *, force: bool) -> None:
        assert self._client is not None and self.settings is not None
        expected = self.manifest.store.store_id or ""
        problem = check_store_identity(
            self._client, self.settings, expected, monotonic=self._monotonic, force=force
        )
        if problem is not None:
            raise unusable_object_store(
                self.manifest, cause=problem, extra={"bucket": self.settings.bucket}
            )

    def _absent(self) -> None:
        """A referenced object is absent: is this still the graph's bucket? (O5)"""
        self._check_identity(force=True)

    def key(self, digest: str) -> str:
        """The object key of a hash, relative to the configured prefix."""
        hexdigest = digest.removeprefix(_SHA256_PREFIX)
        return f"{self._base}{hexdigest[:2]}/{hexdigest}"

    def _objects(self, store: ObjectStoreClient) -> Iterator[tuple[str, ObjectInfo]]:
        """Every blob object of the graph by one paged listing, in key (= hash) order;
        other keys below the graph's prefix are not blobs."""
        for entry in store.list_objects_v2(self._base):
            match = _ENTRY_RE.match(entry.key.removeprefix(self._base))
            if match is None or not match.group(2).startswith(match.group(1)):
                continue
            yield _SHA256_PREFIX + match.group(2), entry

    def _staged_file(self, staged: StagedBlob) -> blobs.FileStagedBlob:
        if not isinstance(staged, blobs.FileStagedBlob) or staged.path.parent != self.layout.blobs:
            raise GraphCliError("internal.error", "the staged blob is not from this store")
        return staged

    def _upload(self, store: ObjectStoreClient, staged: blobs.FileStagedBlob) -> None:
        short = False
        try:
            with open(staged.path, "rb") as handle:
                try:
                    store.put_object(
                        self.key(staged.hash),
                        handle,
                        staged.size,
                        staged.hash.removeprefix(_SHA256_PREFIX),
                    )
                except ObjectSourceError:
                    short = True
        except FileNotFoundError:
            raise GraphCliError("internal.error", "the staged blob was discarded") from None
        except OSError as exc:
            raise GraphCliError(
                "backend.unavailable", f"cannot read the staged file: {exc.strerror or exc}"
            ) from None
        if short:
            raise GraphCliError(
                "backend.unavailable", "the staged file is shorter than when it was staged"
            )
        fire(self.faults, CrashPoint.BLOB_AFTER_UPLOAD)

    # ---- the write path -------------------------------------------------

    def stage(self, chunks: Iterable[bytes], *, max_bytes: int) -> blobs.FileStagedBlob:
        return blobs.stage(
            self.layout, chunks, max_bytes=max_bytes, syncer=self.syncer, faults=self.faults
        )

    def promote(self, staged: Sequence[StagedBlob]) -> None:
        files = [self._staged_file(blob) for blob in staged]
        if not files:
            return
        store = self._store()
        done: set[str] = set()
        for blob in files:
            if blob.hash in done:
                continue
            done.add(blob.hash)
            stored = store.head_object(self.key(blob.hash))
            age = _age(stored) if stored is not None else None
            if stored is None or stored.size != blob.size or age is None or age > PROMOTE_REFRESH:
                self._upload(store, blob)

    def publish(self, staged: StagedBlob) -> BlobInfo:
        blob = self._staged_file(staged)
        store = self._store()
        stored = store.head_object(self.key(blob.hash))
        if stored is None or stored.size != blob.size:
            if not blob.path.exists():
                raise GraphCliError("internal.error", "the staged blob was discarded")
            self._upload(store, blob)  # the fallback under the lock (KB1s)
        self._confirmed.add(blob.hash)
        fire(self.faults, CrashPoint.BLOB_BEFORE_STAGING_REMOVAL)
        blob.discard()
        return BlobInfo(blob.hash, blob.size)

    def missing(self, referenced: Set[str]) -> list[str]:
        wanted = sorted(referenced)
        invalid = [digest for digest in wanted if not HASH_RE.match(digest)]
        unconfirmed = [d for d in wanted if HASH_RE.match(d) and d not in self._confirmed]
        if not unconfirmed:
            return invalid
        store = self._store()
        candidates = unconfirmed
        if len(unconfirmed) > LISTING_THRESHOLD:
            listed = {digest for digest, _ in self._objects(store)}
            self._confirmed.update(d for d in unconfirmed if d in listed)
            candidates = [d for d in unconfirmed if d not in listed]
        absent = []
        for digest in candidates:
            if store.head_object(self.key(digest)) is None:
                absent.append(digest)
            else:
                self._confirmed.add(digest)
        if absent:
            self._absent()
        return sorted([*invalid, *absent])

    # ---- reads ------------------------------------------------------------

    def info(self, digest: str) -> BlobInfo | None:
        if not HASH_RE.match(digest):
            return None
        stored = self._store().head_object(self.key(digest))
        if stored is None:
            self._absent()
            return None
        return BlobInfo(digest, stored.size)

    def read(self, digest: str) -> Iterator[bytes]:
        if not HASH_RE.match(digest):
            raise GraphCliError("internal.error", f"not a blob hash: {digest!r}")
        store = self._store()
        hexdigest = digest.removeprefix(_SHA256_PREFIX)
        try:
            body = store.get_object(self.key(digest), expected_sha256=hexdigest)
        except ObjectNotFound:
            body = None
        if body is None:
            self._absent()
            raise _corrupt("a stored file is missing", digest)
        return self._verified(body, digest)

    @staticmethod
    def _verified(body: Iterable[bytes], digest: str) -> Iterator[bytes]:
        """The object's chunks; a hash mismatch is raised before its last chunk."""
        mismatch = False
        try:
            yield from body
        except ObjectContentMismatch:
            mismatch = True
        if mismatch:
            raise _corrupt("the stored bytes differ from their hash", digest)

    def local_path(self, digest: str) -> Path | None:
        """No local files (no ``blob_local_path`` capability)."""
        return None

    def listing(self) -> Iterator[BlobInfo]:
        store = self._store()
        return (BlobInfo(digest, entry.size) for digest, entry in self._objects(store))

    def staging(self) -> Iterator[tuple[str, datetime]]:
        return blobs.staging(self.layout)

    # ---- maintenance --------------------------------------------------------

    def _rehash(self, store: ObjectStoreClient, digest: str) -> str | None:
        """The hash of the object's bytes; ``None`` when it went away meanwhile."""
        try:
            body = store.get_object(
                self.key(digest), expected_sha256=digest.removeprefix(_SHA256_PREFIX)
            )
        except ObjectNotFound:
            return None
        actual = digest
        with body:
            try:
                for _ in body:
                    pass
            except ObjectContentMismatch as exc:
                actual = _SHA256_PREFIX + exc.actual
        return actual

    def verify(self, referenced: Set[str] | None, *, rehash: bool) -> list[Finding]:
        findings: list[Finding] = []

        def add(
            code: str,
            path: str,
            message: str,
            severity: Severity,
            expected: JsonValue = None,
            actual: JsonValue = None,
        ) -> None:
            findings.append(Finding(severity, code, {"path": path}, message, expected, actual))

        store = self._store()
        listed = dict(self._objects(store))
        absent = [
            digest
            for digest in sorted(referenced or ())
            if digest not in listed
            and (not HASH_RE.match(digest) or store.head_object(self.key(digest)) is None)
        ]
        if absent:
            self._absent()
        for digest in absent:
            add(BLOB_MISSING, self.target(digest), "a referenced file is missing", _E)
        for digest in sorted(listed):
            path = self.target(digest)
            if referenced is not None and digest not in referenced:
                add(BLOB_ORPHAN, path, "no tx references this file", _I)
            if rehash:
                actual = self._rehash(store, digest)
                if actual is not None and actual != digest:
                    message = "the stored bytes differ from their hash"
                    add(BLOB_HASH_MISMATCH, path, message, _E, digest, actual)
        for name, _ in blobs.staging(self.layout):
            add(BLOB_STAGING_LEFTOVER, f"blobs/{name}", "a staged file was never published", _I)
        return findings

    def collectable(self, referenced: Set[str]) -> list[BlobInfo]:
        return [
            BlobInfo(digest, entry.size)
            for digest, entry in self._objects(self._store())
            if digest not in referenced and self._expired(entry)
        ]

    @staticmethod
    def _expired(entry: ObjectInfo) -> bool:
        age = _age(entry)
        return age is not None and age > GC_GRACE

    def remove(
        self, hashes: Sequence[str], names: Sequence[str], *, referenced: Set[str]
    ) -> Mapping[str, int]:
        for digest in hashes:
            if not HASH_RE.match(digest) or digest in referenced:
                raise GraphCliError("internal.error", f"refusing to remove blob {digest!r}")
        for name in names:
            if not name.startswith(STAGING_PREFIX) or "/" in name or name in (".", ".."):
                raise GraphCliError("internal.error", f"not a staging name: {name!r}")
        removed = 0
        if hashes:
            store = self._store()
            wanted = set(hashes)
            for digest, entry in self._objects(store):
                if digest in wanted and self._expired(entry):
                    store.delete_object(self.key(digest))
                    removed += 1
        local = blobs.remove(self.layout, (), names, referenced=referenced, syncer=self.syncer)
        return {"blobs": removed, "staging": local["staging"]}

    def target(self, digest: str) -> str:
        prefix = self.settings.prefix if self.settings is not None else ""
        return prefix + self.key(digest)
