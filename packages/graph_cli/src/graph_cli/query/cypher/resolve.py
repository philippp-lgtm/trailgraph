"""Names, fields, parameters and scopes of a parsed query, against the model.

``resolve`` returns the same syntax tree with every label and relationship
type replaced by its CURIE, ``SKIP``/``LIMIT`` replaced by their integers
and ``ORDER BY`` keys that repeat a projected expression replaced by the
column, plus what ``--explain`` shows. It raises:

- ``term.unknown`` (did-you-mean) for a label, relationship type or field
  the model does not have; ``query.ambiguous_term`` for a local name that
  several CURIEs share;
- ``query.unbound_variable``, ``query.parameter_missing``;
- ``query.unsupported`` for functions outside the subset;
- ``query.syntax`` for misplaced aggregates, wrong argument counts,
  variables used as two kinds, duplicate columns, bad ``SKIP``/``LIMIT``.

A label means the class and every subclass (``find --type``). A field is
checked against the types a variable can have; a variable without label
may use any field of the model, a value (from ``WITH`` or ``UNWIND``) any
key.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import SYSTEM_FIELDS
from graph_cli.query.cypher import render
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
    ListComprehension,
    ListExpr,
    MapExpr,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    PatternPredicate,
    Pos,
    Projection,
    ProjectionItem,
    Property,
    Quantifier,
    Query,
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
from graph_cli.query.cypher.errors import (
    ambiguous_term,
    did_you_mean,
    parameter_missing,
    syntax_error,
    unbound_variable,
    unknown_term,
    unsupported,
)
from graph_cli.query.cypher.values import Value, plain_json

AGGREGATES: Final = frozenset({"count", "collect", "min", "max", "sum", "avg"})
# name -> (fewest, most arguments; None = any number)
FUNCTIONS: Final[dict[str, tuple[int, int | None]]] = {
    **dict.fromkeys(AGGREGATES, (1, 1)),
    "labels": (1, 1),
    "type": (1, 1),
    "keys": (1, 1),
    "properties": (1, 1),
    "coalesce": (1, None),
    "size": (1, 1),
    "head": (1, 1),
    "last": (1, 1),
    "tostring": (1, 1),
    "tointeger": (1, 1),
    "tofloat": (1, 1),
    "tolower": (1, 1),
    "toupper": (1, 1),
    "trim": (1, 1),
    "substring": (2, 3),
    "split": (2, 2),
    "replace": (3, 3),
    "abs": (1, 1),
    "ceil": (1, 1),
    "floor": (1, 1),
    "round": (1, 2),
}
_REFUSED_FUNCTIONS: Final = {
    "id": "use n.id, the stable entity id",
    "elementid": "use n.id, the stable entity id",
    "exists": "use n.key IS NOT NULL, or a pattern in WHERE",
}
SYSTEM_BASES: Final = {
    "rev": "integer",
    "tx_seq": "integer",
    "created_at": "datetime",
    "recorded_at": "datetime",
}
EDGE_ID: Final = "edge_id"

# value: anything, maybe a node or relationship; list: a list that may hold
# them; plain: neither (literals, parameters, properties, computed values).
VarKind = Literal["node", "rel", "rels", "value", "list", "plain"]
_ENTITY_KINDS: Final = frozenset({"node", "rel", "value"})


@dataclass(frozen=True, slots=True)
class VarInfo:
    """``types``: the classes a node can have, the relations of a relationship; None = any."""

    kind: VarKind
    types: tuple[str, ...] | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "types": None if self.types is None else list(self.types)}


@dataclass(frozen=True, slots=True)
class FieldUse:
    variable: str | None  # None: an anonymous node or relationship
    field: str
    datatypes: tuple[str, ...]  # base types; several when the variable's types differ
    many: bool

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "variable": self.variable,
            "field": self.field,
            "datatypes": list(self.datatypes),
            "many": self.many,
        }


@dataclass(frozen=True, slots=True)
class ResolvedQuery:
    query: Query
    columns: tuple[str, ...]
    parameters: Mapping[str, Value]  # the ones the query uses
    introduced: tuple[Mapping[str, VarInfo], ...]  # per clause, the variables it binds
    fields: tuple[FieldUse, ...]

    def explain(self) -> dict[str, JsonValue]:
        """``data.plan`` of ``--explain``: the resolved clauses as Cypher, nothing run."""
        return {
            "clauses": [
                {
                    "clause": _clause_name(clause),
                    "text": render.clause(clause),
                    "variables": {k: v.to_json() for k, v in introduced.items()},
                }
                for clause, introduced in zip(self.query.clauses, self.introduced, strict=True)
            ],
            "columns": list(self.columns),
            "parameters": {k: plain_json(v) for k, v in self.parameters.items()},
            "fields": [f.to_json() for f in self.fields],
        }


def _clause_name(clause: Clause) -> str:
    if isinstance(clause, Match):
        return "OPTIONAL MATCH" if clause.optional else "MATCH"
    return type(clause).__name__.upper()


def _local(curie: str) -> str:
    return curie.partition(":")[2]


@dataclass(slots=True)
class _Scope:
    variables: dict[str, VarInfo] = field(default_factory=dict)

    def copy(self) -> _Scope:
        return _Scope(dict(self.variables))


class _Resolver:
    def __init__(self, model: CompiledModel, parameters: Mapping[str, Value]) -> None:
        self.model = model
        self.given = parameters
        self.used: dict[str, Value] = {}
        self.fields: dict[tuple[str | None, str], FieldUse] = {}
        self.class_names = _local_index(model.classes)
        self.relation_names = _local_index(model.relations)

    # Names

    def term(self, kind: str, name: str, pos: Pos) -> str:
        known, index = (
            (self.model.classes, self.class_names)
            if kind == "type"
            else (self.model.relations, self.relation_names)
        )
        if name in known:
            return name
        candidates = index.get(name, ())
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise ambiguous_term(pos, kind, name, candidates)
        is_prefix = any(curie.startswith(f"{name}:") for curie in known)
        raise unknown_term(pos, kind, name, [*known, *index], is_prefix=is_prefix)

    def label_groups(
        self, groups: tuple[tuple[str, ...], ...], pos: Pos
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(self.term("type", label, pos) for label in group) for group in groups)

    def classes_of(self, groups: tuple[tuple[str, ...], ...], pos: Pos) -> tuple[str, ...] | None:
        """The classes carrying every group's label (None: no labels, any class).

        A node has one type and its supertypes, so labels that no class has
        together are refused instead of matching nothing.
        """
        if not groups:
            return None
        found: set[str] | None = None
        for group in groups:
            members = {c for label in group for c in self.model.subtypes(label)}
            found = members if found is None else found & members
        if not found:
            raise unsupported(
                pos,
                "pattern",
                f"labels that no type has together ({render.labels(groups)})",
                "a node has one type and its supertypes; use :A|B for either",
            )
        return tuple(sorted(found))

    # Fields

    def check_field(self, variable: str | None, info: VarInfo, name: str, pos: Pos) -> None:
        if info.kind in ("value", "list", "plain"):
            return
        if info.kind == "rels":
            raise syntax_error(
                pos,
                f"{variable!r} is a list of relationships; it has no property {name!r}",
                found=f"{variable}.{name}",
            )
        found = self.node_field(info, name) if info.kind == "node" else self.rel_field(info, name)
        if found is None:
            choices = self.node_fields(info) if info.kind == "node" else self.rel_fields(info)
            raise unknown_term(pos, "field", name, choices)
        self.fields.setdefault((variable, name), FieldUse(variable, name, *found))

    def node_classes(self, info: VarInfo) -> tuple[str, ...]:
        return tuple(sorted(self.model.classes)) if info.types is None else info.types

    def node_fields(self, info: VarInfo) -> set[str]:
        attributes = {a for c in self.node_classes(info) for a in self.model.classes[c].attributes}
        return attributes | set(SYSTEM_FIELDS)

    def node_field(self, info: VarInfo, name: str) -> tuple[tuple[str, ...], bool] | None:
        if name in SYSTEM_FIELDS:
            return (SYSTEM_BASES.get(name, "string"),), False
        bases: set[str] = set()
        many = False
        for cls in self.node_classes(info):
            attribute = self.model.classes[cls].attributes.get(name)
            if attribute is not None:
                bases.add(self.model.value_base(attribute.kind, attribute.type))
                many = many or attribute.many
        return (tuple(sorted(bases)), many) if bases else None

    def rel_relations(self, info: VarInfo) -> tuple[str, ...]:
        return tuple(sorted(self.model.relations)) if info.types is None else info.types

    def rel_fields(self, info: VarInfo) -> set[str]:
        qualifiers = {
            q for r in self.rel_relations(info) for q in self.model.relations[r].qualifiers
        }
        return qualifiers | {EDGE_ID}

    def rel_field(self, info: VarInfo, name: str) -> tuple[tuple[str, ...], bool] | None:
        if name == EDGE_ID:
            return ("string",), False
        bases: set[str] = set()
        many = False
        for relation in self.rel_relations(info):
            qualifier = self.model.relations[relation].qualifiers.get(name)
            if qualifier is not None:
                bases.add(self.model.value_base(qualifier.kind, qualifier.type))
                many = many or qualifier.many
        return (tuple(sorted(bases)), many) if bases else None

    # Clauses

    def query(self, query: Query) -> ResolvedQuery:
        scope = _Scope()
        clauses: list[Clause] = []
        introduced: list[Mapping[str, VarInfo]] = []
        columns: tuple[str, ...] = ()
        for clause in query.clauses:
            before = dict(scope.variables)
            if isinstance(clause, Match):
                resolved_clause: Clause = self.match(clause, scope)
            elif isinstance(clause, Unwind):
                resolved_clause = self.unwind(clause, scope)
            elif isinstance(clause, With):
                resolved_clause, scope = self.with_(clause, scope)
            else:
                resolved_clause, columns = self.return_(clause, scope)
            clauses.append(resolved_clause)
            introduced.append(
                {k: v for k, v in scope.variables.items() if before.get(k) != v}
                if not isinstance(clause, Return)
                else {}
            )
        return ResolvedQuery(
            Query(tuple(clauses)),
            columns,
            dict(sorted(self.used.items())),
            tuple(introduced),
            tuple(self.fields.values()),
        )

    def match(self, clause: Match, scope: _Scope) -> Match:
        self.clause_rels: set[str] = set()
        outer = scope.copy()
        patterns = tuple(self.pattern(p, scope, outer, introduce=True) for p in clause.patterns)
        where = None if clause.where is None else self.expr(clause.where, scope)
        return replace(clause, patterns=patterns, where=where)

    def unwind(self, clause: Unwind, scope: _Scope) -> Unwind:
        value = self.expr(clause.expr, scope)
        if clause.variable in scope.variables:
            raise syntax_error(
                clause.pos,
                f"variable {clause.variable!r} is already defined",
                found=clause.variable,
            )
        scope.variables[clause.variable] = VarInfo(_element_kind(value, scope))
        return replace(clause, expr=value)

    def with_(self, clause: With, scope: _Scope) -> tuple[With, _Scope]:
        projection, inner, visible = self.projection(clause.projection, scope, "WITH", clause.pos)
        where = None if clause.where is None else self.expr(clause.where, visible)
        return replace(clause, projection=projection, where=where), inner

    def return_(self, clause: Return, scope: _Scope) -> tuple[Return, tuple[str, ...]]:
        projection, inner, _ = self.projection(clause.projection, scope, "RETURN", clause.pos)
        return replace(clause, projection=projection), tuple(inner.variables)

    def projection(
        self, projection: Projection, scope: _Scope, keyword: str, pos: Pos
    ) -> tuple[Projection, _Scope, _Scope]:
        """The resolved projection, the scope after it and the scope its ORDER BY and
        WHERE see: the earlier variables too, unless it aggregates or is DISTINCT."""
        if projection.star and not scope.variables:
            raise syntax_error(pos, f"{keyword} * needs at least one variable in scope", found="*")
        star_names = sorted(scope.variables) if projection.star else []
        items = tuple(
            replace(item, expr=self.expr(item.expr, scope, aggregates=True))
            for item in projection.items
        )
        self.check_grouping(items)
        seen = set(star_names)
        for item in items:
            if item.name in seen:
                raise syntax_error(item.pos, f"column {item.name!r} appears twice", found=item.name)
            seen.add(item.name)
        aggregating = any(_has_aggregate(item.expr) for item in items)
        inner = _Scope({n: scope.variables[n] for n in star_names})
        for item in items:
            inner.variables[item.name] = (
                scope.variables[item.expr.name]
                if isinstance(item.expr, Variable)
                else VarInfo(_kind_of(item.expr, scope))
            )
        visible = (
            inner
            if aggregating or projection.distinct
            else _Scope({**scope.variables, **inner.variables})
        )
        order = tuple(self.sort_item(s, projection.items, visible) for s in projection.order)
        resolved = replace(
            projection,
            items=items,
            order=order,
            skip=self.count(projection.skip, "SKIP"),
            limit=self.count(projection.limit, "LIMIT"),
        )
        return resolved, inner, visible

    def check_grouping(self, items: tuple[ProjectionItem, ...]) -> None:
        """Outside its aggregates an item may use only grouping keys (other items)."""
        keys = [item.expr for item in items if not _has_aggregate(item.expr)]
        for item in items:
            if not _has_aggregate(item.expr):
                continue
            loose = list(_uncovered(item.expr, keys))
            if loose:
                raise unsupported(
                    item.pos,
                    "expression",
                    f"a value next to an aggregate that is not a grouping key ({item.text})",
                    f"project {render.expr(loose[0])} as its own column",
                )

    def sort_item(
        self, item: SortItem, items: tuple[ProjectionItem, ...], scope: _Scope
    ) -> SortItem:
        """Parts that repeat a projected expression (as written) or name a column sort
        by that column; the rest is resolved in ``scope``."""
        columns = [(projected.expr, projected.name) for projected in items]
        columns += [(Variable(projected.name), projected.name) for projected in items]
        if item.expr not in [c for c, _ in columns] and _has_aggregate(item.expr):
            # next to an aggregate only simple keys stand for their column
            columns = [(c, n) for c, n in columns if is_aggregate(c) or _simple(c)]
        return replace(item, expr=self.expr(_to_columns(item.expr, columns), scope))

    def count(self, value: Expr | None, keyword: str) -> Expr | None:
        if value is None:
            return None
        number: object
        if isinstance(value, Const):
            number = value.value
        elif (
            isinstance(value, Unary)
            and value.op == "-"
            and isinstance(value.operand, Const)
            and type(value.operand.value) is int
        ):
            number = -value.operand.value
        elif isinstance(value, Parameter):
            number = self.parameter(value)
        else:
            raise syntax_error(
                value.pos,
                f"{keyword} takes an integer or a parameter",
                found=render.expr(value),
            )
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise syntax_error(
                value.pos,
                f"{keyword} takes a non-negative integer, got {render.const(number)}",
                found=render.const(number),
            )
        return Const(number, value.pos)

    # Patterns

    def pattern(
        self, part: PatternPart, scope: _Scope, outer: _Scope, *, introduce: bool
    ) -> PatternPart:
        nodes = tuple(self.node(n, scope, outer, introduce) for n in part.nodes)
        rels = tuple(self.rel(r, scope, outer, introduce) for r in part.rels)
        return replace(part, nodes=nodes, rels=rels)

    def bind(
        self, variable: str | None, info: VarInfo, scope: _Scope, pos: Pos, introduce: bool
    ) -> VarInfo:
        if variable is None:
            return info
        known = scope.variables.get(variable)
        if known is None:
            if not introduce:
                raise unbound_variable(
                    pos,
                    variable,
                    f"a pattern in an expression cannot introduce variable {variable!r}; "
                    "match it first",
                )
            scope.variables[variable] = info
            return info
        if info.kind == "rels":
            raise unsupported(
                pos,
                "pattern",
                f"reusing the variable-length relationship variable {variable!r}",
            )
        if known.kind == "value":  # from WITH or UNWIND: checked when matched
            scope.variables[variable] = info
            return info
        if known.kind != info.kind:
            raise syntax_error(
                pos,
                f"variable {variable!r} is a {_KIND_WORDS[known.kind]}, "
                f"used here as a {_KIND_WORDS[info.kind]}",
                found=variable,
            )
        merged = _narrow(known.types, info.types)
        scope.variables[variable] = VarInfo(known.kind, merged)
        return scope.variables[variable]

    def node(self, n: NodePattern, scope: _Scope, outer: _Scope, introduce: bool) -> NodePattern:
        labels = self.label_groups(n.labels, n.pos)
        info = self.bind(
            n.variable, VarInfo("node", self.classes_of(labels, n.pos)), scope, n.pos, introduce
        )
        properties = self.properties(n.properties, n.variable, info, outer)
        return replace(n, labels=labels, properties=properties)

    def rel(self, r: RelPattern, scope: _Scope, outer: _Scope, introduce: bool) -> RelPattern:
        types = tuple(self.term("relation", t, r.pos) for t in r.types)
        if introduce and r.variable is not None:
            if r.variable in self.clause_rels:
                raise syntax_error(
                    r.pos,
                    f"relationship {r.variable!r} appears twice in one MATCH; "
                    "a relationship matches at most once per MATCH",
                    found=r.variable,
                )
            self.clause_rels.add(r.variable)
        kind: VarKind = "rel" if r.length is None else "rels"
        info = self.bind(r.variable, VarInfo(kind, types or None), scope, r.pos, introduce)
        checked = VarInfo("rel", info.types)
        properties = self.properties(r.properties, r.variable, checked, outer)
        return replace(r, types=types, properties=properties)

    def properties(
        self, properties: MapExpr | None, variable: str | None, info: VarInfo, outer: _Scope
    ) -> MapExpr | None:
        if properties is None:
            return None
        for name, _ in properties.entries:
            self.check_field(variable, info, name, properties.pos)
        entries = tuple((k, self.expr(v, outer)) for k, v in properties.entries)
        return replace(properties, entries=entries)

    # Expressions

    def parameter(self, p: Parameter) -> Value:
        if p.name not in self.given:
            raise parameter_missing(p.pos, p.name)
        self.used[p.name] = self.given[p.name]
        return self.given[p.name]

    def expr(self, e: Expr, scope: _Scope, *, aggregates: bool = False) -> Expr:
        match e:
            case Const():
                return e
            case Parameter():
                self.parameter(e)
                return e
            case Variable(name):
                if name not in scope.variables:
                    raise unbound_variable(e.pos, name)
                return e
            case Property(Variable(name) as subject, key):
                self.expr(subject, scope)
                self.check_field(name, scope.variables[name], key, e.pos)
                return e
            case Property(subject):
                return replace(e, subject=self.expr(subject, scope, aggregates=aggregates))
            case ListExpr(items):
                return replace(
                    e, items=tuple(self.expr(i, scope, aggregates=aggregates) for i in items)
                )
            case MapExpr(entries):
                return replace(
                    e,
                    entries=tuple(
                        (k, self.expr(v, scope, aggregates=aggregates)) for k, v in entries
                    ),
                )
            case Unary(_, operand):
                return replace(e, operand=self.expr(operand, scope, aggregates=aggregates))
            case Binary(_, left, right):
                return replace(
                    e,
                    left=self.expr(left, scope, aggregates=aggregates),
                    right=self.expr(right, scope, aggregates=aggregates),
                )
            case IsNull(operand):
                return replace(e, operand=self.expr(operand, scope, aggregates=aggregates))
            case CountStar():
                if not aggregates:
                    raise _misplaced_aggregate(e.pos, "count(*)")
                return e
            case FunctionCall():
                return self.function(e, scope, aggregates)
            case Case(subject, whens, default):
                return replace(
                    e,
                    subject=None
                    if subject is None
                    else self.expr(subject, scope, aggregates=aggregates),
                    whens=tuple(
                        (
                            self.expr(w, scope, aggregates=aggregates),
                            self.expr(t, scope, aggregates=aggregates),
                        )
                        for w, t in whens
                    ),
                    default=None
                    if default is None
                    else self.expr(default, scope, aggregates=aggregates),
                )
            case HasLabels(subject, groups):
                resolved = self.expr(subject, scope, aggregates=aggregates)
                if isinstance(resolved, Variable) and scope.variables[resolved.name].kind in (
                    "rel",
                    "rels",
                ):
                    raise syntax_error(
                        e.pos,
                        f"{resolved.name!r} is not a node; only nodes have labels",
                        found=resolved.name,
                    )
                return replace(e, subject=resolved, labels=self.label_groups(groups, e.pos))
            case PatternPredicate(part):
                inner = scope.copy()
                return replace(e, pattern=self.pattern(part, inner, scope, introduce=False))
            case Quantifier(_, variable, source, where):
                listed = self.expr(source, scope, aggregates=aggregates)
                local = self.local_scope(variable, listed, scope, e.pos)
                return replace(e, source=listed, where=self.expr(where, local))
            case ListComprehension(variable, source, where, project):
                listed = self.expr(source, scope, aggregates=aggregates)
                local = self.local_scope(variable, listed, scope, e.pos)
                return replace(
                    e,
                    source=listed,
                    where=None if where is None else self.expr(where, local),
                    project=None if project is None else self.expr(project, local),
                )
            case Subscript(subject, index):
                return replace(
                    e,
                    subject=self.expr(subject, scope, aggregates=aggregates),
                    index=self.expr(index, scope, aggregates=aggregates),
                )
            case Slice(subject, start, end):
                return replace(
                    e,
                    subject=self.expr(subject, scope, aggregates=aggregates),
                    start=None if start is None else self.expr(start, scope, aggregates=aggregates),
                    end=None if end is None else self.expr(end, scope, aggregates=aggregates),
                )
        raise AssertionError(f"unknown expression {e!r}")

    def local_scope(self, variable: str, source: Expr, scope: _Scope, pos: Pos) -> _Scope:
        """The scope inside a list expression: ``variable`` is an element of ``source``;
        aggregates are not allowed there (openCypher)."""
        if variable in scope.variables:
            raise syntax_error(pos, f"variable {variable!r} is already defined", found=variable)
        local = scope.copy()
        local.variables[variable] = VarInfo(_element_kind(source, scope))
        return local

    def function(self, call: FunctionCall, scope: _Scope, aggregates: bool) -> Expr:
        name = call.name
        if name in _REFUSED_FUNCTIONS:
            raise unsupported(call.pos, "function", f"{name}()", _REFUSED_FUNCTIONS[name])
        if name not in FUNCTIONS:
            raise unsupported(
                call.pos,
                "function",
                f"the function {name}()",
                "supported: " + ", ".join(f"{f}()" for f in sorted(FUNCTIONS)),
                suggestions=[str(s) for s in did_you_mean(name, FUNCTIONS)],
            )
        fewest, most = FUNCTIONS[name]
        if len(call.args) < fewest or (most is not None and len(call.args) > most):
            wanted = f"{fewest}" if fewest == most else f"at least {fewest}"
            raise syntax_error(
                call.pos,
                f"{name}() takes {wanted} argument{'' if fewest == 1 else 's'}, "
                f"got {len(call.args)}",
                found=name,
            )
        is_aggregate = name in AGGREGATES
        if call.distinct and not is_aggregate:
            raise syntax_error(call.pos, f"DISTINCT works only in aggregates, not {name}()")
        if is_aggregate and not aggregates:
            raise _misplaced_aggregate(call.pos, f"{name}()")
        if name == "size" and isinstance(call.args[0], PatternPredicate):
            raise syntax_error(
                call.pos,
                "size() needs a list or a string; a pattern here is a true/false test",
                found=name,
            )
        # Aggregates nest in plain functions (size(collect(x))), not in aggregates.
        inner = aggregates and not is_aggregate
        args = tuple(self.expr(a, scope, aggregates=inner) for a in call.args)
        return replace(call, args=args)


_KIND_WORDS: Final = {
    "list": "list",
    "plain": "value",
    "node": "node",
    "rel": "relationship",
    "rels": "list of relationships",
    "value": "value",
}


def _kind_of(e: Expr, scope: _Scope) -> VarKind:
    """What a projected or unwound value can be, as far as the text tells."""
    match e:
        case Variable(name):
            return scope.variables[name].kind
        case Property(Variable(name)) if scope.variables[name].kind in ("node", "rel"):
            return "plain"  # stored properties are JSON
        case Property():
            return "value"  # a key of a map may hold anything
        case FunctionCall("collect", (arg,)):
            return "list" if _kind_of(arg, scope) in _ENTITY_KINDS else "plain"
        case FunctionCall("coalesce" | "min" | "max" | "head" | "last", args):
            return "value" if any(_kind_of(a, scope) != "plain" for a in args) else "plain"
        case Case(_, whens, default):
            branches = [then for _, then in whens] + ([default] if default is not None else [])
            return "value" if any(_kind_of(b, scope) != "plain" for b in branches) else "plain"
        case ListExpr(items):
            return "list" if any(_kind_of(i, scope) != "plain" for i in items) else "plain"
        case Binary("+", left, right):
            sides = (_kind_of(left, scope), _kind_of(right, scope))
            return "list" if any(k != "plain" for k in sides) else "plain"
        case ListComprehension(variable, source, _, project):
            if project is None:
                return "plain" if _kind_of(source, scope) == "plain" else "list"
            local = _Scope({**scope.variables, variable: VarInfo(_element_kind(source, scope))})
            return "plain" if _kind_of(project, local) == "plain" else "list"
        case Subscript(subject):
            return "plain" if _kind_of(subject, scope) == "plain" else "value"
        case Slice(subject):
            return "plain" if _kind_of(subject, scope) == "plain" else "list"
    return "plain"


def _element_kind(source: Expr, scope: _Scope) -> VarKind:
    """What an element of a list can be (``UNWIND``, list expressions)."""
    return "plain" if _kind_of(source, scope) == "plain" else "value"


def _narrow(known: tuple[str, ...] | None, more: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Both constraints; () when they exclude each other (the pattern matches nothing)."""
    if known is None:
        return more
    if more is None:
        return known
    return tuple(t for t in known if t in more)


