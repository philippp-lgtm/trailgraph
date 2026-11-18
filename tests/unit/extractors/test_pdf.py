"""The ``pdf`` extractor and its parser library (file metadata M10, M14, M15)."""

from __future__ import annotations

import io
from pathlib import Path

import pypdf
import pytest

from graph_cli.extractors import pdf
from graph_cli.extractors.model import ExtractionResult, RunInput


def run(path: Path) -> ExtractionResult:
    return pdf.run(RunInput(path, "application/pdf"))


def test_the_parser_library_is_the_one_the_goldens_were_made_with() -> None:
    """A library upgrade can change output: bump ``VERSION`` and ``TESTED_PYPDF`` together."""
    assert pypdf.__version__ == pdf.TESTED_PYPDF


def test_an_encrypted_file_with_an_empty_user_password_is_read(tmp_path: Path) -> None:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("", "owner password")
    buffer = io.BytesIO()
    writer.write(buffer)
    path = tmp_path / "locked.pdf"
    path.write_bytes(buffer.getvalue())
    result = run(path)
    assert result.status == "ok"
    assert result.values["page_count"] == 2 and result.values["encrypted"] is True


def test_a_file_that_needs_a_password_is_failed_without_a_page_count(tmp_path: Path) -> None:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    path = tmp_path / "locked.pdf"
    path.write_bytes(buffer.getvalue())
    assert run(path) == ExtractionResult.failed("pdf.encrypted")


@pytest.mark.parametrize("content", [b"%PDF-1.7\n", b"%PDF-1.4\nnot a document", b"%PDF-"])
def test_damaged_files_are_failed_not_raised(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(content)
    assert run(path) == ExtractionResult.failed("pdf.unreadable")


def test_the_header_version_is_the_files_own(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "fixtures" / "files" / "one_page.pdf"
    path = tmp_path / "v.pdf"
    path.write_bytes(source.read_bytes().replace(b"%PDF-1.4", b"%PDF-1.7", 1))
    assert run(path).values["pdf_version"] == "1.7"


def test_a_cipher_that_needs_a_missing_package_is_failed_as_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AES needs the optional ``cryptography`` package: the install cannot open the file, on
    every machine alike, so it is a recorded result and not a transient one."""
    from pypdf.errors import DependencyError

    def refuse(self: pypdf.PdfReader, password: str) -> int:
        raise DependencyError("cryptography is required for AES algorithm")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("")
    buffer = io.BytesIO()
    writer.write(buffer)
    path = tmp_path / "aes.pdf"
    path.write_bytes(buffer.getvalue())
    monkeypatch.setattr(pypdf.PdfReader, "decrypt", refuse)
    assert run(path) == ExtractionResult.failed("pdf.encrypted")
