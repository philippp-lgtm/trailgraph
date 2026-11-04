"""Cipher primitives of sealed graphs (plan T16, step 2): RFC vectors, tampering, and the
chunked blob stream."""

import io
from collections.abc import Iterator
from uuid import UUID

import pytest

from graph_cli.kernel.sealing import MAC_KEY_BYTES, Kid
from graph_cli.storage.common.crypto import (
    BLOB_CHUNK,
    BLOB_HEADER_BYTES,
    KEY_BYTES,
    TAG_BYTES,
    CryptoError,
    hkdf_sha256,
    new_key,
    open_blob,
    open_sealed,
    parse_blob_header,
    seal,
    seal_blob,
    subkey,
    unwrap_key,
    wrap_key,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
KID = Kid(UUID("0192f6d4-2a10-7b43-a5f6-000000000009"))
OTHER_KID = Kid(UUID("0192f6d4-2a10-7b43-a5f6-00000000000a"))
SEALED_CHUNK = BLOB_CHUNK + TAG_BYTES


def test_hkdf_matches_rfc_5869_case_1() -> None:
    okm = hkdf_sha256(
        bytes.fromhex("0b" * 22),
        salt=bytes.fromhex("000102030405060708090a0b0c"),
        info=bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"),
        length=42,
    )
    assert okm == bytes.fromhex(
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_subkeys_are_independent_per_label_and_key() -> None:
    enc, mac = subkey(KEY, "enc"), subkey(KEY, "mac")
    assert len(enc) == len(mac) == 32
    assert enc != mac
    assert subkey(KEY, "enc") == enc
    assert subkey(OTHER_KEY, "enc") != enc
    with pytest.raises(ValueError, match="32 bytes"):
        subkey(KEY[:16], "enc")


def test_aes_kw_matches_rfc_3394_section_4_6() -> None:
    kek = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    dek = bytes.fromhex("00112233445566778899aabbccddeeff000102030405060708090a0b0c0d0e0f")
    wrapped = bytes.fromhex(
        "28c9f404c4b810f4cbccb35cfb87f8263f5786e2d80ed326cbc7f0e71a99f43bfb988b9b7a02dd21"
    )
    assert wrap_key(kek, dek) == wrapped
    assert unwrap_key(kek, wrapped) == dek


def test_unwrap_refuses_a_wrong_kek_or_a_damaged_key() -> None:
    wrapped = wrap_key(KEY, OTHER_KEY)
    with pytest.raises(CryptoError, match="does not authenticate"):
        unwrap_key(OTHER_KEY, wrapped)
    with pytest.raises(CryptoError, match="does not authenticate"):
        unwrap_key(KEY, bytes([wrapped[0] ^ 1]) + wrapped[1:])
    with pytest.raises(CryptoError, match="malformed"):
        unwrap_key(KEY, wrapped[:-1])


def test_aes_gcm_opens_the_gcm_spec_test_case_16() -> None:
    # McGrew and Viega, "The Galois/Counter Mode of Operation", test case 16 (256-bit key).
    key = bytes.fromhex("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308")
    plaintext = bytes.fromhex(
        "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
        "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39"
    )
    ct = bytes.fromhex(
        "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
        "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662"
        "76fc6ece0f4e1768cddf8853bb2d551b"
    )
    nonce = bytes.fromhex("cafebabefacedbaddecaf888")
    aad = bytes.fromhex("feedfacedeadbeeffeedfacedeadbeefabaddad2")
    assert open_sealed(key, nonce, ct, aad) == plaintext


def test_seal_round_trip_with_a_fresh_nonce() -> None:
    first, second = seal(KEY, b"value", b"aad"), seal(KEY, b"value", b"aad")
    assert first[0] != second[0] and first[1] != second[1]
    assert open_sealed(KEY, *first, b"aad") == b"value"
    assert open_sealed(KEY, *seal(KEY, b"", b""), b"") == b""


def test_open_refuses_tampering_wrong_aad_and_wrong_key() -> None:
    nonce, ct = seal(KEY, b"value", b"graph/entity/1")
    for args in (
        (KEY, nonce, bytes([ct[0] ^ 1]) + ct[1:], b"graph/entity/1"),
        (KEY, nonce, ct, b"graph/entity/2"),
        (OTHER_KEY, nonce, ct, b"graph/entity/1"),
        (KEY, bytes([nonce[0] ^ 1]) + nonce[1:], ct, b"graph/entity/1"),
    ):
        with pytest.raises(CryptoError, match="does not authenticate"):
            open_sealed(*args)
    with pytest.raises(CryptoError, match="malformed"):
        open_sealed(KEY, nonce[:8], ct, b"graph/entity/1")
    with pytest.raises(CryptoError, match="malformed"):
        open_sealed(KEY, nonce, ct[: TAG_BYTES - 1], b"graph/entity/1")


def test_new_keys_are_random_and_256_bit() -> None:
    assert len(new_key()) == 32
    assert new_key() != new_key()


def pieces(data: bytes, size: int) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def sealed_blob(data: bytes, piece: int = 4096, key: bytes = KEY, kid: Kid = KID) -> bytes:
    return b"".join(seal_blob(key, kid, pieces(data, piece)))


def opened(blob: bytes, key: bytes = KEY) -> bytes:
    return b"".join(open_blob(lambda kid: key, io.BytesIO(blob)))


def chunks_of(blob: bytes) -> tuple[bytes, list[bytes]]:
    body = blob[BLOB_HEADER_BYTES:]
    return blob[:BLOB_HEADER_BYTES], list(pieces(body, SEALED_CHUNK))


@pytest.mark.parametrize(
    "size", [0, 1, BLOB_CHUNK - 1, BLOB_CHUNK, BLOB_CHUNK + 1, 3 * BLOB_CHUNK, 3 * BLOB_CHUNK + 7]
)
@pytest.mark.parametrize("piece", [1000, BLOB_CHUNK, 10 * BLOB_CHUNK])
def test_blob_round_trip_for_any_size_and_chunking(size: int, piece: int) -> None:
    data = bytes(i % 251 for i in range(size))
    blob = sealed_blob(data, piece)
    full, rest = divmod(size, BLOB_CHUNK)
    chunk_count = max(1, full + (1 if rest else 0))
    assert len(blob) == BLOB_HEADER_BYTES + size + chunk_count * TAG_BYTES
    assert opened(blob) == data


def test_blob_header_names_the_key() -> None:
    blob = sealed_blob(b"x")
    kid, salt, prefix = parse_blob_header(blob[:BLOB_HEADER_BYTES])
    assert (kid, len(salt), len(prefix)) == (KID, 16, 7)
    seen: list[Kid] = []

    def key_for(kid: Kid) -> bytes:
        seen.append(kid)
        return KEY

    assert b"".join(open_blob(key_for, io.BytesIO(blob))) == b"x"
    assert seen == [KID]
    assert sealed_blob(b"x") != blob  # a fresh nonce prefix per blob


def test_blob_refuses_truncation_at_a_chunk_boundary() -> None:
    header, chunks = chunks_of(sealed_blob(bytes(3 * BLOB_CHUNK + 5)))
    with pytest.raises(CryptoError, match="chunk 2 does not authenticate"):
        opened(header + b"".join(chunks[:3]))  # the last flag is missing
    with pytest.raises(CryptoError, match="truncated"):
        opened(header)


def test_blob_refuses_truncation_inside_a_chunk() -> None:
    blob = sealed_blob(bytes(2 * BLOB_CHUNK))
    with pytest.raises(CryptoError, match="does not authenticate"):
        opened(blob[:-1])
    with pytest.raises(CryptoError, match="truncated"):
        opened(blob[: BLOB_HEADER_BYTES + SEALED_CHUNK + TAG_BYTES - 1])


def test_blob_refuses_reordered_repeated_and_appended_chunks() -> None:
    header, chunks = chunks_of(sealed_blob(bytes(range(256)) * 1024))  # four chunks
    assert len(chunks) == 4
    with pytest.raises(CryptoError, match="chunk 0 does not authenticate"):
        opened(header + b"".join([chunks[1], chunks[0], *chunks[2:]]))
    with pytest.raises(CryptoError, match="chunk 1 does not authenticate"):
        opened(header + b"".join([chunks[0], chunks[0], *chunks[2:]]))
    with pytest.raises(CryptoError, match="chunk 3 does not authenticate"):
        opened(header + b"".join([*chunks, chunks[3]]))  # the old last chunk is no longer last


def test_blob_refuses_chunks_of_another_blob_under_the_same_key() -> None:
    header, chunks = chunks_of(sealed_blob(bytes(2 * BLOB_CHUNK + 1)))
    _, foreign = chunks_of(sealed_blob(bytes(2 * BLOB_CHUNK + 1)))
    with pytest.raises(CryptoError, match="chunk 1 does not authenticate"):
        opened(header + b"".join([chunks[0], foreign[1], chunks[2]]))


def test_blob_refuses_a_changed_header_flipped_bit_wrong_key() -> None:
    blob = sealed_blob(bytes(100))
    other_kid = sealed_blob(bytes(100), kid=OTHER_KID)[:BLOB_HEADER_BYTES]
    with pytest.raises(CryptoError, match="chunk 0 does not authenticate"):
        opened(other_kid[:24] + blob[24:])  # another kid, same prefix: the AAD differs
    with pytest.raises(CryptoError, match="chunk 0 does not authenticate"):
        opened(blob[:-1] + bytes([blob[-1] ^ 1]))
    with pytest.raises(CryptoError, match="chunk 0 does not authenticate"):
        opened(blob, OTHER_KEY)
    with pytest.raises(CryptoError, match="not a sealed blob"):
        opened(b"plain bytes of a format-1 blob, longer than a header")
    with pytest.raises(CryptoError, match="not a sealed blob"):
        opened(blob[:10])
    v4_kid = UUID("0192f6d4-2a10-4b43-a5f6-000000000009").bytes
    with pytest.raises(CryptoError, match="not a sealed blob"):
        opened(blob[:8] + v4_kid + blob[24:])


def test_blob_arguments_are_checked_before_the_first_chunk() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        seal_blob(KEY[:16], KID, [b"x"])
    with pytest.raises(CryptoError, match="not a sealed blob"):
        open_blob(lambda kid: KEY, io.BytesIO(b"short"))


def test_the_mac_subkey_has_the_kernel_s_mac_key_size() -> None:
    assert KEY_BYTES == MAC_KEY_BYTES