def cypher_names(curies: Iterable[str]) -> dict[str, str]:
    """How a query writes each term: the local name when no other term has it, else the
    CURIE in backticks (the inverse of the name rule ``resolve`` applies)."""
    return {
        curie: name if name != curie else render.name(curie)
        for curie, name in short_names(curies).items()
    }


def short_names(curies: Iterable[str]) -> dict[str, str]:
    """Each term's name as ``labels()`` and ``type()`` return it: the local name when no
    other term has it, else the CURIE."""
    index = _local_index(dict.fromkeys(curies))
    return {
        curie: local if len(index[local]) == 1 else curie
        for local, found in index.items()
        for curie in found
    }


def _local_index(curies: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for curie in sorted(curies):
        index.setdefault(_local(curie), []).append(curie)
    return {k: tuple(v) for k, v in index.items()}


def _misplaced_aggregate(pos: Pos, name: str) -> GraphCliError:
    return syntax_error(
        pos,
        f"aggregate {name} is allowed only in WITH and RETURN items",
        found=name,
    )


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
            for w, t in whens:
                yield w
                yield t
            if default is not None:
                yield default
        case HasLabels(subject):
            yield subject
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
        case PatternPredicate(part):
            for n in part.nodes:
                if n.variable is not None:
                    yield Variable(n.variable, n.pos)
                if n.properties is not None:
                    yield n.properties
            for r in part.rels:
                if r.variable is not None:
                    yield Variable(r.variable, r.pos)
                if r.properties is not None:
                    yield r.properties


def is_aggregate(e: Expr) -> bool:
    return isinstance(e, CountStar) or (isinstance(e, FunctionCall) and e.name in AGGREGATES)


def aggregates_in(e: Expr) -> list[Expr]:
    """The aggregate calls in ``e`` (never nested, resolve refuses that)."""
    if is_aggregate(e):
        return [e]
    return [found for child in _children(e) for found in aggregates_in(child)]


def _has_aggregate(e: Expr) -> bool:
    return bool(aggregates_in(e))


def _simple(e: Expr) -> bool:
    """A grouping key usable next to an aggregate: a variable or one of its properties."""
    return isinstance(e, Variable) or (isinstance(e, Property) and isinstance(e.subject, Variable))


def _uncovered(e: Expr, keys: list[Expr]) -> Iterator[Expr]:
    """Variables of ``e`` outside aggregates and outside the simple grouping keys ``keys``."""
    if is_aggregate(e) or (_simple(e) and e in keys):
        return
    if isinstance(e, Variable):
        yield e
        return
    if isinstance(e, Quantifier | ListComprehension):  # its variable is bound inside
        yield from _uncovered(e.source, keys)
        for child in list(_children(e))[1:]:
            yield from (v for v in _uncovered(child, keys) if v != Variable(e.variable))
        return
    for child in _children(e):
        yield from _uncovered(child, keys)


def _to_columns(e: Expr, columns: list[tuple[Expr, str]]) -> Expr:
    """``e`` with every part equal to a projected expression replaced by its column."""
    for projected, name in columns:
        if e == projected:
            return Variable(name, e.pos)

    def go(child: Expr) -> Expr:
        return _to_columns(child, columns)

    match e:
        case Property(subject):
            return replace(e, subject=go(subject))
        case ListExpr(items):
            return replace(e, items=tuple(go(i) for i in items))
        case MapExpr(entries):
            return replace(e, entries=tuple((k, go(v)) for k, v in entries))
        case Unary(_, operand):
            return replace(e, operand=go(operand))
        case IsNull(operand):
            return replace(e, operand=go(operand))
        case Binary(_, left, right):
            return replace(e, left=go(left), right=go(right))
        case FunctionCall(_, args):
            return replace(e, args=tuple(go(a) for a in args))
        case Case(subject, whens, default):
            return replace(
                e,
                subject=None if subject is None else go(subject),
                whens=tuple((go(w), go(t)) for w, t in whens),
                default=None if default is None else go(default),
            )
        case HasLabels(subject):
            return replace(e, subject=go(subject))
        case Quantifier(_, _, source) | ListComprehension(_, source):
            return replace(e, source=go(source))  # the inner parts see the local variable
        case Subscript(subject, index):
            return replace(e, subject=go(subject), index=go(index))
        case Slice(subject, start, end):
            return replace(
                e,
                subject=go(subject),
                start=None if start is None else go(start),
                end=None if end is None else go(end),
            )
    return e


def resolve(
    query: Query, model: CompiledModel, parameters: Mapping[str, Value] | None = None
) -> ResolvedQuery:
    return _Resolver(model, parameters or {}).query(query)
