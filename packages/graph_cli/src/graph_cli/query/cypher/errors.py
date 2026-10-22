"""Errors of the Cypher subset, with the position in the query text.

Every error names line and column; ``query.syntax`` adds what was found and
what was expected, ``query.unsupported`` the kind of construct, the construct
and, where one exists, the alternative in Cypher terms (no CLI syntax; the
hint of kind ``write`` points to ``apply``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.query.cypher.ast import Pos

UnsupportedKind = Literal["write", "clause", "pattern", "expression", "function"]


def _where(pos: Pos) -> dict[str, JsonValue]:
    return {"line": pos.line, "column": pos.column}


def syntax_error(
    pos: Pos, message: str, *, found: str | None = None, expected: Sequence[str] = ()
) -> GraphCliError:
    details: dict[str, JsonValue] = {**_where(pos), "found": found, "expected": list(expected)}
    return GraphCliError(
        "query.syntax", f"line {pos.line}, column {pos.column}: {message}", details=details
    )


def unsupported(
    pos: Pos, kind: UnsupportedKind, construct: str, alternative: str | None = None
) -> GraphCliError:
    message = f"line {pos.line}, column {pos.column}: not supported: {construct}"
    if alternative is not None:
        message += f"; {alternative}"
    return GraphCliError(
        "query.unsupported",
        message,
        details={
            **_where(pos),
            "kind": kind,
            "construct": construct,
            "alternative": alternative,
        },
    )
