"""Blob backends: where a graph's stored file bytes live (contracts section 5, 4.1 KB0-KB2).

``FileStore`` keeps the tx log, revisions, index, audit and manifest itself and hands
the bytes of stored files to a ``BlobBackend``. ``LocalBlobs`` keeps them in the graph
directory's ``blobs/`` (``storage.files.blobs``, unchanged layout, modes and crash
points); ``S3Blobs`` (``storage.files.s3_blobs``) in an S3-compatible object store.
The protocol is internal to ``graph_cli.storage``; the kernel ports keep their shape
and only ``GraphStoreWrite.promote_blobs`` reaches it from the services.

Which backend serves a graph follows from its manifest (``[store]`` ``blob_location``),
never from the installation's ``[files] store``, and ``BlobBackends.backend`` is the
one place that turns a manifest into a backend:

- a ``FileSession`` binds the backend on its first blob use, under the graph lock
  (``publish_blob`` and the commit check), and keeps it for the session, so a tx never
  references a blob outside the location the manifest names while the lock is held;
- readers stay lock-free and resolve the backend per call (the parsed manifest is
  cached per process, see ``blob_manifest``);
- a graph directory whose manifest is missing or unreadable is served by
  ``LocalBlobs``, as before blob locations existed: storage tests build stores without
  one, and maintenance reports an unreadable manifest itself (``verify``). The catalog
  never opens such a graph, and the write service re-reads the manifest under the lock.

Staging is always local: a write stages the bytes into ``blobs/.tmp-<random>`` before
the lock whatever the backend, keeps the staging file until the request ends, and the
backend bound under the lock publishes from it. A blob staged while the graph named
another location is therefore published to the bound one from the same file.

The write path per blob: ``stage`` (before the lock, KB0), ``promote`` (before the
lock, after the write is authorized; a no-op for ``LocalBlobs``), ``publish`` (under
the lock, KB1/KB2), then the commit check asks ``missing`` for the tx's blobs.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ports.catalog import BlobLocation, ManifestData
from graph_cli.kernel.ports.storage import (
    CAPABILITY_BLOB_LOCAL_PATH,
    BlobVerification,
    StagedBlob,
)
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage.common.faults import FaultHook
from graph_cli.storage.files import blobs
from graph_cli.storage.files.durable import Syncer
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import read_manifest
from graph_cli.storage.files.s3_blobs import ClientFactory, S3Blobs, missing_credentials
from graph_cli.storage.s3.settings import ObjectStoreSettings


class BlobBackend(Protocol):
    """The stored file bytes of one graph, by hash (``sha256:<hex>``)."""

    @property
    def location(self) -> BlobLocation:
        """The manifest's ``blob_location`` this backend serves."""
        ...

    @property
    def local_paths(self) -> bool:
        """Whether ``local_path`` hands out files (capability ``blob_local_path``)."""
        ...

    def stage(self, chunks: Iterable[bytes], *, max_bytes: int) -> blobs.FileStagedBlob:
        """Before the lock: a synced local staging file, hashed and limited while written
        (KB0); ``file.too_large`` beyond ``max_bytes``."""
        ...

    def promote(self, staged: Sequence[StagedBlob]) -> None:
        """Before the lock, once the write is authorized: make staged blobs ready, so that
        ``publish`` rarely moves bytes under the lock. The staging files stay."""
        ...

    def publish(self, staged: StagedBlob) -> BlobInfo:
        """Under the lock: the staged blob stored under its hash, durable on return
        (KB1/KB2). A blob already stored is kept; the staged copy is discarded."""
        ...

    def info(self, digest: str) -> BlobInfo | None:
        """The stored blob, ``None`` when absent or not a hash."""
        ...

    def read(self, digest: str) -> Iterator[bytes]:
        """The stored bytes in chunks; ``backend.corrupt`` when the blob is absent."""
        ...

    def local_path(self, digest: str) -> Path | None:
        """The read-only file of a stored blob; ``None`` when absent or without
        ``local_paths``."""
        ...

    def missing(self, referenced: Set[str]) -> list[str]:
        """The hashes of ``referenced`` that are not stored, sorted (the commit check)."""
        ...

    def absent(self, referenced: Set[str]) -> list[str]:
        """``missing`` for maintenance (``audit verify``): an object store answers it from
        one listing of the graph's prefix, confirming what the listing lacks by ``HEAD``."""
        ...

    def listing(self) -> Iterator[BlobInfo]:
        """Every stored blob of the graph, in hash order."""
        ...

    def staging(self) -> Iterator[tuple[str, datetime]]:
        """Staging leftovers with their modification time."""
        ...

    def verify(self, referenced: Set[str] | None, *, rehash: bool) -> BlobVerification:
        """``integrity.storage.blob_*`` findings and the stored blobs, from one listing;
        ``referenced`` is ``None`` when the tx log cannot be read whole (no orphans then);
        ``rehash`` reads every stored blob."""
        ...

    def unavailable(self, error: GraphCliError) -> Finding | None:
        """The ``blob_store_unavailable`` finding that stands for ``error`` when it says
        that this backend's store cannot be reached or is another store; ``None`` for any
        other error (it is raised: a local disk that fails stops the command)."""
        ...

    def collectable(self, referenced: Set[str]) -> list[BlobInfo]:
        """The stored blobs ``files gc`` may remove now: unreferenced and, in an object
        store, older than the grace period by the store's clock; in hash order."""
        ...

    def remove(
        self, hashes: Sequence[str], names: Sequence[str], *, referenced: Set[str]
    ) -> Mapping[str, int]:
        """Delete unreferenced blobs (an object store keeps those younger than the grace
        period) and staging leftovers; how many of each went."""
        ...

    def store_id(self) -> str | None:
        """The id of the object store whose bucket this backend uses (its marker's);
        ``None`` for the data root."""
        ...

    def hash_of(self, digest: str) -> str | None:
        """The hash of the stored bytes of ``digest``, read in full; ``None`` when they are
        absent or cannot be read (``files gc`` confirms a location this way before it
        removes a misplaced copy elsewhere)."""
        ...

    def remove_misplaced(self, hashes: Sequence[str]) -> int:
        """Delete this location's copies of ``hashes``, referenced or not: misplaced copies
        of a graph that keeps its stored files elsewhere. Only after the caller confirmed
        that the graph's location holds the same bytes (``hash_of``). How many went."""
        ...

    def target(self, digest: str) -> str:
        """Where a blob is, as finding targets name it."""
        ...


