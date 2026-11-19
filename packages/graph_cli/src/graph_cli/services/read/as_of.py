"""The earlier state ``--as-of`` names and the warning that says which state is read."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import CAPABILITY_SNAPSHOT_AT_SEQ, GraphStoreRead, Snapshot
from graph_cli.kernel.query_ast import AsOf
from graph_cli.services.names import GraphNames
from graph_cli.temporal_knowledge.as_of import as_of_text, seq_at
from graph_cli.temporal_knowledge.name_map import RenameHint, TermKind


@dataclass(frozen=True, slots=True)
class _AsOfState:
    """The earlier state ``--as-of`` names (B8.8): its snapshot, seq, the head's seq and the
    version of the revisions its last tx wrote (``tx.ontology``); every current revision of
    the state fits that version."""

    snapshot: Snapshot
    as_of: AsOf
    seq: int
    head_seq: int
    version: str | None

    @property
    def earlier(self) -> bool:
        return self.seq != self.head_seq

    def warning(
        self,
        entry: GraphEntry,
        names: GraphNames,
        known: Callable[[TermKind, RenameHint], bool] | None = None,
    ) -> tuple[WarningRecord, bool]:
        """The warning that says which state is read, and whether its current revisions are
        shown under mapped names (migration plan M7). When names changed between the state's
        version and the pin, the state's revisions are mapped, and the warning says so when a
        change concerns a term the reader sees (``known``, R4; every change without it)."""
        version = self.version
        mapped = self.earlier and version is not None and names.changes(version)
        shown = mapped and (known is None or names.changes(version or "", known))
        message = f"read as of tx:{self.seq}; the graph is at tx:{self.head_seq}"
        if version is not None and version != entry.pin.version:
            message += f"; that state was written under ontology {version}"
            if shown:
                message += f", its names are mapped to those of {entry.pin.version}"
        details: dict[str, JsonValue] = {
            "as_of": as_of_text(self.as_of),
            "seq": self.seq,
            "head_seq": self.head_seq,
            "ontology_version": version,
        }
        if shown:
            details["names_mapped"] = True
        return WarningRecord("temporal.as_of_read", message, details), mapped


def snapshot_as_of(read: GraphStoreRead, head: Snapshot, as_of: AsOf) -> _AsOfState:
    """The state at the seq ``as_of`` names."""
    if CAPABILITY_SNAPSHOT_AT_SEQ not in read.capabilities():
        raise GraphCliError(
            "temporal.as_of_unsupported",
            "the storage backend of this graph cannot read an earlier state",
            details={"as_of": as_of_text(as_of)},
        )
    seq = seq_at(head, as_of)
    snapshot = head if seq == head.seq else read.snapshot(at_seq=seq)
    tx = snapshot.tx(seq)
    version = tx.ontology.version if tx is not None else None
    return _AsOfState(snapshot, as_of, seq, head.seq, version)
