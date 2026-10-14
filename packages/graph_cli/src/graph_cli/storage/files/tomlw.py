"""A minimal TOML writer with a fixed key order (contracts C.8).

Only what graph-admin writes: top-level keys and tables whose values are
strings, integers, booleans or arrays of strings. Reading uses the stdlib
``tomllib``; a round trip through it is the test oracle.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

TomlValue = str | int | bool | tuple[str, ...]
TomlTable = tuple[str | None, Sequence[tuple[str, TomlValue]]]  # (dotted header or None, items)

_BARE_KEY: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _string(text: str) -> str:
    out = []
    for char in text:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def _value(value: TomlValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    return "[" + ", ".join(_string(item) for item in value) + "]"


def dumps(tables: Sequence[TomlTable]) -> str:
    """Render tables in the given order; the root table (header ``None``) must come first."""
    blocks = []
    for index, (header, items) in enumerate(tables):
        if header is None and index != 0:
            raise ValueError("the root table must come first")
        lines = [] if header is None else [f"[{'.'.join(_key(p) for p in header.split('.'))}]"]
        lines += [f"{_key(key)} = {_value(value)}" for key, value in items]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
