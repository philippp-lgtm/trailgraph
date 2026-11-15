"""Defensive parsing of S3 XML answers: the body of a store is untrusted input.

Bodies with a document type declaration or entity declaration are refused (no entity
expansion is ever possible), as are bodies with NUL bytes (an ASCII-incompatible encoding
such as UTF-16 would hide a declaration from the scan) and bodies over a size limit.
"""

from __future__ import annotations

import re
from typing import Final
from xml.etree import ElementTree

MAX_ERROR_BODY: Final = 64 * 1024
MAX_LIST_BODY: Final = 16 * 1024 * 1024

_DECLARATIONS: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse_xml(data: bytes, *, limit: int) -> ElementTree.Element | None:
    """The root element, or ``None`` when the body is too large, forbidden or malformed."""
    if len(data) > limit or b"\x00" in data or _DECLARATIONS.search(data):
        return None
    try:
        return ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError):
        return None


def local_name(tag: str) -> str:
    """The tag without its namespace (``{uri}Code`` -> ``Code``)."""
    return tag.rsplit("}", 1)[-1]


def child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def error_code(data: bytes) -> str | None:
    """The ``Code`` of an ``<Error>`` document; nothing else of the body is read."""
    root = parse_xml(data, limit=MAX_ERROR_BODY)
    if root is None or local_name(root.tag) != "Error":
        return None
    return child_text(root, "Code")
