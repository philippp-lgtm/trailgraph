"""Canonical JSON, JCS bytes and hashes (contracts C.1-C.6)."""

import json

import pytest

from graph_cli.kernel.canonical import (
    GENESIS_HASH,
    CanonicalError,
    canonical_bytes,
    check_text,
    hash_document,
    hash_value,
    jsonl_line,
    parse_json,
    pretty_bytes,
)


def test_key_order_follows_utf16_code_units() -> None:
    # RFC 8785 section 3.2.3 sorting example (values replaced by integers).
    keys = ["€", "\r", "דּ", "1", "\U0001f600", "\u0080", "ö"]
    doc = {key: index for index, key in enumerate(keys)}
    expected_order = ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    encoded = canonical_bytes(doc).decode("utf-8")
    positions = [encoded.index(json.dumps(key, ensure_ascii=False)) for key in expected_order]
    assert positions == sorted(positions)


def test_compact_output_and_string_escapes() -> None:
    line_separator = chr(0x2028)  # stays literal, like ECMAScript JSON.stringify
    doc = {"b": [1, True, None], "a": 'x"\\\n\t\x01\x7fé' + line_separator}
    expected = '{"a":"x\\"\\\\\\n\\t\\u0001\x7fé' + line_separator + '","b":[1,true,null]}'
    assert canonical_bytes(doc) == expected.encode()


def test_nested_keys_are_sorted() -> None:
    assert canonical_bytes({"z": {"b": 1, "a": 2}, "a": []}) == b'{"a":[],"z":{"a":2,"b":1}}'


@pytest.mark.parametrize(
    "value",
    [1.5, 2**53, -(2**53), {"k": float("nan")}, {1: "x"}, "a\x00b", "\ud800", object()],
)
def test_values_outside_the_data_model_are_refused(value: object) -> None:
    with pytest.raises(CanonicalError):
        canonical_bytes(value)  # type: ignore[arg-type]


def test_safe_integer_bounds_are_accepted() -> None:
    assert canonical_bytes([2**53 - 1, -(2**53 - 1)]) == b"[9007199254740991,-9007199254740991]"


def test_hash_document_ignores_its_own_hash_key() -> None:
    doc = {"a": 1, "hash": "sha256:old"}
    assert hash_document(doc) == hash_value({"a": 1})
    assert hash_document(doc).startswith("sha256:")
    assert len(hash_document(doc)) == len("sha256:") + 64


def test_hash_is_invariant_to_key_order() -> None:
    assert hash_value({"a": 1, "b": 2}) == hash_value({"b": 2, "a": 1})


def test_genesis_hash() -> None:
    assert GENESIS_HASH == "sha256:" + "0" * 64


def test_jsonl_line() -> None:
    assert jsonl_line({"b": 1, "a": "é"}) == '{"a":"é","b":1}\n'.encode()


def test_pretty_bytes() -> None:
    assert pretty_bytes({"b": [1], "a": {}}) == b'{\n  "a": {},\n  "b": [\n    1\n  ]\n}\n'


def test_parse_json_round_trip() -> None:
    doc = {"a": [1, "x", None, True], "b": {"c": -3}}
    assert parse_json(canonical_bytes(doc)) == doc


@pytest.mark.parametrize(
    "text",
    ['{"a": 1.0}', '{"a": NaN}', '{"a": 1, "a": 2}', "[9007199254740992]", "{", '"\\u0000"'],
)
def test_parse_json_is_strict(text: str) -> None:
    with pytest.raises(CanonicalError):
        parse_json(text)


def test_parse_json_refuses_invalid_utf8() -> None:
    with pytest.raises(CanonicalError):
        parse_json(b'"\xff"')


def test_check_text() -> None:
    check_text("plain é \U0001f600")
    for bad in ("\x00", "\udc00"):
        with pytest.raises(CanonicalError):
            check_text(bad)
