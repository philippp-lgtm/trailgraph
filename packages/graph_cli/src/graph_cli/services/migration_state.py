"""A migration that is committed but not pinned yet, as the app sees it (plan M3).

A release with a data migration commits one tx and then switches the pin; between the two,
and after a crash there, the stored data has the target version's names while the manifest
still names the old one. The catalog of the app already pins the graph to the target version
then (``FileCatalog``) and hands the marker out as ``GraphEntry.migration``, so reads see
consistent names; this module decides what else follows: writes are refused
(``write.migration_incomplete``), and reads warn when no writer is at work, which means the
release crashed (``graph.migration_incomplete``). The entry carries the marker only while the
pin is not switched: a marker file left over after the switch (a crash before its removal)
is no state for the app, and the next release deletes it.
"""

from __future__ import annotations

from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import GraphStoreRead

REMEDY = "a maintainer finishes it by running the same release again (graph-admin ontology release)"


def refuse_write(entry: GraphEntry) -> None:
    """``write.migration_incomplete`` while a migration is committed but not pinned."""
    marker = entry.migration
    if marker is not None:
        raise GraphCliError(
            "write.migration_incomplete",
            f"graph {entry.name} has a migration to {marker.to_version} that is committed but not"
            f" pinned yet; writes wait until it is finished: {REMEDY}",
            details={
                "graph": entry.name,
                "from": marker.from_version,
                "to": marker.to_version,
            },
        )


def read_warning(entry: GraphEntry, read: GraphStoreRead) -> WarningRecord | None:
    """``graph.migration_incomplete`` for a read of the crash case: the migration is
    committed and no writer holds the lock. While a release runs, its lock is held and the
    state is the normal short window between the tx and the pin."""
    marker = entry.migration
    if marker is None or read.writer_active():
        return None
    return WarningRecord(
        "graph.migration_incomplete",
        f"graph {entry.name} has a migration to {marker.to_version} that is committed but not"
        f" pinned (an interrupted release); reads use {marker.to_version}, writes are refused:"
        f" {REMEDY}",
        {"graph": entry.name, "from": marker.from_version, "to": marker.to_version},
    )
