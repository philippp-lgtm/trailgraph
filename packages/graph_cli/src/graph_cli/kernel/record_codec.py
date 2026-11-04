"""The record codec of one graph: plain (store format 1) or sealed (store format 2, plan T16).

Every place where a tx or a revision turns into stored bytes or back goes through
here, in both backends. A sealed graph keeps the structure of a record in the
clear and its values in one envelope under the key of its entity (a revision)
or its tx (change set, resolution, reason). The associated data binds an
envelope to its place: the graph, the record kind and the record's id and
number. A record keeps the envelope it was read or sealed with, so it
serializes to the same bytes again (revision files equal the tx copy).

Opening never creates a key. A shredded key opens as ``shredded: true`` with
empty values; a missing key or an envelope that does not authenticate is
``backend.corrupt``.
"""

from __future__ import annotations

import json
from dataclasses import replace

from graph_cli.kernel.canonical import JsonObject, canonical_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, GraphId, TxId
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.records import (
    Revision,
    TxRecord,
    revision_from_json,
    revision_payload,
    tx_from_json,
    tx_payload,
)
from graph_cli.kernel.sealing import (
    EnvelopeInvalid,
    KeyRing,
    KeyUnavailable,
    PlainKeyRing,
    Sealed,
)


class RecordCodec:
    """Seals and opens the records of one graph; ``ring`` is ``None`` for an unsealed one."""

    def __init__(self, graph_id: GraphId, ring: KeyRing | None = None) -> None:
        self.graph_id = graph_id
        self.sealed = ring is not None
        self.ring: KeyRing = ring if ring is not None else PlainKeyRing()

    # ---- writing --------------------------------------------------------

    def seal(self, tx: TxRecord) -> TxRecord:
        """``tx`` with fresh envelopes for itself and its revisions; as is when unsealed."""
        if not self.sealed:
            return tx
        revisions = tuple(self._seal_revision(revision) for revision in tx.revisions)
        kid = self.ring.tx_key(tx.tx_id)
        assert kid is not None
        sealed = self.ring.seal(
            kid, canonical_bytes(tx_payload(tx)), self._tx_aad(tx.tx_id, tx.seq)
        )
        return replace(tx, revisions=revisions, sealed=sealed)

    def _seal_revision(self, revision: Revision) -> Revision:
        kid = self.ring.entity_key(revision.id)
        assert kid is not None
        sealed = self.ring.seal(
            kid,
            canonical_bytes(revision_payload(revision)),
            self._revision_aad(revision.id, revision.rev),
        )
        return replace(revision, sealed=sealed)

    def entity_mac(self, entity: EntityId) -> bytes | None:
        """The MAC key of ``entity``'s content hash for a writer; creates its key."""
        return self.ring.mac_key(self.ring.entity_key(entity))

    # ---- reading --------------------------------------------------------

    def parse_tx(self, data: JsonObject) -> TxRecord:
        return tx_from_json(data, self if self.sealed else None)

    def parse_revision(self, data: JsonObject, path: str = "") -> Revision:
        return revision_from_json(data, path, self if self.sealed else None)

    def revision_mac(self, revision: Revision) -> bytes | None:
        """The MAC key a checker recomputes ``revision``'s content hash with, taken from its
        envelope; raises ``KeyUnavailable`` when the key is shredded or missing."""
        if revision.sealed is None:
            if self.sealed:
                raise GraphCliError(
                    "internal.error", "a revision of a sealed graph has no envelope"
                )
            return None
        return self.ring.mac_key(revision.sealed.kid)

    # Opener (kernel.records)

    def revision_payload(self, entity: EntityId, rev: int, sealed: Sealed) -> JsonObject | None:
        return self._open(sealed, self._revision_aad(entity, rev))

    def tx_payload(self, tx: TxId, seq: int, sealed: Sealed) -> JsonObject | None:
        return self._open(sealed, self._tx_aad(tx, seq))

    def _open(self, sealed: Sealed, aad: bytes) -> JsonObject | None:
        try:
            plaintext = self.ring.open(sealed, aad)
        except KeyUnavailable as exc:
            if exc.reason == "shredded":
                return None
            raise GraphCliError(
                "backend.corrupt",
                f"the key store has no key {exc.kid}; the data under it cannot be read "
                "(restore the key store from a backup)",
                details={"kid": str(exc.kid), "reason": exc.reason},
            ) from None
        except EnvelopeInvalid as exc:
            raise GraphCliError(
                "backend.corrupt",
                "a sealed record does not authenticate: changed, moved or under another key",
                details={"kid": str(exc.kid)},
            ) from None
        try:  # authenticated bytes this codec wrote canonically: no strict re-check needed
            return as_object(json.loads(plaintext), "sealed")
        except (ValueError, JsonShapeError) as exc:
            raise JsonShapeError(f"sealed: the payload is not a JSON object: {exc}") from None

    # ---- associated data ------------------------------------------------

    def _revision_aad(self, entity: EntityId, rev: int) -> bytes:
        return revision_aad(self.graph_id, entity, rev)

    def _tx_aad(self, tx: TxId, seq: int) -> bytes:
        return tx_aad(self.graph_id, tx, seq)


# The associated data is the JCS form of {graph, id, record, rev|seq}, written out: ids and
# numbers need no escaping, and every read of a sealed record builds it.


def revision_aad(graph: GraphId, entity: EntityId, rev: int) -> bytes:
    return f'{{"graph":"{graph}","id":"{entity}","record":"revision","rev":{rev}}}'.encode()


def tx_aad(graph: GraphId, tx: TxId, seq: int) -> bytes:
    return f'{{"graph":"{graph}","id":"{tx}","record":"tx","seq":{seq}}}'.encode()
