"""The ``image`` extractor: sizes from headers, never a decode (file metadata M14)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from graph_cli.extractors import image
from graph_cli.extractors.model import ExtractionResult, RunInput


def run(tmp_path: Path, data: bytes) -> ExtractionResult:
    path = tmp_path / "image"
    path.write_bytes(data)
    return image.run(RunInput(path, "image/png"))


def sof(marker: int, width: int, height: int) -> bytes:
    return (
        b"\xff" + bytes([marker]) + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    )


def app(size: int) -> bytes:
    return b"\xff\xe1" + struct.pack(">H", size + 2) + b"x" * size


def test_a_progressive_jpeg_after_a_large_exif_segment(tmp_path: Path) -> None:
    data = b"\xff\xd8" + app(60000) + b"\xff\xff\xff" + sof(0xC2, 640, 480) + b"\xff\xd9"
    assert run(tmp_path, data) == ExtractionResult(
        status="ok", values={"width": 640, "height": 480}
    )


@pytest.mark.parametrize("marker", [0xC4, 0xC8, 0xCC])
def test_table_markers_in_the_sof_range_are_not_frames(tmp_path: Path, marker: int) -> None:
    data = b"\xff\xd8\xff" + bytes([marker]) + struct.pack(">H", 4) + b"\x00\x00" + sof(0xC0, 3, 2)
    assert run(tmp_path, data).values == {"width": 3, "height": 2}


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xd8",  # nothing after the start
        b"\xff\xd8\xff\xda\x00\x08",  # the scan starts before any frame header
        b"\xff\xd8\xff\xd9",  # the image ends
        b"\xff\xd8" + sof(0xC0, 0, 5),  # zero width
        b"\xff\xd8" + b"\xff\xe0\x00\x01",  # a segment shorter than its length field
        b"\xff\xd8\xff\xc0\x00\x0b\x08",  # a cut frame header
        b"\x89PNG\r\n\x1a\n",  # no IHDR
        b"GIF89a\x01",  # no screen descriptor
        b"RIFF\x00\x00\x00\x00WEBPJUNKxxxx",  # an unknown chunk
    ],
)
def test_unreadable_headers_are_failed(tmp_path: Path, data: bytes) -> None:
    assert run(tmp_path, data) == ExtractionResult.failed("image.unreadable")


def test_a_webp_with_the_extended_header(tmp_path: Path) -> None:
    chunk = b"\x00\x00\x00\x00" + (100 - 1).to_bytes(3, "little") + (60 - 1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(chunk)) + chunk
    data = b"RIFF" + struct.pack("<I", len(body)) + body
    assert run(tmp_path, data).values == {"width": 100, "height": 60}


def test_a_png_that_claims_zero_pixels_is_failed(tmp_path: Path) -> None:
    ihdr = struct.pack(">II", 0, 5) + b"\x08\x00\x00\x00\x00"
    data = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
    assert run(tmp_path, data) == ExtractionResult.failed("image.unreadable")
