"""Markdown pieces of the generated skills: tables and inline code."""

from __future__ import annotations

from collections.abc import Sequence


def cell(text: str) -> str:
    """One table cell; a line break would end the row, a pipe would split it."""
    if "\n" in text or "`" in text:
        raise ValueError(f"cannot render {text!r} in a table cell")
    return text.replace("|", "\\|")


def code(text: str) -> str:
    return f"`{cell(text)}`"


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Rows are rendered cells (use ``cell`` or ``code``)."""
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines
