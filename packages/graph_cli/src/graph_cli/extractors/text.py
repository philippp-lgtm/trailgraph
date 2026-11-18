"""One rule for text that comes from the bytes of a file and becomes a name (file metadata).

Column names of a table and the names of a database's tables, views and columns are untrusted:
they end up in attribute values of single-line type and, for the parts of a database, in the
key of an entity that the reconcile planner accepts only as printable text of bounded length.
``label`` turns any text into one printable line: every character that is not printable
(controls, newlines, blanks other than the ASCII one, format characters) becomes a blank, runs of
blanks collapse, the ends are trimmed, and the result is cut to ``MAX_CHARACTERS`` characters
and ``MAX_BYTES`` bytes of UTF-8, never inside a character; what is left empty is the fallback.
"""

from __future__ import annotations

import re
from typing import Final

MAX_CHARACTERS: Final = 200
MAX_BYTES: Final = 400  # a part key is "<file id>/<name>": at most 512 bytes in UTF-8

_BLANKS: Final = re.compile(r" {2,}")


def label(text: str, fallback: str = "unnamed") -> str:
    """``text`` as one printable line within the limits, or ``fallback``."""
    printable = "".join(c if c.isprintable() else " " for c in text)
    return cut(_BLANKS.sub(" ", printable).strip()) or fallback


def cut(text: str, characters: int = MAX_CHARACTERS, size: int = MAX_BYTES) -> str:
    """The start of ``text`` that fits ``characters`` and ``size`` bytes, trimmed."""
    text = text[:characters]
    while len(text.encode("utf-8")) > size:
        text = text[:-1]
    return text.strip()
