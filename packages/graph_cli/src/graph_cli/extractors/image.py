"""The ``image`` extractor (file metadata M14): pixel size of PNG, JPEG, GIF and WebP.

Standard library only, headers only: the size as stored, before any rotation the file asks for
(no EXIF, no camera or location data, which stay out of stage 1). A truncated or inconsistent
header is ``failed`` with ``image.unreadable``.
"""

from __future__ import annotations

import struct
from typing import BinaryIO, Final

from graph_cli.extractors.model import (
    ExtractionResult,
    Extractor,
    ModuleRef,
    RunInput,
    accepts_types,
)

NAME: Final = "image"
VERSION: Final = 1
PROBLEM_UNREADABLE: Final = "image.unreadable"
TYPES: Final = ("image/png", "image/jpeg", "image/gif", "image/webp")

_MAX_JPEG_SEGMENTS: Final = 4096
_JPEG_NO_LENGTH: Final = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})
# SOF0..SOF15 without the tables and the arithmetic-coding marker that share the range
_JPEG_SOF: Final = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})


class _Unreadable(Exception):
    pass


def run(source: RunInput) -> ExtractionResult:
    with open(source.path, "rb") as handle:
        head = handle.read(64)
        try:
            size = _size(head, handle)
        except (_Unreadable, struct.error):
            return ExtractionResult.failed(PROBLEM_UNREADABLE)
    width, height = size
    if width < 1 or height < 1:
        return ExtractionResult.failed(PROBLEM_UNREADABLE)
    return ExtractionResult(status="ok", values={"width": width, "height": height})


def _size(head: bytes, handle: BinaryIO) -> tuple[int, int]:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(head)
    if head.startswith((b"GIF87a", b"GIF89a")):
        return _gif(head)
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return _webp(head)
    if head.startswith(b"\xff\xd8\xff"):
        return _jpeg(handle)
    raise _Unreadable


def _png(head: bytes) -> tuple[int, int]:
    if len(head) < 24 or head[12:16] != b"IHDR":
        raise _Unreadable
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _gif(head: bytes) -> tuple[int, int]:
    if len(head) < 10:
        raise _Unreadable
    width, height = struct.unpack("<HH", head[6:10])
    return width, height


def _webp(head: bytes) -> tuple[int, int]:
    kind, data = head[12:16], head[20:]
    if kind == b"VP8 " and len(data) >= 10 and data[3:6] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[6:10])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L" and len(data) >= 5 and data[0] == 0x2F:
        (bits,) = struct.unpack("<I", data[1:5])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(data) >= 10:
        width = int.from_bytes(data[4:7], "little") + 1
        height = int.from_bytes(data[7:10], "little") + 1
        return width, height
    raise _Unreadable


def _jpeg(handle: BinaryIO) -> tuple[int, int]:
    handle.seek(2)
    for _ in range(_MAX_JPEG_SEGMENTS):
        marker = _next_marker(handle)
        if marker in _JPEG_NO_LENGTH:
            continue
        if marker in (0xD9, 0xDA):  # the image ends or its data starts before any frame header
            raise _Unreadable
        (length,) = struct.unpack(">H", _read(handle, 2))
        if length < 2:
            raise _Unreadable
        if marker in _JPEG_SOF:
            frame = _read(handle, 5)
            height, width = struct.unpack(">xHH", frame)
            return width, height
        handle.seek(length - 2, 1)
    raise _Unreadable


def _next_marker(handle: BinaryIO) -> int:
    byte = _read(handle, 1)
    if byte != b"\xff":
        raise _Unreadable
    while byte == b"\xff":  # fill bytes
        byte = _read(handle, 1)
    return byte[0]


def _read(handle: BinaryIO, count: int) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise _Unreadable
    return data


IMAGE: Final = Extractor(
    name=NAME,
    version=VERSION,
    module=ModuleRef("files_image", "1.0.0"),
    record_class="files_image:ImageMetadata",
    accepts=accepts_types(*TYPES),
    accepted_types=TYPES,
    run=run,
    summary="Width and height in pixels of PNG, JPEG, GIF and WebP images, from the headers.",
)
