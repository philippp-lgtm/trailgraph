"""Cipher primitives of sealed graphs (plan T16): AEAD envelopes, subkeys, key wrapping and
the chunked stream cipher of stored files.

The only module that imports ``cryptography`` (import contract), so every cipher call is
reviewable in one place. Import it only where a sealed graph needs it, so the library stays
out of the start-up time of commands that never touch one. Every failure to authenticate or
to parse raises ``CryptoError``; nothing here returns unauthenticated plaintext.

- **Envelopes**: AES-256-GCM with a random 96-bit nonce. A key seals few envelopes (the
  revisions of one entity, one tx), far below the bound for random nonces.
- **Subkeys**: HKDF-SHA256 (RFC 5869) from a data key, one per label: ``enc`` for the
  cipher, ``mac`` for the keyed hashes (``kernel.sealing.keyed_hash``).
- **Key wrapping**: AES-KW (RFC 3394) under the 256-bit key-encryption key.
- **Stored files**: a header, then chunks of up to ``BLOB_CHUNK`` plaintext bytes, each
  sealed with AES-256-GCM under the nonce ``prefix (7) || counter (4, big-endian) ||
  last (1)`` and the header as associated data (the STREAM construction). The counter
  refuses reordering, the last flag truncation and extension. The header is
  ``BLOB_MAGIC || kid (16) || prefix (7)``.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Iterator
from typing import Final, Literal, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from graph_cli.kernel.sealing import MAC_KEY_BYTES, Kid

KEY_BYTES: Final = MAC_KEY_BYTES  # data keys, subkeys and the key-encryption key
NONCE_BYTES: Final = 12
TAG_BYTES: Final = 16
WRAPPED_KEY_BYTES: Final = KEY_BYTES + 8

SubkeyLabel = Literal["enc", "mac"]
_SUBKEY_INFO: Final = b"graph_cli.dek/1 "

BLOB_MAGIC: Final = b"GCBLOB\x00\x01"
BLOB_CHUNK: Final = 64 * 1024
_PREFIX_BYTES: Final = 7
BLOB_HEADER_BYTES: Final = len(BLOB_MAGIC) + 16 + _PREFIX_BYTES
_SEALED_CHUNK: Final = BLOB_CHUNK + TAG_BYTES
_MAX_CHUNKS: Final = 2**32


class CryptoError(Exception):
    """Ciphertext, wrapped key or blob that does not authenticate or parse."""


class Reader(Protocol):
    def read(self, size: int, /) -> bytes: ...


def new_key() -> bytes:
    """A random 256-bit key (a data key or a key-encryption key)."""
    return secrets.token_bytes(KEY_BYTES)


def hkdf_sha256(ikm: bytes, *, salt: bytes | None, info: bytes, length: int) -> bytes:
    """RFC 5869 extract-and-expand."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def subkey(dek: bytes, label: SubkeyLabel) -> bytes:
    """The subkey of a data key for one purpose; distinct labels give independent keys."""
    _check_key(dek, "data key")
    return hkdf_sha256(dek, salt=None, info=_SUBKEY_INFO + label.encode("ascii"), length=KEY_BYTES)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM under a fresh random nonce: ``(nonce, ciphertext with tag)``."""
    _check_key(key, "cipher key")
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, nonce: bytes, ct: bytes, aad: bytes) -> bytes:
    """The plaintext of ``seal``; ``CryptoError`` unless key, nonce, ciphertext and AAD match."""
    _check_key(key, "cipher key")
    if len(nonce) != NONCE_BYTES or len(ct) < TAG_BYTES:
        raise CryptoError("malformed envelope")
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag:
        raise CryptoError("envelope does not authenticate") from None


def wrap_key(kek: bytes, dek: bytes) -> bytes:
    """AES-KW of a data key under the key-encryption key (``WRAPPED_KEY_BYTES``)."""
    _check_key(kek, "key-encryption key")
    _check_key(dek, "data key")
    return aes_key_wrap(kek, dek)


def unwrap_key(kek: bytes, wrapped: bytes) -> bytes:
    _check_key(kek, "key-encryption key")
    if len(wrapped) != WRAPPED_KEY_BYTES:
        raise CryptoError("malformed wrapped key")
    try:
        return aes_key_unwrap(kek, wrapped)
    except InvalidUnwrap:
        raise CryptoError("wrapped key does not authenticate") from None


def seal_blob(key: bytes, kid: Kid, chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Encrypt a stream of any chunking: yields the header, then one sealed chunk per
    ``BLOB_CHUNK`` plaintext bytes. The last chunk may be full; it is empty only for an
    empty stream."""
    _check_key(key, "cipher key")
    prefix = secrets.token_bytes(_PREFIX_BYTES)
    header = BLOB_MAGIC + kid.bytes + prefix
    yield header
    aead = AESGCM(key)
    counter = 0
    buffer = bytearray()
    for data in chunks:
        buffer += data
        start = 0
        # Strictly more than one chunk: the last one waits for the end of the stream.
        while len(buffer) - start > BLOB_CHUNK:
            plain = bytes(buffer[start : start + BLOB_CHUNK])
            yield aead.encrypt(_chunk_nonce(prefix, counter, last=False), plain, header)
            start += BLOB_CHUNK
            counter += 1
        del buffer[:start]
    yield aead.encrypt(_chunk_nonce(prefix, counter, last=True), bytes(buffer), header)


