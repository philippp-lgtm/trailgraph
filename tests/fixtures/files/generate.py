"""Rebuild the golden corpus of the extractors: ``uv run python tests/fixtures/files/generate.py``.

Every file is tiny and made of neutral content: no real documents, no metadata of a camera or
an author, fixed timestamps. The bytes are committed; this script documents how they were made
and rebuilds them (``database.sqlite`` can differ between SQLite library versions).
``manifest.json`` lists the files the golden test runs and, where a file was recorded with a type
other than ``media_types.detect`` gives (an explicit ``--media-type``), that type.
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


ID_HEX = "00112233445566778899aabbccddeeff"
# Not a real encryption dictionary: 32 bytes each that no password opens. It is enough for a
# reader to see an encrypted file whose empty password fails.
ENCRYPT = (f"<< /Filter /Standard /V 1 /R 2 /P -4 /O <{'ab' * 32}> /U <{'cd' * 32}> >>").encode()


def pdf(pages: int, *, encrypted: bool = False) -> bytes:
    """A valid PDF of ``pages`` empty pages (xref offsets computed); ``encrypted`` adds a
    standard security handler no password opens."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + n} 0 R" for n in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>" for _ in range(pages)]
    if encrypted:
        objects.append(ENCRYPT)
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
    extra = f" /Encrypt {len(objects)} 0 R /ID [<{ID_HEX}> <{ID_HEX}>]" if encrypted else ""
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{extra} >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
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


def gif(width: int, height: int) -> bytes:
    """A GIF header with a logical screen of the given size and an empty image."""
    return (
        b"GIF89a"
        + struct.pack("<HH", width, height)
        + b"\x00\x00\x00"
        + b"\x2c\x00\x00\x00\x00"
        + struct.pack("<HH", width, height)
        + b"\x00\x02\x01\x00\x00;"
    )


def jpeg_structure(width: int, height: int) -> bytes:
    """The segments of a baseline JPEG up to its first scan: enough for a header reader, not
    a picture a viewer can decode."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + app0 + sof0 + sos + b"\x00\x00" + b"\xff\xd9"


def webp_lossy(width: int, height: int) -> bytes:
    """A RIFF container with a VP8 chunk whose frame header names the size (no picture data)."""
    chunk = b"\x10\x02\x00\x9d\x01\x2a" + struct.pack("<HH", width, height)
    body = b"WEBP" + b"VP8 " + struct.pack("<I", len(chunk)) + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_lossless(width: int, height: int) -> bytes:
    """A RIFF container with a VP8L chunk whose header names the size (no picture data)."""
    bits = (width - 1) | ((height - 1) << 14)
    chunk = b"\x2f" + struct.pack("<I", bits) + b"\x00"
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(chunk)) + chunk + b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body


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
        "encrypted.pdf": pdf(1, encrypted=True),
        "damaged.pdf": b"%PDF-1.4\nthis file starts like a PDF and then stops making sense\n",
        "truncated.png": png(3, 2)[:20],
        "header.jpg": jpeg_structure(4, 3),
        "small.gif": gif(2, 3),
        "lossy.webp": webp_lossy(5, 6),
        "lossless.webp": webp_lossless(7, 8),
        "table.tsv": b"id\tname\tamount\n1\talpha\t10\n2\tbeta\t20\n",
        "table_quoted.csv": (
            b'id,note,amount\n1,"a, with comma",10\n2,"say ""hi""",20\n3,plain,30\n'
        ),
        "table_noheader.csv": b"1,2.5,3\n4,5.5,6\n7,8.5,9\n",
        "table_wide.csv": (
            ",".join(f"c{n}" for n in range(202)) + "\n" + ",".join(["1"] * 202) + "\n"
        ).encode(),
        "single_column.csv": b"alpha\nbeta\ngamma\n",
        "corrupt.sqlite": b"SQLite format 3\x00" + b"\x00" * 40 + b"this is not a database page",
        "database.sqlite": sqlite_database(),
        "names.sqlite": sqlite_names(),
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


HOSTILE_NAMES = (
    "two\nlines",  # a newline: one line, collapsed
    "two lines",  # collapses to the same label as the one above: kept as "two lines (2)"
    "  padded  ",  # blanks at both ends
    "tab\there",
    "zero\u200bwidth",  # a format character: not printable
    "  ",  # nothing printable: "unnamed"
    "n" * 210,  # more than 200 characters: cut to 200
    "\U0001f600" * 101,  # 101 characters but 404 bytes: cut to 400 bytes
)


def sqlite_names() -> bytes:
    """Views whose names an extractor must not take as they are (they become part keys)."""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA page_size = 512")
    for name in HOSTILE_NAMES:  # views have no pages of their own: the file stays small
        quoted = '"' + name.replace('"', '""') + '"'
        connection.execute(f'CREATE VIEW {quoted} AS SELECT 1 AS a, 2 AS "b\nc"')
    connection.execute('CREATE VIEW "v\x01iew" AS SELECT 1 AS one')
    data = connection.serialize()
    connection.close()
    return bytes(data)


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
