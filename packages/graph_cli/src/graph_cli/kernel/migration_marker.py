"""The migration marker: the durable note of a data migration that is not finished (plan M3).

A release with a migration commits one tx and then switches the pin. Between the two, and
after a crash there, the stored data carries the target names while the pin still names the
old version. The marker is written before the tx is committed and removed after the pin
switch; a reader that finds one decides from ``marker_committed`` whether the tx is in the log.

Storage keeps the marker (``MigrationMarker`` is its payload); services decide what it means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TxId, parse_id
from graph_cli.kernel.json_access import JsonShapeError, as_object, req_int, req_str

if TYPE_CHECKING:
    from graph_cli.kernel.ports.storage import Snapshot

MARKER_SCHEMA: Final = "graph_cli.migration_marker/1"


@dataclass(frozen=True, slots=True)
class MigrationMarker:
    """The migration tx that is (or is about to be) committed, and what it migrates between.

    ``seq`` and ``tx_id`` name the tx; ``artifact_hash`` is the target version's and
    ``migration_hash`` the migration file's, so a re-run can tell its own release from another."""

    seq: int
    tx_id: TxId
    from_version: str
    to_version: str
    artifact_hash: str
    migration_hash: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema": MARKER_SCHEMA,
            "seq": self.seq,
            "tx_id": str(self.tx_id),
            "from": self.from_version,
            "to": self.to_version,
            "artifact_hash": self.artifact_hash,
            "migration_hash": self.migration_hash,
        }

    @classmethod
    def from_json(cls, data: JsonValue) -> MigrationMarker:
        """Parse a marker document; ``ValueError`` for anything that is not one."""
        try:
            doc = as_object(data, "marker")
            if req_str(doc, "schema") != MARKER_SCHEMA:
                raise ValueError(f"the marker schema is not {MARKER_SCHEMA}")
            seq = req_int(doc, "seq")
            if seq < 1:
                raise ValueError("the marker seq is not positive")
            return cls(
                seq=seq,
                tx_id=TxId(parse_id(req_str(doc, "tx_id"))),
                from_version=req_str(doc, "from"),
                to_version=req_str(doc, "to"),
                artifact_hash=req_str(doc, "artifact_hash"),
                migration_hash=req_str(doc, "migration_hash"),
            )
        except (JsonShapeError, CanonicalError, GraphCliError) as exc:
            raise ValueError(str(exc)) from None

    @classmethod
    def from_bytes(cls, data: bytes) -> MigrationMarker:
        try:
            return cls.from_json(parse_json(data))
        except CanonicalError as exc:
            raise ValueError(str(exc)) from None


def marker_committed(snapshot: Snapshot, marker: MigrationMarker) -> bool:
    """Whether the marker's tx is in the log: the tx at ``marker.seq`` exists and is that tx.

    Decided by identity, not by ``head.seq >= marker.seq``: after a crash before the commit an
    application write can take the same seq, and a stale marker must not look committed."""
    tx = snapshot.tx(marker.seq)
    return tx is not None and tx.tx_id == marker.tx_id
