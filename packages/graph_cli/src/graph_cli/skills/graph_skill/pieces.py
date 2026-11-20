"""Markdown pieces of the generated skill: code spans, plain text, summaries and tables.

The summary of a deprecated term starts with "Deprecated." unless the
author already wrote it that way.

``md_table`` builds on ``graph_cli.skills.markdown.table``, which refuses cells with
backticks; here ``code`` wraps text in them and the cells get their pipes escaped.
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.kernel.canonical import JsonObject, JsonValue, canonical_bytes
from graph_cli.kernel.json_access import opt_str
from graph_cli.skills.markdown import table


def code(text: str) -> str:
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def codes(names: Sequence[str], joiner: str = ", ") -> str:
    return joiner.join(code(name) for name in names)


def plain(text: str | None) -> str:
    return " ".join((text or "").split())


def term_summary(term: JsonObject) -> str:
    """The summary of a term, marked when the term is deprecated."""
    text = plain(opt_str(term, "summary"))
    if term.get("deprecated") is True and not text.lower().startswith("deprecated"):
        return f"Deprecated. {text}"
    return text


def _row(cells: Sequence[str]) -> list[str]:
    """Table cells: one line each, pipes escaped (also inside code spans, GFM)."""
    return [plain(cell).replace("|", "\\|") for cell in cells]


def md_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return table(header, [_row(row) for row in rows])


def value_code(value: JsonValue) -> str:
    return code(value if isinstance(value, str) else canonical_bytes(value).decode("utf-8"))