class LocalBlobs:
    """``blobs/sha256/<xx>/<hex>`` of the graph directory (``storage.files.blobs``)."""

    location: BlobLocation = "local"
    local_paths = True

    def __init__(self, layout: GraphLayout, *, syncer: Syncer, faults: FaultHook | None) -> None:
        self.layout = layout
        self.syncer = syncer
        self.faults = faults

    def stage(self, chunks: Iterable[bytes], *, max_bytes: int) -> blobs.FileStagedBlob:
        return blobs.stage(
            self.layout, chunks, max_bytes=max_bytes, syncer=self.syncer, faults=self.faults
        )

    def promote(self, staged: Sequence[StagedBlob]) -> None:
        """Nothing to do: the staging file is renamed into place by ``publish``."""

    def publish(self, staged: StagedBlob) -> BlobInfo:
        return blobs.publish(self.layout, staged, syncer=self.syncer, faults=self.faults)

    def info(self, digest: str) -> BlobInfo | None:
        return blobs.info(self.layout, digest)

    def read(self, digest: str) -> Iterator[bytes]:
        return blobs.read(self.layout, digest)

    def local_path(self, digest: str) -> Path | None:
        return blobs.local_path(self.layout, digest)

    def missing(self, referenced: Set[str]) -> list[str]:
        return blobs.missing(self.layout, referenced)

    def absent(self, referenced: Set[str]) -> list[str]:
        return blobs.missing(self.layout, referenced)

    def listing(self) -> Iterator[BlobInfo]:
        return blobs.listing(self.layout)

    def staging(self) -> Iterator[tuple[str, datetime]]:
        return blobs.staging(self.layout)

    def verify(self, referenced: Set[str] | None, *, rehash: bool) -> BlobVerification:
        return blobs.verify(self.layout, referenced, rehash_all=rehash)

    def unavailable(self, error: GraphCliError) -> Finding | None:
        return None

    def collectable(self, referenced: Set[str]) -> list[BlobInfo]:
        return [blob for blob in blobs.listing(self.layout) if blob.hash not in referenced]

    def remove(
        self, hashes: Sequence[str], names: Sequence[str], *, referenced: Set[str]
    ) -> Mapping[str, int]:
        return blobs.remove(self.layout, hashes, names, referenced=referenced, syncer=self.syncer)

    def store_id(self) -> str | None:
        return None

    def hash_of(self, digest: str) -> str | None:
        try:
            return blobs.rehash(self.layout, digest)
        except (OSError, GraphCliError):
            return None

    def remove_misplaced(self, hashes: Sequence[str]) -> int:
        return blobs.remove_copies(self.layout, hashes, syncer=self.syncer)

    def target(self, digest: str) -> str:
        return blobs.relative(self.layout, digest)


