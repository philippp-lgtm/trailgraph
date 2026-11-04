"""Sealing port: key ids, sealed envelopes, keyed hashes and the key ring (plan T16).

A sealed graph stores every instance value encrypted under a data key that
belongs to its entity, its tx or its stored file; purging destroys the keys.
The kernel defines the shapes only: no cipher runs here, and no key material
is stored. The key ring implementation lives in storage.

A graph of store format 1 is not sealed: its key ring (``PlainKeyRing``) hands
out no key ids, and every hash keeps its plain form (C.2).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal, NewType, Protocol
from uuid import UUID

from graph_cli.kernel.canonical import (
    HASH_PREFIX,
    JsonObject,
    JsonValue,
    canonical_bytes,
    hash_value,
)
from graph_cli.kernel.ids import EntityId, EventId, TxId
from graph_cli.kernel.json_access import JsonShapeError, req_str

Kid = NewType("Kid", UUID)  # id of one data key, a UUIDv7

KeyScope = Literal["entity", "tx", "file"]
KEY_SCOPES: Final[tuple[KeyScope, ...]] = ("entity", "tx", "file")

MAC_KEY_BYTES: Final = 32
# lowercase 8-4-4-4-12, version 7, RFC 9562 variant: every read of a sealed record checks one
_CANONICAL_UUID7: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class KeyUnavailable(Exception):
    """A data key cannot be used: shredded by a purge, or absent from the key store."""

    def __init__(self, kid: Kid, reason: Literal["shredded", "missing"]) -> None:
        super().__init__(f"key {kid} is {reason}")
        self.kid = kid
        self.reason = reason


class EnvelopeInvalid(Exception):
    """A sealed envelope does not authenticate under its key and associated data."""

    def __init__(self, kid: Kid) -> None:
        super().__init__(f"an envelope under key {kid} does not authenticate")
        self.kid = kid


@dataclass(frozen=True, slots=True)
class Sealed:
    """One sealed envelope: the ciphertext of a JSON document under the key ``kid``.

    The persisted form is ``{kid, nonce, ct}`` with ``nonce`` and ``ct`` in
    unpadded base64url (RFC 4648 section 5). The cipher and the associated data
    are defined by the record format that holds the envelope.
    """

    kid: Kid
    nonce: bytes
    ct: bytes

    def to_json(self) -> dict[str, JsonValue]:
        return {"kid": str(self.kid), "nonce": _b64encode(self.nonce), "ct": _b64encode(self.ct)}

    @classmethod
    def from_json(cls, data: JsonObject, path: str = "") -> Sealed:
        if set(data) != {"kid", "nonce", "ct"}:
            where = path.removesuffix(".") or "sealed"
            raise JsonShapeError(f"{where}: expected the keys kid, nonce and ct")
        text = req_str(data, "kid", path)
        # Canonical text only, so that the envelope's bytes survive a round trip.
        if _CANONICAL_UUID7.fullmatch(text) is None:
            raise JsonShapeError(f"{path}kid: not a UUIDv7 in canonical form")
        return cls(Kid(UUID(text)), _b64decode(data, "nonce", path), _b64decode(data, "ct", path))


def keyed_hash(value: JsonValue, key: bytes | None) -> str:
    """``sha256:<hex>`` over the JCS bytes of ``value``: HMAC-SHA256 under ``key``, or the
    plain C.2 hash when ``key`` is ``None`` (store format 1).

    The shape is the same either way; the record format defines which one a hash is.
    """
    if key is None:
        return hash_value(value)
    if len(key) != MAC_KEY_BYTES:
        raise ValueError(f"a MAC key has {MAC_KEY_BYTES} bytes, got {len(key)}")
    return HASH_PREFIX + hmac.new(key, canonical_bytes(value), hashlib.sha256).hexdigest()


class KeyRing(Protocol):
    """The data keys of one graph.

    ``entity_key`` and ``tx_key`` return the key of their subject and create it on
    first use; ``file_key`` creates a new key for one stored file. A ring of an
    unsealed graph returns ``None`` from all three, and ``mac_key(None)`` is ``None``,
    so callers compute the same hash either way:
    ``content_hash(projection, ring.mac_key(ring.entity_key(entity)))``.

    ``mac_key``, ``seal`` and ``open`` raise ``KeyUnavailable`` for a shredded or
    missing key; ``open`` raises ``EnvelopeInvalid`` for an envelope that does not
    authenticate. A new key is durable before the record that uses it is written.
    Readers and verify take the kid from the record they check, never from
    ``entity_key`` or ``tx_key``, so a read creates no key.
    """

    def entity_key(self, entity: EntityId) -> Kid | None: ...

    def tx_key(self, tx: TxId) -> Kid | None: ...

    def file_key(self) -> Kid | None: ...

    def mac_key(self, kid: Kid | None) -> bytes | None:
        """The key of the keyed hashes (``MAC_KEY_BYTES``), derived from the data key."""
        ...

    def seal(self, kid: Kid, plaintext: bytes, aad: bytes) -> Sealed: ...

    def open(self, sealed: Sealed, aad: bytes) -> bytes: ...

    def is_shredded(self, kid: Kid) -> bool: ...

    def shred(self, kids: Iterable[Kid], purge_event: EventId) -> None:
        """Destroy the key material of ``kids``, recording the purge event that did it."""
        ...

    def refresh(self) -> None:
        """Pick up shreds by other processes now; called when a read or write begins."""
        ...


class PlainKeyRing:
    """The key ring of an unsealed graph (store format 1): no keys, plain hashes."""

    def entity_key(self, entity: EntityId) -> Kid | None:
        return None

    def tx_key(self, tx: TxId) -> Kid | None:
        return None

    def file_key(self) -> Kid | None:
        return None

    def mac_key(self, kid: Kid | None) -> bytes | None:
        if kid is not None:
            raise KeyUnavailable(kid, "missing")
        return None

    def seal(self, kid: Kid, plaintext: bytes, aad: bytes) -> Sealed:
        raise KeyUnavailable(kid, "missing")

    def open(self, sealed: Sealed, aad: bytes) -> bytes:
        raise KeyUnavailable(sealed.kid, "missing")

    def is_shredded(self, kid: Kid) -> bool:
        return False

    def shred(self, kids: Iterable[Kid], purge_event: EventId) -> None:
        for kid in kids:
            raise KeyUnavailable(kid, "missing")

    def refresh(self) -> None:
        pass


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: JsonObject, key: str, path: str) -> bytes:
    text = req_str(data, key, path)
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raw = b""
    # The round trip refuses padding, other alphabets and non-zero trailing bits.
    if not raw or _b64encode(raw) != text:
        raise JsonShapeError(f"{path}{key}: expected unpadded base64url")
    return raw
