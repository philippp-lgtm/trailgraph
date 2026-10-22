"""Syntax tree of the read-only Cypher subset, as the parser produces it.

Names are kept as written (labels, relationship types, property keys,
functions); ``resolve`` maps them to the model. Every node carries the
position of its first token for error details; positions are left out of
equality so tests compare trees by structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class Pos:
    """1-based line and column of a token; ``offset`` indexes the query text."""

    line: int
    column: int
    offset: int


NO_POS: Final = Pos(0, 0, 0)


# Expressions

# Number literals with a fraction or exponent are exact decimals, as graph_cli
# stores decimal attributes; JSON output shows them as strings.
LiteralValue = None | bool | int | Decimal | str
BinaryOp = Literal[
    "or",
    "xor",
    "and",
    "=",
    "<>",
    "<",
    "<=",
    ">",
    ">=",
    "in",
    "starts_with",
    "ends_with",
    "contains",
    "+",
    "-",
    "*",
    "/",
    "%",
]
UnaryOp = Literal["not", "-", "+"]


@dataclass(frozen=True, slots=True)
class Const:
    value: LiteralValue
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Variable:
    name: str
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Property:
    subject: Expr
    key: str
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class ListExpr:
    items: tuple[Expr, ...]
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class MapExpr:
    entries: tuple[tuple[str, Expr], ...]
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Unary:
    op: UnaryOp
    operand: Expr
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Binary:
    op: BinaryOp
    left: Expr
    right: Expr
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class IsNull:
    operand: Expr
    negated: bool = False  # IS NOT NULL
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """``name`` is lower-cased (Cypher function names are case-insensitive)."""

    name: str
    args: tuple[Expr, ...]
    distinct: bool = False
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class CountStar:
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Case:
    """``subject`` set: simple form (``CASE x WHEN 1 THEN ...``); else generic form."""

    subject: Expr | None
    whens: tuple[tuple[Expr, Expr], ...]
    default: Expr | None = None
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class HasLabels:
    """``n:A:B`` in an expression; ``labels`` as in ``NodePattern``."""

    subject: Expr
    labels: tuple[tuple[str, ...], ...]
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class PatternPredicate:
    """A relationship pattern used as a boolean in an expression (``WHERE (a)-->(b)``)."""

    pattern: PatternPart
    pos: Pos = field(default=NO_POS, compare=False)


Expr = (
    Const
    | Parameter
    | Variable
    | Property
    | ListExpr
    | MapExpr
    | Unary
    | Binary
    | IsNull
    | FunctionCall
    | CountStar
    | Case
    | HasLabels
    | PatternPredicate
)


# Patterns

Direction = Literal["out", "in", "both"]


@dataclass(frozen=True, slots=True)
class NodePattern:
    """``labels`` is a conjunction of alternatives: ``:A|B:C`` is ``((A, B), (C,))``."""

    variable: str | None = None
    labels: tuple[tuple[str, ...], ...] = ()
    properties: MapExpr | None = None
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class RelPattern:
    """``length`` is ``(min, max)`` for a variable-length pattern, else ``None``."""

    variable: str | None = None
    types: tuple[str, ...] = ()  # alternatives; () = any relation
    direction: Direction = "both"
    properties: MapExpr | None = None
    length: tuple[int, int] | None = None
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class PatternPart:
    """A chain: ``nodes[i]`` and ``nodes[i + 1]`` are joined by ``rels[i]``."""

    nodes: tuple[NodePattern, ...]
    rels: tuple[RelPattern, ...] = ()
    pos: Pos = field(default=NO_POS, compare=False)


# Clauses


@dataclass(frozen=True, slots=True)
class ProjectionItem:
    """``text`` is the expression as written: the column name without ``AS``."""

    expr: Expr
    alias: str | None
    text: str
    pos: Pos = field(default=NO_POS, compare=False)

    @property
    def name(self) -> str:
        return self.alias if self.alias is not None else self.text


@dataclass(frozen=True, slots=True)
class SortItem:
    expr: Expr
    descending: bool = False
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Projection:
    items: tuple[ProjectionItem, ...]
    star: bool = False  # ``*``, before the items
    distinct: bool = False
    order: tuple[SortItem, ...] = ()
    skip: Expr | None = None
    limit: Expr | None = None


@dataclass(frozen=True, slots=True)
class Match:
    patterns: tuple[PatternPart, ...]
    where: Expr | None = None
    optional: bool = False
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Unwind:
    expr: Expr
    variable: str
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class With:
    projection: Projection
    where: Expr | None = None
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True, slots=True)
class Return:
    projection: Projection
    pos: Pos = field(default=NO_POS, compare=False)


Clause = Match | Unwind | With | Return


@dataclass(frozen=True, slots=True)
class Query:
    """Reading clauses in order; the last one is always ``Return``."""

    clauses: tuple[Clause, ...]

    @property
    def returns(self) -> Return:
        last = self.clauses[-1]
        assert isinstance(last, Return)
        return last