_MANIFEST_CACHE_SIZE: Final = 256
_ManifestKey = tuple[int, int, int, int]
_manifest_lock = threading.Lock()
_manifests: OrderedDict[str, tuple[_ManifestKey, ManifestData]] = OrderedDict()


def _manifest_key(path: Path) -> _ManifestKey | None:
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)


def blob_manifest(layout: GraphLayout) -> ManifestData | None:
    """The manifest that decides the graph's blob location; ``None`` when it is missing
    or unreadable (``config.invalid``), which means local. An unknown format or feature
    is raised: no backend is guessed for it.

    Parsed manifests are cached per process, keyed by the file's device, inode,
    modification time and size: graph-admin replaces a manifest by a rename (a new
    inode), so a replaced manifest is never served from the cache. A manifest that
    changed while it was read is not cached."""
    path = layout.manifest
    name = str(path)
    before = _manifest_key(path)
    if before is not None:
        with _manifest_lock:
            hit = _manifests.get(name)
            if hit is not None and hit[0] == before:
                _manifests.move_to_end(name)
                return hit[1]
    try:
        manifest = read_manifest(layout)[0]
    except GraphCliError as exc:
        if exc.code != "config.invalid":
            raise
        return None
    if before is not None and _manifest_key(path) == before:
        with _manifest_lock:
            _manifests[name] = (before, manifest)
            _manifests.move_to_end(name)
            while len(_manifests) > _MANIFEST_CACHE_SIZE:
                _manifests.popitem(last=False)
    return manifest


@dataclass(frozen=True, slots=True)
class BlobBackends:
    """Builds the backend a manifest names, from the installation's object store
    settings (built by ``catalog``; storage never reads the environment)."""

    object_store: ObjectStoreSettings | None = None
    # why ``object_store`` is ``None``, when it is known (the configuration error)
    object_store_problem: GraphCliError | None = None
    # tests: the object store client (default ``ObjectStoreClient``) and the monotonic
    # clock of the store marker cache
    client_factory: ClientFactory | None = None
    monotonic: Callable[[], float] = time.monotonic

    def backend(
        self,
        layout: GraphLayout,
        manifest: ManifestData | None,
        *,
        syncer: Syncer,
        faults: FaultHook | None,
    ) -> BlobBackend:
        if manifest is None or manifest.store.blob_location == "local":
            return LocalBlobs(layout, syncer=syncer, faults=faults)
        # No I/O and no settings check here: S3Blobs refuses unusable settings and a
        # foreign bucket on blob access, so a read that touches no file never needs them.
        return S3Blobs(
            layout,
            manifest,
            settings=self.object_store,
            settings_problem=self.object_store_problem,
            syncer=syncer,
            faults=faults,
            client_factory=self.client_factory,
            monotonic=self.monotonic,
        )

    def elsewhere(
        self,
        layout: GraphLayout,
        manifest: ManifestData | None,
        *,
        syncer: Syncer,
        faults: FaultHook | None,
    ) -> BlobBackend | None:
        """Where misplaced copies of the graph's blobs may lie (object storage O7, O8): the
        graph directory's ``blobs/`` for a graph in an object store; for a local graph the
        configured object store, when one is configured with credentials (any graph_cli
        store marker will do for finding copies; whether they are the graph's own, which
        ``files gc`` may delete, the caller decides from the marker's id and the graph's
        audit trail; whether the store can be reached shows on first use). ``None``
        when there is no such place, or the manifest is unreadable (nothing is judged)."""
        if manifest is None:
            return None
        if manifest.store.blob_location != "local":
            return LocalBlobs(layout, syncer=syncer, faults=faults)
        if self.object_store is None or missing_credentials(self.object_store):
            return None
        return S3Blobs(
            layout,
            manifest,
            settings=self.object_store,
            settings_problem=None,
            syncer=syncer,
            faults=faults,
            client_factory=self.client_factory,
            monotonic=self.monotonic,
            require_store_id=False,
        )

    def capabilities(self, manifest: ManifestData | None) -> frozenset[str]:
        """The blob capabilities of the backend ``manifest`` names, without building it
        (so a read that touches no file never needs the object store)."""
        if manifest is None or manifest.store.blob_location == "local":
            return frozenset({CAPABILITY_BLOB_LOCAL_PATH})
        return frozenset()
