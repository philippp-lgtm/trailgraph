"""Media type table (files slice 1): magic bytes first, then the extension."""

import pytest

from graph_cli.services.media_types import OCTET_STREAM, detect


@pytest.mark.parametrize(
    ("head", "name", "expected"),
    [
        (b"%PDF-1.7\n", "scan.bin", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n\x00\x00", "a.txt", "image/png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "a", "image/jpeg"),
        (b"GIF89a\x01\x00", "a", "image/gif"),
        (b"GIF87a\x01\x00", "a", "image/gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "a", "image/webp"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "a", "image/heic"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00", "a", "image/heif"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00", "a.mp4", OCTET_STREAM),
        (b"hello", "notes.TXT", "text/plain"),
        (b"# Title", "README.md", "text/markdown"),
        (b"a,b", "table.csv", "text/csv"),
        (b"<html>", "page.htm", "text/html"),
        (b"{}", "data.json", "application/json"),
        (
            b"PK\x03\x04",
            "letter.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            b"PK\x03\x04",
            "sheet.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (b"PK\x03\x04", "text.odt", "application/vnd.oasis.opendocument.text"),
        (b"PK\x03\x04", "archive.zip", OCTET_STREAM),
        (b"", "empty", OCTET_STREAM),
        (b"%PDF-", "report 2024.txt", "application/pdf"),
    ],
)
def test_detect(head: bytes, name: str, expected: str) -> None:
    assert detect(head, name) == expected
