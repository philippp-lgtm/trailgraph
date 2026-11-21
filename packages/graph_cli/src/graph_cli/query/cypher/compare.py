"""Where a query compares a value with a field of a node or relationship.

``comparisons`` walks a parsed query and yields one ``Comparison`` per place
that sets a field against a value: ``x.f = $p`` (either side, also ``<>``,
``<``, ``<=``, ``>``, ``>=``), ``x.f IN $list``, ``$p IN x.list_field``,
``x.f STARTS WITH $p`` (``ENDS WITH``, ``CONTAINS``) and the inline map of a
pattern, ``(x:T {f: $p})``. Checks of stored queries use it to see which
fields a parameter meets: a function's parameter types, the text a
``--param`` value is bound as.

The value is a ``Parameter`` by default. With ``literals=True`` it can also be
a ``Const`` or a list of constants (a number with a sign included), so a check
sees ``x.f = 2.1`` and ``x.f = -1`` as well.
Only the shapes above count: a field inside an arithmetic expression or a
function call is not a comparison here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from graph_cli.query.cypher.ast import (
    Binary,
    Const,
    Expr,
    ListExpr,
    MapExpr,
    NodePattern,
    Parameter,
    Property,
    Query,
    Unary,
    Variable,
)
from graph_cli.query.cypher.walk import clause_expressions, clause_patterns

Shape = Literal["value", "in_param", "in_field", "text"]

_COMPARISONS: Final = frozenset({"=", "<>", "<", "<=", ">", ">="})
_TEXT_OPS: Final = frozenset({"starts_with", "ends_with", "contains"})


@dataclass(frozen=True, slots=True)
class Comparison:
    """A value compared with a field.

    ``shape``: ``value`` (the value stands where one field value does),
    ``in_param`` (``field IN value``, the value is a list), ``in_field``
    (``value IN field``, the field is a list), ``text`` (``STARTS WITH``,
    ``ENDS WITH`` or ``CONTAINS`` on the field). ``op`` is the operator as the
    syntax tree names it; an inline map is ``=``. ``node`` is the pattern node an
    inline-map field belongs to (``None`` for other comparisons and for
    relationships), so a caller can resolve an anonymous node (``variable`` is
    ``None`` then).
    """

    value: Parameter | Const | ListExpr
    variable: str | None
    field: str
    shape: Shape
    op: str
    node: NodePattern | None


def comparisons(query: Query, *, literals: bool = False) -> Iterator[Comparison]:
    """Every comparison, clause by clause (a clause's expressions, then its patterns).

    Constants count only when ``literals`` is set.
    """
    for clause in query.clauses:
        for e in clause_expressions(clause):
            if isinstance(e, Binary):
                yield from _binary(e, literals)
        for part in clause_patterns(clause):
            for node in part.nodes:
                yield from _property_map(node.variable, node.properties, node, literals)
            for rel in part.rels:
                yield from _property_map(rel.variable, rel.properties, None, literals)


def _field_of(e: Expr) -> tuple[str, str] | None:
    if isinstance(e, Property) and isinstance(e.subject, Variable):
        return e.subject.name, e.key
    return None


def _constant(e: Expr) -> Const | None:
    """A constant; a number with a sign (``-1``, ``+2.5``) is one too."""
    if isinstance(e, Const):
        return e
    if isinstance(e, Unary) and e.op in ("-", "+") and isinstance(e.operand, Const):
        value = e.operand.value
        if isinstance(value, int | Decimal) and not isinstance(value, bool):
            return Const(-value if e.op == "-" else value, e.pos)
    return None


def _operand(e: Expr, literals: bool) -> Parameter | Const | ListExpr | None:
    if isinstance(e, Parameter):
        return e
    if not literals:
        return None
    if isinstance(e, ListExpr):
        items = [_constant(i) for i in e.items]
        if all(i is not None for i in items):
            return ListExpr(tuple(i for i in items if i is not None), e.pos)
        return None
    return _constant(e)


def _scalar(e: Expr, literals: bool) -> Parameter | Const | None:
    operand = _operand(e, literals)
    return None if isinstance(operand, ListExpr) else operand


def _binary(e: Binary, literals: bool) -> Iterator[Comparison]:
    if e.op in _COMPARISONS:
        for side, other in ((e.left, e.right), (e.right, e.left)):
            target = _field_of(side)
            value = _scalar(other, literals)
            if target is not None and value is not None:
                yield Comparison(value, *target, "value", e.op, None)
    elif e.op == "in":
        target = _field_of(e.left)
        listed = _operand(e.right, literals)
        if target is not None and isinstance(listed, Parameter | ListExpr):
            yield Comparison(listed, *target, "in_param", e.op, None)
        target = _field_of(e.right)
        value = _scalar(e.left, literals)
        if target is not None and value is not None:
            yield Comparison(value, *target, "in_field", e.op, None)
    elif e.op in _TEXT_OPS:
        target = _field_of(e.left)
        value = _scalar(e.right, literals)
        if target is not None and value is not None:
            yield Comparison(value, *target, "text", e.op, None)


def _property_map(
    variable: str | None,
    properties: MapExpr | None,
    node: NodePattern | None,
    literals: bool,
) -> Iterator[Comparison]:
    for key, entry in () if properties is None else properties.entries:
        value = _scalar(entry, literals)
        if value is not None:
            yield Comparison(value, variable, key, "value", "=", node)
