"""Sealing port: envelopes, keyed hashes and the plain key ring (plan T16, step 1)."""

from uuid import UUID

import pytest

from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.changeset import changeset_hash, changeset_to_json, parse_changeset
from graph_cli.kernel.ids import EntityId, EventId, TxId
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.records import RequestMeta
from graph_cli.kernel.sealing import (
    KeyRing,
    KeyUnavailable,
    Kid,
    PlainKeyRing,
    Sealed,
    keyed_hash,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
KID = Kid(UUID("0192f6d4-2a10-7b43-a5f6-000000000009"))


def test_keyed_hash_without_a_key_is_the_plain_hash() -> None:
    value: JsonValue = {"b": [1, "x"], "a": None}
    assert keyed_hash(value, None) == hash_value(value)


def test_keyed_hash_is_hmac_sha256_over_the_jcs_bytes() -> None:
    # Checked with: printf '{"a":1}' | openssl dgst -sha256 -mac HMAC -macopt hexkey:0001...1f
    expected = "sha256:b4cebde30982443ec36c18fa99ad10a70e705bba779f57c37e84decb64ae50fb"
    assert keyed_hash({"a": 1}, KEY) == expected
    assert keyed_hash({"a": 1}, OTHER_KEY) != expected


def test_keyed_hash_refuses_a_short_key() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        keyed_hash({"a": 1}, KEY[:16])


def test_changeset_hash_and_args_digest_take_a_key() -> None:
    cs = parse_changeset(
        {"schema": "graph_cli.changeset/1", "ops": [{"op": "create", "type": "ex:T"}]}
    )
    assert changeset_hash(cs) == hash_value(changeset_to_json(cs))
    assert changeset_hash(cs, KEY) == keyed_hash(changeset_to_json(cs), KEY)
    plain = RequestMeta.of("update", {"graph": "g"})
    assert plain.args_digest == hash_value({"command": "update", "args": {"graph": "g"}})
    keyed = RequestMeta.of("update", {"graph": "g"}, key=KEY)
    assert keyed.args_digest == keyed_hash({"command": "update", "args": {"graph": "g"}}, KEY)


def test_sealed_envelope_round_trip_uses_unpadded_base64url() -> None:
    sealed = Sealed(KID, b"\xfb\xff\x00" * 4, b"\xfe" * 17)
    data = sealed.to_json()
    assert data == {"kid": str(KID), "nonce": "-_8A" * 4, "ct": "_v7-_v7-_v7-_v7-_v7-_v4"}
    assert Sealed.from_json(data) == sealed


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"kid": str(KID), "nonce": "AA"}, "sealed: expected the keys"),
        ({"kid": str(KID), "nonce": "AA", "ct": "AA", "x": 1}, "sealed: expected the keys"),
        ({"kid": "nope", "nonce": "AA", "ct": "AA"}, "kid: not a UUID"),
        ({"kid": str(KID), "nonce": "AA==", "ct": "AA"}, "nonce: expected unpadded base64url"),
        ({"kid": str(KID), "nonce": "AA", "ct": "+/+/"}, "ct: expected unpadded base64url"),
        ({"kid": str(KID), "nonce": "AB", "ct": "AA"}, "nonce: expected unpadded base64url"),
        ({"kid": str(KID), "nonce": "AA", "ct": ""}, "ct: expected unpadded base64url"),
        ({"kid": str(KID), "nonce": "A", "ct": "AA"}, "nonce: expected unpadded base64url"),
    ],
)
def test_malformed_envelopes_are_refused(data: dict[str, JsonValue], message: str) -> None:
    with pytest.raises(JsonShapeError, match=message):
        Sealed.from_json(data)


def test_the_plain_key_ring_has_no_keys() -> None:
    ring: KeyRing = PlainKeyRing()
    entity = EntityId(UUID("0192f6d4-2a10-7b43-a5f6-000000000001"))
    tx = TxId(UUID("0192f6d4-2a10-7b43-a5f6-000000000002"))
    assert ring.entity_key(entity) is None
    assert ring.tx_key(tx) is None
    assert ring.file_key() is None
    assert ring.mac_key(ring.entity_key(entity)) is None
    assert not ring.is_shredded(KID)
    with pytest.raises(KeyUnavailable) as info:
        ring.mac_key(KID)
    assert (info.value.kid, info.value.reason) == (KID, "missing")
    with pytest.raises(KeyUnavailable):
        ring.seal(KID, b"x", b"aad")
    with pytest.raises(KeyUnavailable):
        ring.open(Sealed(KID, b"n", b"c"), b"aad")
    with pytest.raises(KeyUnavailable):
        ring.shred([KID], EventId(UUID("0192f6d4-2a10-7b43-a5f6-000000000003")))
