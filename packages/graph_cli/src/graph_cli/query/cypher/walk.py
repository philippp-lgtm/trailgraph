"""Read-only walks over a parsed or resolved query tree.

``parameters`` lists the ``$parameters`` a query uses, so a caller can compare
them with the values it holds before resolving (``resolve`` stops at the first
parameter without a value). ``clause_expressions`` and ``clause_patterns`` give
checks of stored queries every expression and every relationship pattern of one
clause, including patterns used as predicates.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from graph_cli.query.cypher.ast import (
    Binary,
    Case,
    Clause,
    Expr,
    FunctionCall,
    HasLabels,
    IsNull,
    ListComprehension,
    ListExpr,
    MapExpr,
    Match,
    Parameter,
    PatternPart,
    PatternPredicate,
    Pos,
    Projection,
    Property,
    Quantifier,
    Query,
    Return,
    Slice,
    Subscript,
    Unary,
    Unwind,
    With,
)


@dataclass(frozen=True, slots=True)
class ParameterUse:
    """One occurrence of ``$name``; ``paging``: inside a ``SKIP`` or ``LIMIT``."""

    name: str
    pos: Pos
    paging: bool


def parameters(query: Query) -> tuple[ParameterUse, ...]:
    """Every parameter occurrence of the query, in clause order (names repeat)."""
    return tuple(
        ParameterUse(e.name, e.pos, paging)
        for clause in query.clauses
        for root, paging in _roots(clause)
        for e in subexpressions(root)
        if isinstance(e, Parameter)
    )


def clause_expressions(clause: Clause) -> Iterator[Expr]:
    """Every expression node of the clause, outer before inner."""
    for root, _ in _roots(clause):
        yield from subexpressions(root)


def clause_patterns(clause: Clause) -> Iterator[PatternPart]:
    """The patterns of a ``MATCH`` and every pattern used as a predicate in the clause."""
    if isinstance(clause, Match):
        yield from clause.patterns
    for e in clause_expressions(clause):
        if isinstance(e, PatternPredicate):
            yield e.pattern


def subexpressions(e: Expr) -> Iterator[Expr]:
    """``e`` and every expression below it, outer before inner."""
    yield e
    for child in _children(e):
        yield from subexpressions(child)


def _roots(clause: Clause) -> Iterator[tuple[Expr, bool]]:
    """The top-level expressions of a clause; the flag marks ``SKIP`` and ``LIMIT``."""
    match clause:
        case Match(patterns, where):
            for part in patterns:
                yield from ((m, False) for m in _property_maps(part))
            if where is not None:
                yield where, False
        case Unwind(value):
            yield value, False
        case With(projection, where):
            yield from _projection_roots(projection)
            if where is not None:
                yield where, False
        case Return(projection):
            yield from _projection_roots(projection)


def _projection_roots(p: Projection) -> Iterator[tuple[Expr, bool]]:
    for item in p.items:
        yield item.expr, False
    for sort in p.order:
        yield sort.expr, False
    for paging in (p.skip, p.limit):
        if paging is not None:
            yield paging, True


def _property_maps(part: PatternPart) -> Iterator[MapExpr]:
    for node in part.nodes:
        if node.properties is not None:
            yield node.properties
    for rel in part.rels:
        if rel.properties is not None:
            yield rel.properties


def _children(e: Expr) -> Iterator[Expr]:
    match e:
        case Property(subject):
            yield subject
        case ListExpr(items):
            yield from items
        case MapExpr(entries):
            yield from (v for _, v in entries)
        case Unary(_, operand) | IsNull(operand):
            yield operand
        case Binary(_, left, right):
            yield left
            yield right
        case FunctionCall(_, args):
            yield from args
        case Case(subject, whens, default):
            if subject is not None:
                yield subject
            for when, then in whens:
                yield when
                yield then
            if default is not None:
                yield default
        case HasLabels(subject):
            yield subject
        case PatternPredicate(part):
            yield from _property_maps(part)
        case Quantifier(_, _, source, where):
            yield source
            yield where
        case ListComprehension(_, source, where, project):
            yield source
            yield from (p for p in (where, project) if p is not None)
        case Subscript(subject, index):
            yield subject
            yield index
        case Slice(subject, start, end):
            yield subject
            yield from (b for b in (start, end) if b is not None)
