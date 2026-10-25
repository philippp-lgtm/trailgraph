"""Canonical JSON data model, JCS bytes and document hashes (contracts C.1-C.6).

The kernel knows the JSON data model, not files: functions here return bytes
and callers decide where they go.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
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


def hash_canonical_document(data: bytes, document: JsonObject, *, hash_key: str = "hash") -> str:
    """``hash_document(document)`` where ``data`` are its JCS bytes (``parse_canonical``).

    Hashes ``data`` with the member ``hash_key`` cut out instead of encoding
    the document again. Every quote inside a JSON string is escaped, so the
    member's bytes found exactly once are the top-level member; otherwise
    this falls back to ``hash_document``.
    """
    if hash_key not in document:
        return hash_bytes(data)
    member = canonical_bytes({hash_key: document[hash_key]})[1:-1]
    start = data.find(member)
    if start < 0 or data.find(member, start + 1) >= 0:
        return hash_document(document, hash_key=hash_key)
    end = start + len(member)
    if data[start - 1 : start] == b",":
        start -= 1
    elif data[end : end + 1] == b",":
        end += 1
    return hash_bytes(data[:start] + data[end:])


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


def parse_json(data: bytes | str, *, floats: Callable[[str], str] | None = None) -> JsonValue:
    """Parse JSON strictly into the C.1 data model.

    Refuses floats, NaN/Infinity, duplicate keys, out-of-range integers,
    lone surrogates, NUL and nesting deeper than the interpreter recurses.
    ``floats`` turns float literals into a string (subclass) instead, for
    input documents whose validator reports them
    (``kernel.changeset.FloatLiteral``).
    """
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        value: JsonValue = json.loads(
            text,
            parse_float=_reject_float if floats is None else floats,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except UnicodeDecodeError as exc:
        raise CanonicalError("input is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalError(f"invalid JSON: {exc}") from exc
    except RecursionError:
        raise CanonicalError("invalid JSON: nested too deeply") from None
    except CanonicalError:
        raise
    except ValueError as exc:  # such as an integer with more digits than int() converts
        raise CanonicalError(f"invalid JSON: {exc}") from None
    try:
        canonical_bytes(value)  # range, surrogate and NUL checks
    except RecursionError:
        raise CanonicalError("invalid JSON: nested too deeply") from None
    return value


def _safe_int(text: str) -> int:
    value = int(text)
    if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
        raise CanonicalError(f"integer out of range: {value}")
    return value


# Input where the C encoder may disagree with JCS: a 4-byte UTF-8 sequence
# (a key outside the BMP sorts by code point there, by UTF-16 code unit in
# JCS) and an escaped NUL (written by the encoder, refused by C.5).
_REFERENCE_ONLY: Final = re.compile(rb"[\xf0-\xf4]|\\u0000")


def parse_canonical(data: bytes) -> JsonValue:
    """Parse ``data`` that must be the JCS bytes (C.3) of its value.

    Accepts and refuses exactly what ``parse_json`` followed by the check
    ``canonical_bytes(value) == data`` would, without the slow encoder:
    after the strict parse (no floats, safe integers, unique keys) and
    without the bytes of ``_REFERENCE_ONLY``, ``json.dumps`` with sorted
    keys writes the JCS bytes. Anything else takes that reference path.
    """
    if _REFERENCE_ONLY.search(data) is None:
        try:
            value: JsonValue = json.loads(
                data.decode("utf-8"),
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                parse_int=_safe_int,
                object_pairs_hook=_unique_object,
            )
            encoded = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (ValueError, RecursionError):  # the reference names the error
            pass
        else:
            if encoded == data:
                return value
    value = parse_json(data)
    if canonical_bytes(value) != data:
        raise CanonicalError("value is not in JCS form")
    return value
