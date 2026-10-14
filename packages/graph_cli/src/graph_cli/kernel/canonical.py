"""Canonical JSON data model, JCS bytes and document hashes (contracts C.1-C.6).

The kernel knows the JSON data model, not files: functions here return bytes
and callers decide where they go.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

type JsonValue = bool | int | str | Sequence[JsonValue] | Mapping[str, JsonValue] | None
type JsonObject = Mapping[str, JsonValue]

MAX_SAFE_INT: Final = 2**53 - 1
HASH_PREFIX: Final = "sha256:"
GENESIS_HASH: Final = HASH_PREFIX + "0" * 64
HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class CanonicalError(ValueError):
    """A value is outside the canonical JSON data model (C.1, C.5)."""


def check_text(text: str) -> None:
    """Refuse lone surrogates and NUL (C.5). Callers map this to their error code."""
    if "\x00" in text:
        raise CanonicalError("NUL character is not allowed")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("lone surrogate is not allowed") from exc


def _key_order(key: str) -> bytes:
    # RFC 8785 sorts by UTF-16 code units; big-endian bytes compare the same way.
    return key.encode("utf-16-be", "surrogatepass")


def _check_scalar(value: object) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise CanonicalError(f"integer out of range: {value}")
        return
    if isinstance(value, str):
        check_text(value)
        return
    if isinstance(value, float):
        raise CanonicalError("floats are not allowed in canonical documents")
    raise CanonicalError(f"not a JSON value: {type(value).__name__}")


def _encode(value: object, out: list[str]) -> None:
    if isinstance(value, Mapping):
        out.append("{")
        items = sorted(value.items(), key=lambda kv: _key_order(_checked_key(kv[0])))
        for index, (key, item) in enumerate(items):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(item, out)
        out.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, str):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        _check_scalar(value)
        # json.dumps escapes exactly like ECMAScript JSON.stringify for strings
        # (short forms for \b \t \n \f \r, lowercase \u00xx otherwise).
        out.append(json.dumps(value, ensure_ascii=False))


def _checked_key(key: object) -> str:
    if not isinstance(key, str):
        raise CanonicalError(f"object keys must be strings, got {type(key).__name__}")
    check_text(key)
    return key


def canonical_bytes(value: JsonValue) -> bytes:
    """JCS (RFC 8785) bytes of a value within the C.1 data model."""
    out: list[str] = []
    _encode(value, out)
    return "".join(out).encode("utf-8")


def hash_bytes(data: bytes) -> str:
    """``sha256:<hex>`` over raw bytes (file listings in release metadata)."""
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def hash_value(value: JsonValue) -> str:
    """``sha256:<hex>`` over the JCS bytes of ``value``."""
    return hash_bytes(canonical_bytes(value))


def hash_document(document: JsonObject, *, hash_key: str = "hash") -> str:
    """C.2 document hash: JCS of the document without its own hash key."""
    return hash_value({k: v for k, v in document.items() if k != hash_key})


def jsonl_line(document: JsonObject) -> bytes:
    """One JSONL line (C.3): the JCS bytes of the full document plus ``\\n``."""
    return canonical_bytes(document) + b"\n"


def _sorted_copy(value: object) -> object:
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda kv: _key_order(_checked_key(kv[0])))
        return {key: _sorted_copy(item) for key, item in items}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_sorted_copy(item) for item in value]
    _check_scalar(value)
    return value


def pretty_bytes(value: JsonValue) -> bytes:
    """Single-document form (C.4): sorted keys, 2-space indent, UTF-8, final newline."""
    text = json.dumps(_sorted_copy(value), ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


def _reject_float(text: str) -> object:
    raise CanonicalError(f"floats are not allowed in canonical documents: {text}")


def _reject_constant(text: str) -> object:
    raise CanonicalError(f"non-finite numbers are not allowed: {text}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalError(f"duplicate key: {key!r}")
        result[key] = value
    return result


def parse_json(data: bytes | str) -> JsonValue:
    """Parse JSON strictly into the C.1 data model.

    Refuses floats, NaN/Infinity, duplicate keys, out-of-range integers,
    lone surrogates and NUL.
    """
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        value: JsonValue = json.loads(
            text,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except UnicodeDecodeError as exc:
        raise CanonicalError("input is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalError(f"invalid JSON: {exc}") from exc
    canonical_bytes(value)  # range, surrogate and NUL checks
    return value
