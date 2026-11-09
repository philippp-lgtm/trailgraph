"""MemoryKeyRing: the data keys of a sealed in-memory graph (plan T16).

The counterpart of the key store for ``InMemoryStore``: random data keys in a
dict, gone with the process. Shredding drops the key and remembers the kid,
so the contract suite runs every sealed-format path on both backends.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import UUID

from graph_cli.kernel.ids import EntityId, EventId, IdProvider, TxId
from graph_cli.kernel.sealing import (
    EnvelopeInvalid,
    KeyRing,
    KeyScope,
    KeyUnavailable,
    Kid,
    Sealed,
)
from graph_cli.storage.common import crypto


class MemoryKeyRing:
    """Thread-safe; ``ids`` mints the kids (UUIDv7)."""

    def __init__(self, ids: IdProvider) -> None:
        self._ids = ids
        self._lock = threading.Lock()
        self._keys: dict[Kid, bytes] = {}
        self._subjects: dict[tuple[KeyScope, UUID], Kid] = {}
        self._shredded: dict[Kid, EventId] = {}
        self._generation = 0

    def entity_key(self, entity: EntityId) -> Kid:
        return self._subject_key("entity", entity)

    def tx_key(self, tx: TxId) -> Kid:
        return self._subject_key("tx", tx)

    def file_key(self) -> Kid:
        with self._lock:
            return self._mint()

    def mac_key(self, kid: Kid | None) -> bytes | None:
        if kid is None:
            return None
        return crypto.subkey(self._key(kid), "mac")

    def seal(self, kid: Kid, plaintext: bytes, aad: bytes) -> Sealed:
        nonce, ct = crypto.seal(crypto.subkey(self._key(kid), "enc"), plaintext, aad)
        return Sealed(kid, nonce, ct)

    def open(self, sealed: Sealed, aad: bytes) -> bytes:
        key = crypto.subkey(self._key(sealed.kid), "enc")
        try:
            return crypto.open_sealed(key, sealed.nonce, sealed.ct, aad)
        except crypto.CryptoError:
            raise EnvelopeInvalid(sealed.kid) from None

    def is_shredded(self, kid: Kid) -> bool:
        return kid in self._shredded

    def shred(self, kids: Iterable[Kid], purge_event: EventId) -> None:
        targets = sorted(set(kids))
        with self._lock:
            for kid in targets:
                if kid not in self._keys and kid not in self._shredded:
                    raise KeyUnavailable(kid, "missing")
            for kid in targets:
                if self._keys.pop(kid, None) is not None:
                    self._shredded[kid] = purge_event
            self._generation += 1

    def refresh(self) -> None:
        pass

    @property
    def generation(self) -> int:
        return self._generation

    def _subject_key(self, scope: KeyScope, subject: UUID) -> Kid:
        with self._lock:
            kid = self._subjects.get((scope, subject))
            if kid is None:
                kid = self._subjects[(scope, subject)] = self._mint()
            return kid

    def _mint(self) -> Kid:
        kid = Kid(self._ids.new_id())
        self._keys[kid] = crypto.new_key()
        return kid

    def _key(self, kid: Kid) -> bytes:
        with self._lock:
            key = self._keys.get(kid)
        if key is None:
            raise KeyUnavailable(kid, "shredded" if kid in self._shredded else "missing")
        return key


if TYPE_CHECKING:  # static proof that it implements the port

    def _conforms(ring: MemoryKeyRing) -> KeyRing:
        return ring
