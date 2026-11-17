"""The type of a file decided from its bytes, never from its name (file metadata M5, M7).

``detect_content`` looks at the first ``DETECT_BYTES`` bytes: known signatures first, then a
text test. ``compatible`` says whether the media type a file was *recorded* with at add time
(from the name when no signature matched) can be true of the detected one; the format
extractor stores the answer as ``media_type_mismatch``.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Final

from graph_cli.extractors.media_types import OCTET_STREAM, sniff

DETECT_BYTES: Final = 64 * 1024
TEXT_PLAIN: Final = "text/plain"
SQLITE: Final = "application/vnd.sqlite3"
ZIP: Final = "application/zip"
GZIP: Final = "application/gzip"

# Encoding names are Python codec names with underscores (the enum values of the module).
UTF_8: Final = "utf_8"
UTF_8_SIG: Final = "utf_8_sig"
UTF_16_LE: Final = "utf_16_le"
UTF_16_BE: Final = "utf_16_be"

_SQLITE_MAGIC: Final = b"SQLite format 3\x00"
_ZIP_MAGICS: Final = (b"PK\x03\x04", b"PK\x05\x06")
_GZIP_MAGIC: Final = b"\x1f\x8b"
# An OpenDocument file is a zip whose first entry is the stored text ``mimetype`` (offset 30).
_ODF_ENTRY: Final = b"PK\x03\x04"
_ODF_NAME_AT: Final = 30
_ODF_VALUE_AT: Final = 38
_ODF_PREFIX: Final = b"application/vnd.oasis.opendocument."


@dataclass(frozen=True)
class Detected:
    media_type: str
    text_encoding: str | None = None


def detect_content(head: bytes) -> Detected:
    """What the first bytes of a file say it is.

    ``head`` is the start of the file, up to ``DETECT_BYTES``; the text test needs the whole
    of it and tolerates a multi-byte character cut at its end. An empty file is empty text.
    """
    head = head[:DETECT_BYTES]
    if head.startswith(_SQLITE_MAGIC):
        return Detected(SQLITE)
    if head.startswith(_ZIP_MAGICS):
        return Detected(_zip_type(head))
    if head.startswith(_GZIP_MAGIC):
        return Detected(GZIP)
    sniffed = sniff(head)
    if sniffed is not None:
        return Detected(sniffed)
    encoding = _text_encoding(head)
    if encoding is not None:
        return Detected(TEXT_PLAIN, encoding)
    return Detected(OCTET_STREAM)


def _zip_type(head: bytes) -> str:
    if head.startswith(_ODF_ENTRY) and head[_ODF_NAME_AT:_ODF_VALUE_AT] == b"mimetype":
        rest = head[_ODF_VALUE_AT : _ODF_VALUE_AT + 96]
        if rest.startswith(_ODF_PREFIX):
            name = rest.split(b"PK", 1)[0]
            try:
                return name.decode("ascii")
            except UnicodeDecodeError:
                return ZIP
    return ZIP


def _text_encoding(head: bytes) -> str | None:
    if head.startswith(codecs.BOM_UTF8):
        return UTF_8_SIG if _decodes(head[3:], "utf-8") else None
    if head.startswith(codecs.BOM_UTF16_LE):
        return UTF_16_LE if _decodes(head[2:], "utf-16-le") else None
    if head.startswith(codecs.BOM_UTF16_BE):
        return UTF_16_BE if _decodes(head[2:], "utf-16-be") else None
    if b"\x00" in head:
        return None
    return UTF_8 if _decodes(head, "utf-8") else None


def _decodes(data: bytes, codec: str) -> bool:
    """Valid in ``codec`` apart from a character cut off at the end, and no control NUL."""
    decoder = codecs.getincrementaldecoder(codec)()
    try:
        text = decoder.decode(data, final=False)
    except UnicodeDecodeError:
        return False
    return "\x00" not in text


_TEXT_LIKE_APPLICATION: Final = frozenset({"application/json", "application/xml"})
_CONTAINER_PREFIXES: Final = (
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
)


def compatible(recorded: str, detected: str) -> bool:
    """Whether a file recorded as ``recorded`` can be of the detected type.

    Compatible: equal types; a file that claimed nothing (``application/octet-stream``); a
    text type (``text/*``, JSON, XML, ``+json``, ``+xml``) with detected plain text; an OOXML or
    OpenDocument type with a detected zip container. Everything else is a mismatch.
    """
    if recorded in (detected, OCTET_STREAM):
        return True
    if detected == TEXT_PLAIN:
        return (
            recorded.startswith("text/")
            or recorded in _TEXT_LIKE_APPLICATION
            or recorded.endswith(("+json", "+xml"))
        )
    if detected == ZIP:
        return recorded.startswith(_CONTAINER_PREFIXES)
    return False
