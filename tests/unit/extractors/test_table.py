"""The ``table`` extractor: dialect, header, shape (file metadata M7, M14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph_cli.extractors import table
from graph_cli.extractors.model import ExtractionResult, RunInput


def run(tmp_path: Path, data: bytes, recorded: str = "text/csv") -> ExtractionResult:
    path = tmp_path / "table"
    path.write_bytes(data)
    return table.run(RunInput(path, recorded))


def test_it_applies_to_text_added_as_csv_or_tsv_only() -> None:
    assert table.accepts("text/plain", "text/csv")
    assert table.accepts("text/plain", "text/tab-separated-values")
    assert not table.accepts("text/plain", "text/plain")
    assert not table.accepts("text/plain", "application/octet-stream")
    assert not table.accepts("application/octet-stream", "text/csv")  # a binary file named .csv


def test_blank_rows_and_quoted_newlines_do_not_miscount(tmp_path: Path) -> None:
    data = b'id,note\n1,"two\nlines"\n\n2,plain\n\n'
    result = run(tmp_path, data)
    assert result.values["row_count"] == 2
    assert result.values["column_count"] == 2


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [("utf-8-sig", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")],
)
def test_encodings_with_a_byte_order_mark(tmp_path: Path, encoding: str, bom: bytes) -> None:
    text = "id,name\n1,ümlaut\n2,x\n"
    data = text.encode("utf-8-sig") if encoding == "utf-8-sig" else bom + text.encode(encoding)
    result = run(tmp_path, data)
    assert result.status == "ok"
    assert result.values["column_names"] == ["id", "name"]
    assert result.values["row_count"] == 2


def test_column_names_are_one_short_line_each(tmp_path: Path) -> None:
    long = "x" * 300
    header = f'  spaced   name ,"two\nlines",,{long}\n'
    result = run(tmp_path, (header + "1,2,3,4\n5,6,7,8\n").encode())
    assert result.values["has_header"] is True
    assert result.values["column_names"] == ["spaced name", "two lines", "column_3", "x" * 200]


def test_a_file_that_is_text_but_shows_no_dialect_is_failed(tmp_path: Path) -> None:
    assert run(tmp_path, b"just\nwords\n") == ExtractionResult.failed("table.no_dialect")
    assert run(tmp_path, b"") == ExtractionResult.failed("table.no_dialect")


def test_a_late_invalid_byte_is_an_encoding_problem(tmp_path: Path) -> None:
    data = b"id,name\n" + b"1,alpha\n" * 20000 + b"2,\xff\xfe\n"
    assert run(tmp_path, data) == ExtractionResult.failed("table.encoding")


def test_a_field_above_the_parser_limit_is_failed(tmp_path: Path) -> None:
    data = b'id,note\n1,"' + b"x" * 200000 + b'"\n2,y\n'
    assert run(tmp_path, data) == ExtractionResult.failed("table.unreadable")


def test_a_pipe_delimited_file(tmp_path: Path) -> None:
    result = run(tmp_path, b"id|name\n1|a\n2|b\n")
    assert result.values["delimiter"] == "pipe"


def test_column_names_follow_the_shared_label_rule(tmp_path: Path) -> None:
    """Control characters, CR/LF inside a quoted header field, a format character and an overlong
    multi-byte name: one printable line each, cut by characters and bytes, never inside one."""
    emoji = "\U0001f600" * 150
    header = f'"a\r\nb",ctl\x01here,"\u200bzero",{emoji}\n'
    result = run(tmp_path, (header + "1,2,3,4\n5,6,7,8\n").encode())
    assert result.status == "ok" and result.values["has_header"] is True
    names = result.values["column_names"]
    assert isinstance(names, list)
    assert names[:3] == ["a b", "ctl here", "zero"]
    assert names[3] == "\U0001f600" * 100 and len(str(names[3]).encode()) == 400
