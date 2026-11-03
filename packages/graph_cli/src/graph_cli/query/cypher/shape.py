"""The canonical shape of a resolved query: its structure with the data left out.

The access log (contract B5.10, addendum) records one shape per ``query`` call
so that T3 stage 2 can mine which questions recur across sessions. Literals
and ``--param`` values are data and must never reach the log, so the shape
strips them; what stays are model terms (types, relations and property keys as
CURIEs, so ``Topic`` and ``vt:Topic`` agree), functions and operators.

- Variables become ``v1, v2, ...`` and projection aliases ``c1, c2, ...`` in
  order of first appearance; the names and column texts as written are gone.
- A constant becomes a typed placeholder: ``$s`` (string), ``$n`` (number,
  including decimals and negated numbers), ``$b`` (boolean). ``null`` stays: it
  is no data. A list of constants is ``$list`` whatever its length; a list
  with computed items keeps its structure; the keys of a map value (not of a
  pattern's property map) become ``k1, k2, ...`` and a property key that is no
  field of the model (a key into such a map) ``p1, p2, ...``. A ``--param`` is
  the placeholder of the value it was given, so ``{alias: $t}`` and
  ``{alias: 'X'}`` agree.
- ``SKIP`` and ``LIMIT`` are dropped.

The text is the clauses rendered one per line; ``hash`` covers the full text.
Above ``SHAPE_MAX_BYTES`` the text is withheld (``None``) and only the hash is
kept. Pure: the resolved tree is all it needs.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

from graph_cli.kernel.canonical import hash_bytes
from graph_cli.query.cypher import render
from graph_cli.query.cypher.ast import (
    Binary,
    Case,
    Clause,
    Const,
    Expr,
    FunctionCall,
    HasLabels,
    IsNull,
    ListComprehension,
    ListExpr,
    MapExpr,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    PatternPredicate,
    Projection,
    ProjectionItem,
    Property,
    Quantifier,
    RelPattern,
    Return,
    Slice,
    SortItem,
    Subscript,
    Unary,
    Unwind,
    Variable,
    With,
)
from graph_cli.query.cypher.resolve import ResolvedQuery
from graph_cli.query.cypher.values import Value

SHAPE_MAX_BYTES: Final = 4096


@dataclass(frozen=True, slots=True)
class Shape:
    text: str | None  # None above SHAPE_MAX_BYTES
    hash: str  # of the full text, "sha256:<hex>"


def shape(resolved: ResolvedQuery) -> Shape:
    shaper = _Shaper(resolved.parameters, frozenset(f.field for f in resolved.fields))
    text = "\n".join(render.clause(shaper.clause(c)) for c in resolved.query.clauses)
    data = text.encode("utf-8")
    return Shape(text if len(data) <= SHAPE_MAX_BYTES else None, hash_bytes(data))


def _placeholder(kind: str) -> Parameter:
    return Parameter(kind)


def _value_kind(value: Value) -> str | None:
    """The placeholder kind of a data value; None for null (kept as is)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "b"
    if isinstance(value, int | Decimal):
        return "n"
    if isinstance(value, str):
        return "s"
    if isinstance(value, list):
        return "list"
    return "map"


