"""Generic input files for files tests: a minimal synthetic PDF and a UTF-8 text file.

Neither is a real document. The PDF has one empty page and a valid cross
reference table, a few hundred bytes, built without any dependency.
"""

from pathlib import Path


def synthetic_pdf(marker: str = "") -> bytes:
    """A one-page PDF; ``marker`` goes into a comment, so content differs per marker."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = bytearray(b"%PDF-1.4\n% " + marker.encode() + b"\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_pdf(path: Path, marker: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(synthetic_pdf(marker))
    return path


def write_text(path: Path, text: str = "Plain UTF-8 text: äöü ß €\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
