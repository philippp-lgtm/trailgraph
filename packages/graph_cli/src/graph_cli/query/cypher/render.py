"""Syntax trees back to Cypher text, for ``--explain`` and messages.

The text parses to the same tree: names that are not plain words (CURIEs,
reserved words) are backticked, and every binary or unary expression is
parenthesized, so precedence never has to be reconstructed.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Final

from graph_cli.query.cypher.ast import (
    Binary,
    Case,
    Clause,
    Const,
    CountStar,
    Expr,
    FunctionCall,
    HasLabels,
    IsNull,
    ListExpr,
    MapExpr,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    PatternPredicate,
    Projection,
    Property,
    RelPattern,
    Return,
    Unary,
    Unwind,
    Variable,
    With,
)
from graph_cli.query.cypher.parser import RESERVED

_PLAIN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPERATORS: Final = {"starts_with": "STARTS WITH", "ends_with": "ENDS WITH"}


def name(text: str) -> str:
    if _PLAIN.match(text) and text.upper() not in RESERVED:
        return text
    return "`" + text.replace("`", "``") + "`"


def key(text: str) -> str:
    """A property or map key: reserved words are fine there."""
    return text if _PLAIN.match(text) else name(text)


def labels(groups: tuple[tuple[str, ...], ...]) -> str:
    return "".join(":" + "|".join(name(label) for label in group) for group in groups)


def const(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "'" + json.dumps(value, ensure_ascii=False)[1:-1].replace("'", "\\'") + "'"
    if isinstance(value, Decimal):
        text = str(value)
        return text if "." in text or "E" in text else text + ".0"
    return repr(value)


def expr(e: Expr) -> str:
    match e:
        case Const(value):
            return const(value)
        case Parameter(param):
            return "$" + name(param)
        case Variable(variable):
            return name(variable)
        case Property(subject, prop):
            return f"{expr(subject)}.{key(prop)}"
        case ListExpr(items):
            return "[" + ", ".join(expr(i) for i in items) + "]"
        case MapExpr(entries):
            return map_(entries)
        case Unary(op, operand):
            return f"(NOT {expr(operand)})" if op == "not" else f"({op}{expr(operand)})"
        case Binary(op, left, right):
            return f"({expr(left)} {_OPERATORS.get(op, op.upper())} {expr(right)})"
        case IsNull(operand, negated):
            return f"({expr(operand)} IS {'NOT ' if negated else ''}NULL)"
        case FunctionCall(function, args, distinct):
            inner = ", ".join(expr(a) for a in args)
            return f"{function}({'DISTINCT ' if distinct else ''}{inner})"
        case CountStar():
            return "count(*)"
        case Case(subject, whens, default):
            parts = ["CASE"] + ([expr(subject)] if subject is not None else [])
            parts += [f"WHEN {expr(w)} THEN {expr(t)}" for w, t in whens]
            parts += [f"ELSE {expr(default)}"] if default is not None else []
            return " ".join([*parts, "END"])
        case HasLabels(subject, groups):
            return f"({expr(subject)}{labels(groups)})"
        case PatternPredicate(part):
            return pattern(part)
    raise AssertionError(f"unknown expression {e!r}")


def map_(entries: tuple[tuple[str, Expr], ...]) -> str:
    return "{" + ", ".join(f"{key(k)}: {expr(v)}" for k, v in entries) + "}"


def node(n: NodePattern) -> str:
    inner = (name(n.variable) if n.variable else "") + labels(n.labels)
    if n.properties is not None:
        inner += (" " if inner else "") + map_(n.properties.entries)
    return f"({inner})"


def rel(r: RelPattern) -> str:
    inner = name(r.variable) if r.variable else ""
    if r.types:
        inner += ":" + "|".join(name(t) for t in r.types)
    if r.length is not None:
        inner += f"*{r.length[0]}..{r.length[1]}"
    if r.properties is not None:
        inner += (" " if inner else "") + map_(r.properties.entries)
    body = f"-[{inner}]-" if inner else "--"
    return ("<" if r.direction == "in" else "") + body + (">" if r.direction == "out" else "")


def pattern(part: PatternPart) -> str:
    text = node(part.nodes[0])
    for r, n in zip(part.rels, part.nodes[1:], strict=True):
        text += rel(r) + node(n)
    return text


def projection(p: Projection) -> str:
    items = ["*"] if p.star else []
    for item in p.items:
        text = expr(item.expr)
        items.append(f"{text} AS {name(item.name)}" if text != name(item.name) else text)
    text = ("DISTINCT " if p.distinct else "") + ", ".join(items)
    if p.order:
        sorted_by = (expr(s.expr) + (" DESC" if s.descending else "") for s in p.order)
        text += " ORDER BY " + ", ".join(sorted_by)
    if p.skip is not None:
        text += f" SKIP {expr(p.skip)}"
    if p.limit is not None:
        text += f" LIMIT {expr(p.limit)}"
    return text


def clause(c: Clause) -> str:
    match c:
        case Match(patterns, where, optional):
            text = ("OPTIONAL MATCH " if optional else "MATCH ") + ", ".join(
                pattern(p) for p in patterns
            )
            return text + (f" WHERE {expr(where)}" if where is not None else "")
        case Unwind(value, variable):
            return f"UNWIND {expr(value)} AS {name(variable)}"
        case With(p, where):
            text = "WITH " + projection(p)
            return text + (f" WHERE {expr(where)}" if where is not None else "")
        case Return(p):
            return "RETURN " + projection(p)
    raise AssertionError(f"unknown clause {c!r}")