class _Shaper:
    def __init__(self, parameters: Mapping[str, Value], fields: frozenset[str]) -> None:
        self.parameters = parameters
        self.fields = fields  # the field names the resolver checked against the model
        self.keys: dict[str, str] = {}  # any other property key -> ``p1, p2, ...``
        self.env: dict[str, str] = {}  # original name -> renamed, in the current scope
        self.variables = 0
        self.columns = 0

    def fresh_variable(self) -> str:
        self.variables += 1
        return f"v{self.variables}"

    def fresh_column(self) -> str:
        self.columns += 1
        return f"c{self.columns}"

    def lookup(self, name: str) -> str:
        if name not in self.env:  # the resolver rejects unbound names; stay total anyway
            self.env[name] = self.fresh_variable()
        return self.env[name]

    def bind(self, name: str | None) -> str | None:
        """Rename a variable a pattern or UNWIND mentions; the first mention introduces it."""
        return None if name is None else self.lookup(name)

    # Clauses

    def clause(self, c: Clause) -> Clause:
        match c:
            case Match(patterns, where):
                new_patterns = tuple(self.part(p) for p in patterns)
                return replace(c, patterns=new_patterns, where=self.opt(where))
            case Unwind(value, variable):
                source = self.expr(value)
                self.env[variable] = self.fresh_variable()
                return Unwind(source, self.env[variable])
            case With(projection, where):
                new_projection, scope = self.projection(projection)
                # WHERE sees the projected names and the incoming variables
                self.env = {**self.env, **scope}
                new_where = self.opt(where)
                self.env = scope
                return With(new_projection, new_where)
            case Return(projection):
                return Return(self.projection(projection)[0])
        raise AssertionError(f"unknown clause {c!r}")

    def projection(self, p: Projection) -> tuple[Projection, dict[str, str]]:
        """The renamed projection and the scope after it."""
        items: list[ProjectionItem] = []
        after: dict[str, str] = dict(self.env) if p.star else {}
        for item in p.items:
            expression = self.expr(item.expr)
            if isinstance(item.expr, Variable) and item.alias in (None, item.expr.name):
                name = self.lookup(item.expr.name)  # a plain passthrough keeps its variable
                after[item.name] = name
            else:
                name = self.fresh_column()
                if item.alias is not None:
                    after[item.alias] = name
            items.append(ProjectionItem(expression, name, name))
        # ORDER BY sees the projected names and the incoming variables
        before, self.env = self.env, {**self.env, **after}
        order = tuple(SortItem(self.expr(s.expr), s.descending, s.pos) for s in p.order)
        self.env = before
        projection = Projection(tuple(items), p.star, p.distinct, order, None, None)
        return projection, {**after}

    def opt(self, e: Expr | None) -> Expr | None:
        return None if e is None else self.expr(e)

    # Patterns

    def part(self, p: PatternPart) -> PatternPart:
        nodes = [self.node(p.nodes[0])]
        rels: list[RelPattern] = []
        for r, n in zip(p.rels, p.nodes[1:], strict=True):  # textual order
            rels.append(self.rel(r))
            nodes.append(self.node(n))
        return replace(p, nodes=tuple(nodes), rels=tuple(rels))

    def node(self, n: NodePattern) -> NodePattern:
        variable = self.bind(n.variable)
        return replace(n, variable=variable, properties=self.map_(n.properties))

    def rel(self, r: RelPattern) -> RelPattern:
        variable = self.bind(r.variable)
        return replace(r, variable=variable, properties=self.map_(r.properties))

    def map_(self, m: MapExpr | None) -> MapExpr | None:
        if m is None:
            return None
        return replace(m, entries=tuple((k, self.expr(v)) for k, v in m.entries))

    # Expressions

    def constant(self, e: Expr) -> bool:
        match e:
            case Const() | Parameter():
                return True
            case Unary("-" | "+", Const(value)):
                return isinstance(value, int | Decimal) and not isinstance(value, bool)
            case ListExpr(items):
                return all(self.constant(i) for i in items)
        return False

    def expr(self, e: Expr) -> Expr:
        if self.constant(e):
            return self.literal(e)
        match e:
            case Variable(name):
                return replace(e, name=self.lookup(name))
            case Property(subject, key):
                if key not in self.fields:  # a key into a map value: written by the caller
                    key = self.keys.setdefault(key, f"p{len(self.keys) + 1}")
                return replace(e, subject=self.expr(subject), key=key)
            case ListExpr(items):
                return replace(e, items=tuple(self.expr(i) for i in items))
            case MapExpr(entries):
                # keys of a map value are written by the caller like aliases, not model terms
                renamed = tuple((f"k{i}", self.expr(v)) for i, (_, v) in enumerate(entries, 1))
                return replace(e, entries=renamed)
            case Unary(_, operand):
                return replace(e, operand=self.expr(operand))
            case Binary(_, left, right):
                return replace(e, left=self.expr(left), right=self.expr(right))
            case IsNull(operand):
                return replace(e, operand=self.expr(operand))
            case FunctionCall(_, args):
                return replace(e, args=tuple(self.expr(a) for a in args))
            case Case(subject, whens, default):
                return replace(
                    e,
                    subject=self.opt(subject),
                    whens=tuple((self.expr(w), self.expr(t)) for w, t in whens),
                    default=self.opt(default),
                )
            case HasLabels(subject):
                return replace(e, subject=self.expr(subject))
            case PatternPredicate(part):
                return replace(e, pattern=self.part(part))
            case Quantifier(_, variable, source, where):
                new_source = self.expr(source)
                with self.local(variable) as name:
                    return replace(e, variable=name, source=new_source, where=self.expr(where))
            case ListComprehension(variable, source, where, project):
                new_source = self.expr(source)
                with self.local(variable) as name:
                    return replace(
                        e,
                        variable=name,
                        source=new_source,
                        where=self.opt(where),
                        project=self.opt(project),
                    )
            case Subscript(subject, index):
                return replace(e, subject=self.expr(subject), index=self.expr(index))
            case Slice(subject, start, end):
                return replace(
                    e, subject=self.expr(subject), start=self.opt(start), end=self.opt(end)
                )
        return e  # CountStar

    @contextmanager
    def local(self, variable: str) -> Iterator[str]:
        """Rename a quantifier or comprehension variable for the length of the block."""
        name = self.fresh_variable()
        outer = self.env.get(variable)
        self.env[variable] = name
        try:
            yield name
        finally:
            if outer is None:
                del self.env[variable]
            else:
                self.env[variable] = outer

    def literal(self, e: Expr) -> Expr:
        """The placeholder of a constant expression (see ``constant``)."""
        match e:
            case Const(value):
                kind = _value_kind(value)
                return e if kind is None else _placeholder(kind)
            case Parameter(name):
                kind = _value_kind(self.parameters.get(name))
                return Const(None) if kind is None else _placeholder(kind)
            case Unary():
                return _placeholder("n")
        return _placeholder("list")
