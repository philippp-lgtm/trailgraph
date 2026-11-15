"""Graphs whose stored files are in the fake object store (object storage O10).

Tests of the storage and service layers put a graph into the fake store here, without the
audited switch of ``graph create`` with ``store = "s3"`` (``tests/cli/test_object_store_files.py``
covers that): the store marker is created in the bucket and the graph's manifest is
rewritten as format 2 naming the marker's id. A graph directory without a manifest (the
storage tests' bare stores) gets a minimal one.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.ids import GraphId, TermRef, Uuid7Provider
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.releases import read_manifest
from graph_cli.storage.files.store_marker import ensure_store_marker
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY


def create_marker(settings: ObjectStoreSettings) -> str:
    """The bucket's store id, creating the marker when there is none."""
    clock = SystemClock()
    return ensure_store_marker(
        ObjectStoreClient(settings), settings, clock=clock, ids=Uuid7Provider(clock)
    )


def object_store_manifest(layout: GraphLayout, graph_id: GraphId, store_id: str) -> None:
    """Rewrite (or, in a bare graph directory, write) the manifest as format 2 naming
    ``store_id``."""
    store = StoreInfo("files", 1, "s3", store_id)
    if layout.manifest.exists():
        current = read_manifest(layout)[0]
        data = replace(current, store=store)
    else:
        entry = GraphEntry(
            id=graph_id,
            name="example_graph",
            state="active",
            pin=OntologyPin("inventory", "1.0.0", "sha256:" + "0" * 64),
            alias_prefixes={TermRef("inv:Item"): "ITEM"},
            extensions=(),
        )
        data = ManifestData(entry, datetime(2030, 1, 1, tzinfo=UTC), store)
    tmp = layout.manifest.with_name("manifest.toml.tmp")
    tmp.write_bytes(format_manifest(data))
    tmp.replace(layout.manifest)  # replaced by rename, as graph-admin does


def move_to_object_store(graph_dir: Path, graph_id: GraphId, settings: ObjectStoreSettings) -> str:
    """Put a graph that holds no stored file yet into the store of ``settings``."""
    store_id = create_marker(settings)
    object_store_manifest(GraphLayout(graph_dir), graph_id, store_id)
    return store_id


def settings_spec(settings: ObjectStoreSettings) -> dict[str, Any]:
    """The settings of the fake as JSON for a worker process (the key pair is the fake's)."""
    return {
        "bucket": settings.bucket,
        "region": settings.region,
        "endpoint_url": settings.endpoint_url,
        "prefix": settings.prefix,
    }


def settings_from_spec(spec: Mapping[str, Any]) -> ObjectStoreSettings:
    return ObjectStoreSettings(
        bucket=spec["bucket"],
        region=spec["region"],
        endpoint_url=spec["endpoint_url"],
        prefix=spec.get("prefix", ""),
        credentials=ObjectStoreCredentials(ACCESS_KEY_ID, SECRET_ACCESS_KEY),
    )


def blob_key(graph_id: GraphId, digest: str, *, prefix: str = "") -> str:
    """The object key of a stored file in the fake's bucket."""
    hexdigest = digest.removeprefix("sha256:")
    return f"{prefix}graphs/{graph_id}/blobs/sha256/{hexdigest[:2]}/{hexdigest}"


def blob_keys(settings: ObjectStoreSettings | None) -> list[str]:
    """The keys of every stored file object in the bucket of ``settings`` (none without)."""
    if settings is None:
        return []
    client = ObjectStoreClient(settings)
    return [entry.key for entry in client.list_objects_v2("graphs/") if "/blobs/" in entry.key]
