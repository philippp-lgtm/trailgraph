"""Errors of the Cypher subset, with the position in the query text.

Every error names line and column; ``query.syntax`` adds what was found and
what was expected, ``query.unsupported`` the kind of construct, the construct
and, where one exists, the alternative in Cypher terms (no CLI syntax; the
hint of kind ``write`` points to ``apply``).
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
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


def did_you_mean(name: str, choices: Iterable[str]) -> list[JsonValue]:
    return list(difflib.get_close_matches(name, sorted(set(choices)), n=5))


def unsupported(
    pos: Pos,
    kind: UnsupportedKind,
    construct: str,
    alternative: str | None = None,
    *,
    suggestions: Iterable[str] = (),
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
            **({"did_you_mean": list(suggestions)} if suggestions else {}),
        },
    )


def unknown_term(pos: Pos, kind: str, name: str, choices: Iterable[str]) -> GraphCliError:
    """``term.unknown`` as ``find`` raises it, plus the position."""
    return GraphCliError(
        "term.unknown",
        f"line {pos.line}, column {pos.column}: unknown {kind} {name!r}",
        details={
            "kind": kind,
            "term": name,
            "did_you_mean": did_you_mean(name, choices),
            **_where(pos),
        },
    )


def ambiguous_term(pos: Pos, kind: str, name: str, candidates: Sequence[str]) -> GraphCliError:
    listed = ", ".join(candidates)
    return GraphCliError(
        "query.ambiguous_term",
        f"line {pos.line}, column {pos.column}: {kind} {name!r} matches {listed}; "
        f"write one of them in backticks, e.g. `{candidates[0]}`",
        details={"kind": kind, "term": name, "candidates": list(candidates), **_where(pos)},
    )


def unbound_variable(pos: Pos, name: str, message: str | None = None) -> GraphCliError:
    return GraphCliError(
        "query.unbound_variable",
        f"line {pos.line}, column {pos.column}: {message or f'variable {name!r} is not defined'}",
        details={"name": name, **_where(pos)},
    )


def parameter_missing(pos: Pos, name: str) -> GraphCliError:
    return GraphCliError(
        "query.parameter_missing",
        f"line {pos.line}, column {pos.column}: parameter ${name} has no value",
        details={"name": name, **_where(pos)},
    )


def invalid_value(pos: Pos, message: str) -> GraphCliError:
    """A runtime error: wrong operand types, division by zero, integer overflow."""
    return GraphCliError(
        "query.invalid_value",
        f"line {pos.line}, column {pos.column}: {message}",
        details=_where(pos),
    )


def too_large(pos: Pos, clause: str, budget: int) -> GraphCliError:
    return GraphCliError(
        "query.too_large",
        f"line {pos.line}, column {pos.column}: {clause} goes over the budget of {budget} rows; "
        "add labels, a property map or a LIMIT",
        details={"clause": clause, "budget": budget, **_where(pos)},
    )
