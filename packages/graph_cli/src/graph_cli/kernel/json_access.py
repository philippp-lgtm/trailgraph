"""Typed access to parsed JSON documents.

Readers of persisted documents use these helpers so that a malformed field
raises one exception type with the field path. Callers map
``JsonShapeError`` to their own error code (for example ``backend.corrupt``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_cli.kernel.canonical import JsonObject, JsonValue


class JsonShapeError(ValueError):
    """A document does not have the expected shape."""


def as_object(value: JsonValue, path: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise JsonShapeError(f"{path}: expected an object")
    return value


def as_list(value: JsonValue, path: str) -> Sequence[JsonValue]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise JsonShapeError(f"{path}: expected an array")
    return value


def req(doc: JsonObject, key: str, path: str = "") -> JsonValue:
    if key not in doc:
        raise JsonShapeError(f"{path}{key}: missing")
    return doc[key]


def req_str(doc: JsonObject, key: str, path: str = "") -> str:
    value = req(doc, key, path)
    if not isinstance(value, str):
        raise JsonShapeError(f"{path}{key}: expected a string")
    return value


def opt_str(doc: JsonObject, key: str, path: str = "") -> str | None:
    return None if doc.get(key) is None else req_str(doc, key, path)


def req_int(doc: JsonObject, key: str, path: str = "") -> int:
    value = req(doc, key, path)
    if isinstance(value, bool) or not isinstance(value, int):
        raise JsonShapeError(f"{path}{key}: expected an integer")
    return value


def opt_int(doc: JsonObject, key: str, path: str = "") -> int | None:
    return None if doc.get(key) is None else req_int(doc, key, path)


def req_bool(doc: JsonObject, key: str, path: str = "") -> bool:
    value = req(doc, key, path)
    if not isinstance(value, bool):
        raise JsonShapeError(f"{path}{key}: expected a boolean")
    return value


def req_obj(doc: JsonObject, key: str, path: str = "") -> JsonObject:
    return as_object(req(doc, key, path), f"{path}{key}")


def opt_obj(doc: JsonObject, key: str, path: str = "") -> JsonObject:
    """An optional object; absent means empty."""
    return {} if doc.get(key) is None else req_obj(doc, key, path)


def req_list(doc: JsonObject, key: str, path: str = "") -> Sequence[JsonValue]:
    return as_list(req(doc, key, path), f"{path}{key}")


def opt_list(doc: JsonObject, key: str, path: str = "") -> Sequence[JsonValue]:
    """An optional array; absent means empty."""
    return () if doc.get(key) is None else req_list(doc, key, path)


def str_list(doc: JsonObject, key: str, path: str = "") -> tuple[str, ...]:
    items = opt_list(doc, key, path)
    if not all(isinstance(item, str) for item in items):
        raise JsonShapeError(f"{path}{key}: expected an array of strings")
    return tuple(str(item) for item in items)
