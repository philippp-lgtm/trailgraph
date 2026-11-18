"""The type of a file decided from its bytes, never from its name (file metadata M5, M7).

``detect_content`` looks at the first ``DETECT_BYTES`` bytes: known signatures first, then a
text test. ``compatible`` says whether the media type a file was *recorded* with at add time
(from the name when no signature matched) can be true of the detected one; the format
extractor stores the answer as ``media_type_mismatch``.
"""

from __future__ import annotations

import codecs
import re
import struct
from dataclasses import dataclass
from typing import Final

from graph_cli.extractors.media_types import OCTET_STREAM, sniff

DETECT_BYTES: Final = 64 * 1024
TEXT_PLAIN: Final = "text/plain"
SQLITE: Final = "application/vnd.sqlite3"
ZIP: Final = "application/zip"
GZIP: Final = "application/gzip"

# The enum values of ``files:TextEncoding``: stable names of the encoding, not Python codec
# names (a codec alias such as ``utf_8_sig`` says nothing to an agent reading the record).
UTF_8: Final = "utf8"
UTF_8_BOM: Final = "utf8_bom"
UTF_16_LE: Final = "utf16le"
UTF_16_BE: Final = "utf16be"

_SQLITE_MAGIC: Final = b"SQLite format 3\x00"
_ZIP_MAGICS: Final = (b"PK\x03\x04", b"PK\x05\x06")
_GZIP_MAGIC: Final = b"\x1f\x8b"
# The pattern of the datatype ``files:MediaType`` (one definition, a test compares it with the
# module): every media type this package returns matches it, so a record can always be written.
MEDIA_TYPE_PATTERN: Final = r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
_MEDIA_TYPE: Final = re.compile(MEDIA_TYPE_PATTERN)
# An OpenDocument file is a zip whose first entry is the stored text ``mimetype``. The local
# file header (30 bytes) gives the method, the stored size, the name and the extra field length.
_ZIP_LOCAL_HEADER: Final = struct.Struct("<4sHHHHHIIIHH")
_ODF_PREFIX: Final = "application/vnd.oasis.opendocument."
_ODF_MAX_VALUE: Final = 128
_ZIP_STORED: Final = 0


@dataclass(frozen=True)
class Detected:
    media_type: str
    text_encoding: str | None = None


def detect_content(head: bytes, *, complete: bool = False) -> Detected:
    """What the first bytes of a file say it is.

    ``head`` is the start of the file, up to ``DETECT_BYTES``; the text test needs the whole
    of it and tolerates a multi-byte character cut at its end, unless ``complete`` says that
    ``head`` is the whole file. An empty file is empty text.
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
    encoding = _text_encoding(head, complete)
    if encoding is not None:
        return Detected(TEXT_PLAIN, encoding)
    return Detected(OCTET_STREAM)


def _zip_type(head: bytes) -> str:
    """The OpenDocument type a zip declares in its first entry, else ``application/zip``.

    The value is read at the length the header gives and has to be a valid media type with the
    OpenDocument prefix; a crafted or odd entry is just a zip.
    """
    if len(head) < _ZIP_LOCAL_HEADER.size or not head.startswith(b"PK\x03\x04"):
        return ZIP
    fields = _ZIP_LOCAL_HEADER.unpack_from(head)
    method, size, name_length, extra_length = fields[3], fields[7], fields[9], fields[10]
    name_at = _ZIP_LOCAL_HEADER.size
    value_at = name_at + name_length + extra_length
    if head[name_at : value_at - extra_length] != b"mimetype":
        return ZIP
    if method != _ZIP_STORED or not 0 < size <= _ODF_MAX_VALUE:
        return ZIP
    value = head[value_at : value_at + size]
    if len(value) != size:
        return ZIP
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        return ZIP
    if text.startswith(_ODF_PREFIX) and _MEDIA_TYPE.fullmatch(text):
        return text
    return ZIP


def _text_encoding(head: bytes, complete: bool) -> str | None:
    if head.startswith(codecs.BOM_UTF8):
        return UTF_8_BOM if _decodes(head[3:], "utf-8", complete) else None
    if head.startswith(codecs.BOM_UTF16_LE):
        return UTF_16_LE if _decodes(head[2:], "utf-16-le", complete) else None
    if head.startswith(codecs.BOM_UTF16_BE):
        return UTF_16_BE if _decodes(head[2:], "utf-16-be", complete) else None
    if b"\x00" in head:
        return None
    return UTF_8 if _decodes(head, "utf-8", complete) else None


def _decodes(data: bytes, codec: str, complete: bool) -> bool:
    """Valid in ``codec`` (a character cut off at the end only if not ``complete``), no NUL."""
    decoder = codecs.getincrementaldecoder(codec)()
    try:
        text = decoder.decode(data, final=complete)
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
