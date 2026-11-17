"""Rebuild the golden corpus of the extractors: ``uv run python tests/fixtures/files/generate.py``.

Every file is tiny and made of neutral content: no real documents, no metadata of a camera or
an author, fixed timestamps. The bytes are committed; this script documents how they were made
and rebuilds them byte for byte. ``manifest.json`` lists the files the golden test runs and,
where a file was recorded with a type other than ``media_types.detect`` gives (an explicit
``--media-type``), that type.
"""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import struct
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def pdf(pages: int) -> bytes:
    """A valid PDF of ``pages`` empty pages (xref offsets computed)."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + n} 0 R" for n in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>" for _ in range(pages)]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def sqlite_database() -> bytes:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        PRAGMA page_size = 512;
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, customer TEXT, total REAL, note TEXT, paid INTEGER
        );
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE INDEX orders_customer ON orders (customer);
        CREATE VIEW open_orders AS SELECT id, customer FROM orders WHERE paid = 0;
        INSERT INTO customers VALUES (1, 'Example A');
        INSERT INTO orders VALUES (1, 'Example A', 10.5, NULL, 0);
        PRAGMA user_version = 3;
        """
    )
    data = connection.serialize()
    connection.close()
    return bytes(data)


def zip_archive(entries: list[tuple[str, bytes, int]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = method
            archive.writestr(info, data)
    return out.getvalue()


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "notes.txt": "Example notes.\nSecond line with an umlaut: ü.\n".encode(),
        "notes_bom.txt": b"\xef\xbb\xbf" + b"Example notes with a BOM.\n",
        "notes_utf16.txt": b"\xff\xfe" + "Example notes in UTF-16.\n".encode("utf-16-le"),
        "empty.txt": b"",
        "text_renamed.pdf": b"This is plain text that was renamed to .pdf.\n",
        "binary_renamed.csv": bytes(range(256)) * 2,
        "table.csv": b"id,name,amount\n1,alpha,10\n2,beta,20\n3,gamma,30\n",
        "table_semicolon.csv": b"id;name;amount\n1;alpha;1,5\n2;beta;2,5\n",
        "pixel.png": png(3, 2),
        "pixel_named.jpg": png(1, 1),
        "one_page.pdf": pdf(1),
        "three_pages.pdf": pdf(3),
        "database.sqlite": sqlite_database(),
        "archive.zip": zip_archive([("a.txt", b"a\n", zipfile.ZIP_DEFLATED)]),
        "letter.docx": zip_archive([("word/document.xml", b"<w/>", zipfile.ZIP_DEFLATED)]),
        "text.odt": zip_archive(
            [
                ("mimetype", b"application/vnd.oasis.opendocument.text", zipfile.ZIP_STORED),
                ("content.xml", b"<office/>", zipfile.ZIP_DEFLATED),
            ]
        ),
        "notes_gzip.txt": gzip.compress(b"Example notes.\n", mtime=0),
    }
    return files


MANIFEST_OVERRIDES = {
    # An explicit --media-type that the bytes contradict.
    "pixel_named.jpg": "image/jpeg",
}


def main() -> None:
    files = build()
    for name, data in files.items():
        (HERE / name).write_bytes(data)
    manifest = {
        name: (
            {"recorded_media_type": MANIFEST_OVERRIDES[name]} if name in MANIFEST_OVERRIDES else {}
        )
        for name in sorted(files)
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
