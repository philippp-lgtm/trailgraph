"""The one rule for names that come from the bytes of a file (file metadata, step 5)."""

from __future__ import annotations

import pytest

from graph_cli.extractors import text
from graph_cli.extractors.text import label


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("  two   spaces  ", "two spaces"),
        ("a\nb", "a b"),
        ("a\r\nb", "a b"),
        ("tab\there", "tab here"),
        ("a\x00b", "a b"),  # NUL
        ("zero\u200bwidth", "zero width"),  # a format character
        ("\u00a0nbsp\u00a0", "nbsp"),
        ("\x1b[31mred", "[31mred"),
        ("", "unnamed"),
        ("\n\t ", "unnamed"),
        ("\x00", "unnamed"),
    ],
)
def test_label_is_one_printable_trimmed_line(raw: str, expected: str) -> None:
    assert label(raw) == expected


def test_the_fallback_is_the_callers() -> None:
    assert label("\n", "column_3") == "column_3"


def test_the_cut_is_by_characters_and_by_bytes_and_never_inside_a_character() -> None:
    assert label("x" * 500) == "x" * text.MAX_CHARACTERS
    emoji = "\U0001f600" * 150  # 150 characters, 600 bytes
    cut = label(emoji)
    assert cut == "\U0001f600" * 100 and len(cut.encode()) == text.MAX_BYTES
    assert label("ä" * 300).encode().decode() == "ä" * 200  # 400 bytes, valid UTF-8


def test_a_cut_does_not_leave_a_blank_at_the_end() -> None:
    assert label("x" * 199 + " y") == "x" * 199


def test_every_label_is_a_valid_key_segment() -> None:
    """What the reconcile planner accepts: printable, no blank at either end."""
    for raw in ("\u200b\u200bx\u200b", "\n" * 3 + "a" + "\x00" * 3, "\U0001f600\x00" * 300):
        found = label(raw)
        assert found.isprintable() and found == found.strip() and found
