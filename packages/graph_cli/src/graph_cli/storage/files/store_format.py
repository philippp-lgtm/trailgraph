"""What a ``FileStore`` opens: the manifest's ``[store]`` (section 5).

One check for the catalog (``FileCatalog.file_store``, app and server) and the
maintenance port (``FileStoreMaintenance.store``, graph-admin), so every program
refuses the same graphs: another backend or layout version is
``format.unsupported_version``.

A graph whose stored files are in an object store (manifest format 2, ``blobs.s3``)
opens like any other: reads and writes that touch no file never need the object store.
Its blob backend (``storage.files.s3_blobs``) refuses every blob access without usable
object store settings with ``unusable_object_store``: ``backend.unavailable`` naming the
graph and what is missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import ManifestData

FILES_BACKEND: Final = "files"
FILES_FORMAT: Final = 1  # [store] format_version of the FileStore layout


def check_store(manifest: ManifestData) -> None:
    """Refuse a graph this store cannot open. The object store settings are checked on
    blob access only (``unusable_object_store``)."""
    graph = manifest.entry.id
    info = manifest.store
    if (info.backend, info.format_version) != (FILES_BACKEND, FILES_FORMAT):
        raise GraphCliError(
            "format.unsupported_version",
            f"graph {graph} uses store {info.backend}/{info.format_version}",
            details={
                "expected": f"{FILES_BACKEND}/{FILES_FORMAT}",
                "actual": f"{info.backend}/{info.format_version}",
            },
        )


def unusable_object_store(
    manifest: ManifestData,
    *,
    cause: str | None = None,
    object_store_problem: GraphCliError | None = None,
    extra: Mapping[str, JsonValue] | None = None,
) -> GraphCliError:
    """``backend.unavailable`` for a blob access of a graph in an object store that cannot
    be served: no settings (``object_store_problem`` says why, when it is known), or
    ``cause`` (incomplete credentials, a store marker that does not match)."""
    graph = manifest.entry.id
    info = manifest.store
    details: dict[str, JsonValue] = {
        "graph_id": str(graph),
        "blob_location": info.blob_location,
        "store_id": info.store_id,
    }
    if cause is None:
        cause = (
            object_store_problem.message
            if object_store_problem is not None
            else "no object store is configured ([files.s3])"
        )
        if object_store_problem is not None:
            details["cause"] = object_store_problem.code
            key = object_store_problem.details.get("key") or object_store_problem.details.get(
                "variable"
            )
            if key is not None:
                details["key"] = key
    details.update(extra or {})
    return GraphCliError(
        "backend.unavailable",
        f"graph {graph} keeps its stored files in an object store, but none is usable: {cause}",
        details=details,
    )
