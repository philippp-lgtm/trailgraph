"""The ``pdf`` extractor (file metadata M14): page count, header version, encryption.

Parsed with ``pypdf`` (pure Python), the one third-party parser of stage 1 and imported only
here. A PDF is read for its structure, never for its text. An encrypted file opens only when
its user password is empty; otherwise (or when the cipher needs a package that is not installed)
the result is ``failed`` with ``pdf.encrypted`` and no page count. Output depends on the library:
a ``pypdf`` upgrade bumps ``VERSION`` and ``TESTED_PYPDF`` (a unit test compares it with the
installed one). The import is deferred to ``run``.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from graph_cli.extractors.model import (
    ExtractionResult,
    Extractor,
    ModuleRef,
    RunInput,
    accepts_types,
)
from graph_cli.kernel.canonical import JsonValue

NAME: Final = "pdf"
VERSION: Final = 1
TESTED_PYPDF: Final = "7.1.0"
PROBLEM_UNREADABLE: Final = "pdf.unreadable"
PROBLEM_ENCRYPTED: Final = "pdf.encrypted"

_HEADER: Final = re.compile(rb"%PDF-(\d\.\d)")
_HEADER_BYTES: Final = 1024


def run(source: RunInput) -> ExtractionResult:
    # imported here, not at the top: the parser costs startup time that only a run of this
    # extractor (in its child process) should pay
    import pypdf
    from pypdf.errors import DependencyError, PyPdfError

    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    with open(source.path, "rb") as handle:
        header = handle.read(_HEADER_BYTES)
        handle.seek(0)
        try:
            reader = pypdf.PdfReader(handle, strict=False)
            encrypted = bool(reader.is_encrypted)
            if encrypted and not reader.decrypt(""):
                return ExtractionResult.failed(PROBLEM_ENCRYPTED)
            pages = len(reader.pages)
        except DependencyError:
            # AES needs the optional ``cryptography`` package, which is not installed: the file
            # is encrypted and this install cannot open it, on every machine alike
            return ExtractionResult.failed(PROBLEM_ENCRYPTED)
        except (PyPdfError, ValueError, KeyError, TypeError, IndexError, RecursionError):
            return ExtractionResult.failed(PROBLEM_UNREADABLE)
    values: dict[str, JsonValue] = {"page_count": pages, "encrypted": encrypted}
    found = _HEADER.match(header)
    if found is not None:
        values["pdf_version"] = found[1].decode("ascii")
    return ExtractionResult(status="ok", values=values)


PDF: Final = Extractor(
    name=NAME,
    version=VERSION,
    module=ModuleRef("files_pdf", "1.0.0"),
    record_class="files_pdf:PdfMetadata",
    accepts=accepts_types("application/pdf"),
    accepted_types=("application/pdf",),
    run=run,
    summary="Page count, PDF version of the header and whether the file is encrypted.",
)
