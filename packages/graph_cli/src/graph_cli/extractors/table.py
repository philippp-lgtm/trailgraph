"""The ``table`` extractor (file metadata M7, M14): dialect and shape of CSV and TSV files.

No signature identifies a delimited table, so it applies to a file that is detected as text
*and* was added as ``text/csv`` or ``text/tab-separated-values``, and the content has to confirm
a dialect (``csv.Sniffer`` on the first 64 KiB, among comma, semicolon, tab and pipe), else the
result is ``failed`` with ``table.no_dialect``. Rows are counted by streaming the whole file under
the process limits. Output depends on the interpreter's ``csv`` module, as the golden corpus
shows; the header decision is the sniffer's heuristic.
"""

from __future__ import annotations

import csv
import os
from typing import Final

from graph_cli.extractors.detect import (
    DETECT_BYTES,
    TEXT_PLAIN,
    UTF_8,
    UTF_8_BOM,
    UTF_16_BE,
    UTF_16_LE,
    detect_content,
)
from graph_cli.extractors.model import ExtractionResult, Extractor, ModuleRef, RunInput
from graph_cli.extractors.text import label
from graph_cli.kernel.canonical import JsonValue

NAME: Final = "table"
VERSION: Final = 1
RECORDED: Final = ("text/csv", "text/tab-separated-values")
PROBLEM_NO_DIALECT: Final = "table.no_dialect"
PROBLEM_UNREADABLE: Final = "table.unreadable"
PROBLEM_ENCODING: Final = "table.encoding"
MAX_COLUMN_NAMES: Final = 200

_DELIMITERS: Final = ",;\t|"
_DELIMITER_NAMES: Final = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
_QUOTE_NAMES: Final = {'"': "double", "'": "single"}
_CODECS: Final = {
    UTF_8: "utf-8",
    UTF_8_BOM: "utf-8-sig",
    UTF_16_LE: "utf-16",
    UTF_16_BE: "utf-16",
}


def accepts(detected: str, recorded: str) -> bool:
    return detected == TEXT_PLAIN and recorded in RECORDED


def run(source: RunInput) -> ExtractionResult:
    with open(source.path, "rb") as handle:
        head = handle.read(DETECT_BYTES)
        size = os.fstat(handle.fileno()).st_size
    detected = detect_content(head, complete=size <= len(head))
    codec = _CODECS.get(detected.text_encoding or "")
    if codec is None:
        return ExtractionResult.failed(PROBLEM_NO_DIALECT)
    try:
        sample = head.decode(codec, errors="ignore")
    except UnicodeError:
        return ExtractionResult.failed(PROBLEM_ENCODING)
    sample = sample.rsplit("\n", 1)[0] if "\n" in sample else sample
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=_DELIMITERS)
        header = sniffer.has_header(sample)
    except csv.Error:
        return ExtractionResult.failed(PROBLEM_NO_DIALECT)
    quote = dialect.quotechar if dialect.quotechar and dialect.quotechar in sample else None
    try:
        rows, first = _scan(source, codec, dialect)
    except UnicodeError:
        return ExtractionResult.failed(PROBLEM_ENCODING)
    except csv.Error:
        return ExtractionResult.failed(PROBLEM_UNREADABLE)
    if first is None:
        return ExtractionResult.failed(PROBLEM_NO_DIALECT)
    values: dict[str, JsonValue] = {
        "delimiter": _DELIMITER_NAMES[dialect.delimiter],
        "quote_char": _QUOTE_NAMES.get(quote or "", "none"),
        "has_header": header,
        "column_count": len(first),
        "row_count": rows - 1 if header else rows,
        "columns_truncated": len(first) > MAX_COLUMN_NAMES,
    }
    if header:
        values["column_names"] = column_names(first)
    return ExtractionResult(status="ok", values=values)


def _scan(source: RunInput, codec: str, dialect: type[csv.Dialect]) -> tuple[int, list[str] | None]:
    """The number of non-empty rows of the whole file and the first one."""
    rows = 0
    first: list[str] | None = None
    with open(source.path, encoding=codec, newline="") as handle:
        for row in csv.reader(handle, dialect):
            if not row:
                continue
            if first is None:
                first = row
            rows += 1
    return rows, first


def column_names(row: list[str]) -> list[JsonValue]:
    """The first ``MAX_COLUMN_NAMES`` names as one printable line each (``extractors.text``: the
    rule of every name that comes from a file); an empty one is ``column_<n>`` (from 1)."""
    return [
        label(name, f"column_{position}")
        for position, name in enumerate(row[:MAX_COLUMN_NAMES], start=1)
    ]


TABLE: Final = Extractor(
    name=NAME,
    version=VERSION,
    module=ModuleRef("files_table", "1.0.0"),
    record_class="files_table:TableMetadata",
    accepts=accepts,
    accepted_types=(TEXT_PLAIN,),
    run=run,
    summary="Delimiter, quoting, header, column and row counts of CSV and TSV files.",
)
