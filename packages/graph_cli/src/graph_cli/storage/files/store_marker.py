"""The object store's identity: the marker ``graph_cli_store.json`` (object storage O5).

An endpoint or a bucket name is not an identity (both change when a bucket moves to
another provider, and a typo points at another bucket). The marker at the root of the
configured prefix names the store's content by a random ``store_id``; a graph in an
object store records that id in its manifest (``[store.blobs] store_id``), and every
blob access checks that the configured bucket carries it before it trusts a listing or
an absent object. A missing marker or another id is ``backend.unavailable`` ("this
graph's files are not in that bucket"), never an empty listing or a storm of missing
files.

``ensure_store_marker`` creates the marker when it is absent (read back after writing,
never overwritten) and is called only from the audited steps that establish a location.
``check_store_identity`` caches what a bucket's marker says per process, keyed by
``(endpoint, bucket, prefix)``, for ``IDENTITY_TTL_S``; readers build a new backend per
call and the server runs requests in threads, so the cache is module-level and locked.
A 404 of a referenced object forces a fresh read (``force=True``).
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.clock import Clock, format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import IdProvider
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.errors import ObjectNotFound
from graph_cli.storage.s3.settings import ObjectStoreSettings

MARKER_KEY: Final = "graph_cli_store.json"
MARKER_SCHEMA: Final = "graph_cli.object_store/1"
IDENTITY_TTL_S: Final = 600.0
_MAX_MARKER_BYTES: Final = 4096

IdentityKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class StoreMarker:
    store_id: str
    created_at: str


def identity_key(settings: ObjectStoreSettings) -> IdentityKey:
    """Where a marker lives: the endpoint (AWS by region without one), bucket and prefix."""
    endpoint = settings.endpoint_url or f"aws:{settings.region}"
    return (endpoint, settings.bucket, settings.prefix)


def _unreadable(client: ObjectStoreClient) -> GraphCliError:
    return GraphCliError(
        "backend.unavailable",
        f"the object store marker {MARKER_KEY} in bucket {client.bucket} is unreadable;"
        " the bucket's content was not written by graph_cli",
        details={"bucket": client.bucket, "marker": MARKER_KEY},
    )


def read_store_marker(client: ObjectStoreClient) -> StoreMarker | None:
    """The bucket's marker, ``None`` when there is none."""
    try:
        body = client.get_object(MARKER_KEY)
    except ObjectNotFound:
        body = None
    if body is None:
        return None
    data = bytearray()
    with body:
        for chunk in body:
            data += chunk
            if len(data) > _MAX_MARKER_BYTES:
                break
    try:
        document = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        document = None
    if (
        not isinstance(document, dict)
        or document.get("schema") != MARKER_SCHEMA
        or not isinstance(document.get("store_id"), str)
        or not isinstance(document.get("created_at"), str)
    ):
        raise _unreadable(client)
    try:
        store_id = str(uuid.UUID(document["store_id"]))
    except ValueError:
        raise _unreadable(client) from None
    return StoreMarker(store_id, document["created_at"])


def ensure_store_marker(
    client: ObjectStoreClient,
    settings: ObjectStoreSettings,
    *,
    clock: Clock,
    ids: IdProvider,
) -> str:
    """The id of the store's marker, created when the bucket has none.

    An existing marker is never overwritten. The new marker is read back after writing,
    and the id read back is the answer: two programs creating a marker at once agree on
    the one the store kept (S3 has no portable create-if-absent; the window is the time
    between the first read and the write, inside an audited configuration step).
    """
    found = read_store_marker(client)
    if found is None:
        document = {
            "schema": MARKER_SCHEMA,
            "store_id": str(ids.new_id()),
            "created_at": format_timestamp(clock.now()),
        }
        body = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        digest = hashlib.sha256(body).hexdigest()
        client.put_object(MARKER_KEY, io.BytesIO(body), len(body), digest)
        found = read_store_marker(client)
        if found is None:
            raise GraphCliError(
                "backend.unavailable",
                f"the object store marker {MARKER_KEY} in bucket {client.bucket} was written"
                " but cannot be read back",
                details={"bucket": client.bucket, "marker": MARKER_KEY},
            )
    _CACHE.forget(identity_key(settings))  # the next check reads the marker
    return found.store_id


class _IdentityCache:
    """What each bucket's marker said, and when it was read (a monotonic clock)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[IdentityKey, tuple[str, float]] = {}

    def lookup(self, key: IdentityKey, now: float) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        store_id, checked = entry
        if now - checked >= IDENTITY_TTL_S:
            return None
        return store_id

    def remember(self, key: IdentityKey, store_id: str | None, now: float) -> None:
        """A marker read at ``now``; a missing marker is not cached (it may be created)."""
        with self._lock:
            if store_id is None:
                self._entries.pop(key, None)
            else:
                self._entries[key] = (store_id, now)

    def forget(self, key: IdentityKey) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHE: Final = _IdentityCache()


def reset_identity_cache() -> None:
    """Forget every marker read (tests)."""
    _CACHE.clear()


def check_store_identity(
    client: ObjectStoreClient,
    settings: ObjectStoreSettings,
    expected: str,
    *,
    monotonic: Callable[[], float],
    force: bool = False,
) -> str | None:
    """``None`` when the configured bucket's marker carries ``expected``, else why not
    (a phrase for ``unusable_object_store``). A cached answer younger than
    ``IDENTITY_TTL_S`` is used unless ``force``; a cached id that differs is read again
    once before it is believed."""
    key = identity_key(settings)
    now = monotonic()
    cached = None if force else _CACHE.lookup(key, now)
    if cached == expected:
        return None
    marker = read_store_marker(client)
    found = marker.store_id if marker is not None else None
    _CACHE.remember(key, found, now)
    if found is None:
        return (
            f"this graph's files are not in that bucket: bucket {settings.bucket} has no"
            f" graph_cli store marker ({MARKER_KEY}); check the bucket, the prefix and the"
            " endpoint"
        )
    if found != expected:
        return (
            f"this graph's files are not in that bucket: bucket {settings.bucket} belongs to"
            f" object store {found}; check the bucket, the prefix and the endpoint"
        )
    return None
