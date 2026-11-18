"""Type detection from the bytes and the compatibility with the recorded type (M5)."""

from __future__ import annotations

import struct
from importlib import resources

import pytest
import yaml

from graph_cli.extractors.detect import (
    DETECT_BYTES,
    GZIP,
    MEDIA_TYPE_PATTERN,
    SQLITE,
    ZIP,
    Detected,
    compatible,
    detect_content,
)
from graph_cli.extractors.media_types import OCTET_STREAM


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"%PDF-1.7\n%", Detected("application/pdf")),
        (b"\x89PNG\r\n\x1a\n\x00", Detected("image/png")),
        (b"\xff\xd8\xff\xe0", Detected("image/jpeg")),
        (b"GIF89a\x01", Detected("image/gif")),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", Detected("image/webp")),
        (b"SQLite format 3\x00\x04\x00", Detected(SQLITE)),
        (b"PK\x03\x04\x14\x00", Detected(ZIP)),
        (b"PK\x05\x06" + b"\x00" * 18, Detected(ZIP)),
        (b"\x1f\x8b\x08\x00", Detected(GZIP)),
        (b"plain text\n", Detected("text/plain", "utf8")),
        ("Umlaut ü\n".encode(), Detected("text/plain", "utf8")),
        (b"\xef\xbb\xbfwith bom", Detected("text/plain", "utf8_bom")),
        (b"\xff\xfe" + "ab".encode("utf-16-le"), Detected("text/plain", "utf16le")),
        (b"\xfe\xff" + "ab".encode("utf-16-be"), Detected("text/plain", "utf16be")),
        (b"", Detected("text/plain", "utf8")),
        (b"a\x00b", Detected(OCTET_STREAM)),
        (b"\x80\x81\x82 not utf-8", Detected(OCTET_STREAM)),
        (b"\x00\x00\x00\x18ftypisom\x00\x00", Detected(OCTET_STREAM)),
    ],
)
def test_detect_content(head: bytes, expected: Detected) -> None:
    assert detect_content(head) == expected


def test_a_signature_wins_over_the_text_test() -> None:
    assert detect_content(b"%PDF-1.4 this is also valid text").media_type == "application/pdf"


def _zip_entry(
    name: bytes, value: bytes, *, method: int = 0, size: int | None = None, extra: bytes = b""
) -> bytes:
    """A local file header and data of one zip entry, as the zip writers produce them."""
    stored = len(value) if size is None else size
    header = struct.pack(
        "<4sHHHHHIIIHH",
        b"PK\x03\x04",
        20,
        0,
        method,
        0,
        0,
        0,
        stored,
        stored,
        len(name),
        len(extra),
    )
    return header + name + extra + value


ODF_TEXT = b"application/vnd.oasis.opendocument.text"


def test_opendocument_is_told_from_a_plain_zip() -> None:
    head = _zip_entry(b"mimetype", ODF_TEXT) + b"PK\x03\x04"
    assert detect_content(head).media_type == "application/vnd.oasis.opendocument.text"
    assert detect_content(_zip_entry(b"other.txt", b"hello")).media_type == ZIP


def test_opendocument_reads_the_length_and_the_extra_field_from_the_header() -> None:
    head = (
        _zip_entry(b"mimetype", ODF_TEXT, extra=b"\x00\x00\x04\x00abcd") + b"content.xml<office/>"
    )
    assert detect_content(head).media_type == "application/vnd.oasis.opendocument.text"
    # A longer type follows its own length, not the next "PK".
    long = b"application/vnd.oasis.opendocument.text-template"
    assert detect_content(_zip_entry(b"mimetype", long)).media_type == long.decode()


@pytest.mark.parametrize(
    "head",
    [
        # a value with blank, newline and NULs after the prefix
        _zip_entry(b"mimetype", ODF_TEXT + b" X\n\x00\x00"),
        # stored text directly followed by other bytes: not a valid media type
        _zip_entry(b"mimetype", ODF_TEXT + b"content.xml<office/>"),
        # compressed entry, no usable length, a length beyond the head, a non-ASCII value
        _zip_entry(b"mimetype", ODF_TEXT, method=8),
        _zip_entry(b"mimetype", ODF_TEXT, size=0),
        _zip_entry(b"mimetype", ODF_TEXT, size=4000),
        _zip_entry(b"mimetype", ODF_TEXT + "\u00e4".encode()),
        _zip_entry(b"mimetype", ODF_TEXT.upper()),
        b"PK\x03\x04" + b"\x00" * 10,
    ],
)
def test_a_crafted_opendocument_head_is_just_a_zip(head: bytes) -> None:
    assert detect_content(head).media_type == ZIP


def test_the_media_type_pattern_is_the_one_of_the_files_module() -> None:
    module = yaml.safe_load(
        (resources.files("graph_cli.ontology_layer") / "modules" / "files.yaml").read_text()
    )
    assert module["datatypes"]["MediaType"]["pattern"] == MEDIA_TYPE_PATTERN


def test_a_file_that_ends_in_a_cut_character_is_not_text() -> None:
    cut = "\u00fc".encode()[:1]
    assert detect_content(b"abc" + cut).media_type == "text/plain"
    assert detect_content(b"abc" + cut, complete=True).media_type == OCTET_STREAM


def test_a_multibyte_character_cut_at_the_end_is_still_text() -> None:
    text = ("a" * (DETECT_BYTES - 1) + "\u00fc").encode()  # the two bytes straddle the limit
    assert len(text) == DETECT_BYTES + 1
    assert detect_content(text[:DETECT_BYTES]) == Detected("text/plain", "utf8")


def test_only_the_first_bytes_count() -> None:
    assert detect_content(b"text" * DETECT_BYTES + b"\x00\x00").media_type == "text/plain"


@pytest.mark.parametrize(
    ("recorded", "detected", "expected"),
    [
        ("application/pdf", "application/pdf", True),
        (OCTET_STREAM, "application/pdf", True),
        (OCTET_STREAM, "text/plain", True),
        ("text/csv", "text/plain", True),
        ("text/markdown", "text/plain", True),
        ("application/json", "text/plain", True),
        ("application/xml", "text/plain", True),
        ("application/ld+json", "text/plain", True),
        ("text/csv", OCTET_STREAM, False),
        ("text/plain", GZIP, False),
        ("application/pdf", "text/plain", False),
        ("image/jpeg", "image/png", False),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ZIP, True),
        ("application/vnd.oasis.opendocument.text", ZIP, True),
        (
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.text",
            True,
        ),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/plain", False),
        ("application/pdf", ZIP, False),
        ("application/zip", ZIP, True),
    ],
)
def test_compatible(recorded: str, detected: str, expected: bool) -> None:
    assert compatible(recorded, detected) is expected