def parse_blob_header(header: bytes) -> tuple[Kid, bytes]:
    """The key id and nonce prefix of a sealed blob's first ``BLOB_HEADER_BYTES``."""
    if len(header) != BLOB_HEADER_BYTES or not header.startswith(BLOB_MAGIC):
        raise CryptoError("not a sealed blob")
    kid_end = len(BLOB_MAGIC) + 16
    return Kid(UUID(bytes=header[len(BLOB_MAGIC) : kid_end])), header[kid_end:]


def open_blob(key_for: Callable[[Kid], bytes], source: Reader) -> Iterator[bytes]:
    """Decrypt a sealed blob chunk by chunk. ``key_for`` maps the header's key id to the
    cipher key. Raises ``CryptoError`` at the first chunk that does not authenticate, so a
    consumer must discard what it received when the iteration fails."""
    header = _read_full(source, BLOB_HEADER_BYTES)
    kid, prefix = parse_blob_header(header)
    key = key_for(kid)
    _check_key(key, "cipher key")
    aead = AESGCM(key)
    counter = 0
    current = _read_full(source, _SEALED_CHUNK)
    while True:
        following = _read_full(source, _SEALED_CHUNK) if len(current) == _SEALED_CHUNK else b""
        last = not following
        yield _open_chunk(aead, prefix, counter, current, header, last=last)
        if last:
            return
        current = following
        counter += 1


def _open_chunk(
    aead: AESGCM, prefix: bytes, counter: int, sealed: bytes, header: bytes, *, last: bool
) -> bytes:
    if len(sealed) < TAG_BYTES:
        raise CryptoError("blob is truncated")
    try:
        return aead.decrypt(_chunk_nonce(prefix, counter, last=last), sealed, header)
    except InvalidTag:
        raise CryptoError(f"blob chunk {counter} does not authenticate") from None


def _chunk_nonce(prefix: bytes, counter: int, *, last: bool) -> bytes:
    if counter >= _MAX_CHUNKS:
        raise CryptoError("blob has too many chunks")
    return prefix + counter.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def _read_full(source: Reader, size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        part = source.read(remaining)
        if not part:
            break
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def _check_key(key: bytes, what: str) -> None:
    if len(key) != KEY_BYTES:
        raise ValueError(f"a {what} has {KEY_BYTES} bytes, got {len(key)}")
