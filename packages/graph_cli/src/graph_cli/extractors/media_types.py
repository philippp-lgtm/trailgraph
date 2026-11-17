"""Media types of stored files from their first bytes and their name (files slice 1).

Deterministic on every machine: a built-in table, never the system's
``mimetypes`` database, which reads files of the host. Magic bytes win over
the extension; anything unknown is ``application/octet-stream``. This is the type a
file is *recorded* with when it is added; ``extractors.detect`` decides the type from
the bytes alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Final

SNIFF_BYTES: Final = 16
OCTET_STREAM: Final = "application/octet-stream"

_MAGIC: Final = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
# ISO base media file brands (bytes 8-12 after "ftyp") of HEIF images.
_HEIC_BRANDS: Final = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"})
_HEIF_BRANDS: Final = frozenset({b"mif1", b"msf1", b"heif"})
_OOXML: Final = "application/vnd.openxmlformats-officedocument"
EXTENSIONS: Final[Mapping[str, str]] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".docx": f"{_OOXML}.wordprocessingml.document",
    ".xlsx": f"{_OOXML}.spreadsheetml.sheet",
    ".pptx": f"{_OOXML}.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
}


def sniff(head: bytes) -> str | None:
    """The media type the first bytes declare, ``None`` when no known signature matches."""
    for magic, media_type in _MAGIC:
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand in _HEIF_BRANDS:
            return "image/heif"
    return None


def detect(head: bytes, file_name: str) -> str:
    """The media type of a file starting with ``head`` (at least ``SNIFF_BYTES`` if longer)."""
    sniffed = sniff(head)
    if sniffed is not None:
        return sniffed
    suffix = PurePosixPath(file_name).suffix.lower()
    return EXTENSIONS.get(suffix, OCTET_STREAM)
