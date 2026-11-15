"""What a ``FileStore`` opens: the manifest's ``[store]`` and blob location (section 5).

One check for the catalog (``FileCatalog.file_store``, app and server) and the
maintenance port (``FileStoreMaintenance.store``, graph-admin), so every program
refuses the same graphs: another backend or layout version is
``format.unsupported_version``. A graph whose stored files are in an object store
(manifest format 2, ``blobs.s3``) needs usable object store settings; without them it
is ``backend.unavailable`` naming the graph and what is missing.

This build has no object store backend yet, so such a graph is refused even with
settings (``backend.unavailable``) instead of being read as if its files were local.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import ManifestData
from graph_cli.storage.s3.settings import ObjectStoreSettings

FILES_BACKEND: Final = "files"
FILES_FORMAT: Final = 1  # [store] format_version of the FileStore layout


def check_store(
    manifest: ManifestData,
    *,
    object_store: ObjectStoreSettings | None,
    object_store_problem: GraphCliError | None = None,
) -> None:
    """Refuse a graph this store cannot open. ``object_store_problem`` says why
    ``object_store`` is ``None`` (the configuration error), when it is known."""
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
    if info.blob_location == "local":
        return
    details: dict[str, JsonValue] = {
        "graph_id": str(graph),
        "blob_location": info.blob_location,
        "store_id": info.store_id,
    }
    if object_store is None:
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
        raise GraphCliError(
            "backend.unavailable",
            f"graph {graph} keeps its stored files in an object store, but none is usable: {cause}",
            details=details,
        )
    raise GraphCliError(
        "backend.unavailable",
        f"graph {graph} keeps its stored files in an object store, which this program cannot"
        " open yet; use a newer version",
        details=details,
    )
