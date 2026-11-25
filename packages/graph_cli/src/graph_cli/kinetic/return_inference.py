"""The types of the result columns of a function that its query makes certain (``infer_columns``).

A declared type (``return_types``) types what the query leaves open. The query itself types exactly
these columns, and only where its text makes the type certain:

- ``RETURN v`` of a node variable bound by a ``MATCH`` pattern: an ``entity`` (the lean entity
  object of the reads) of the class the pattern's label names. The class is the label itself, not
  ``VarInfo.types``, which also lists every subclass; several labels or alternatives leave it open.
- ``RETURN v.attr`` of such a variable: the attribute's datatype, a list when the attribute is
  ``many``, ``lang`` for language-tagged text. An attribute is always nullable (a marking can hide
  it from a reader), so is every system field of a node an ``OPTIONAL MATCH`` bound. When the
  classes a variable may have disagree on the attribute, the column stays untyped.
- ``RETURN r.qualifier`` of a relationship variable of a ``MATCH`` pattern (one hop): the
  qualifier's datatype, like an attribute, always nullable.
- ``RETURN v.id`` and ``v.alias``: a ``ref`` (of the label's class), ``v.rev``, ``v.tx_seq`` an
  integer, ``v.created_at`` and ``v.recorded_at`` a datetime, ``v.type``, ``v.record_status``,
  ``v.tx_id``, ``v.content_hash`` and ``v.ontology_version`` text; ``v.valid_from`` and
  ``v.valid_to`` stay untyped.
- ``count(...)``: an integer, never null. ``collect(x)``: a list of what ``x`` is, never null
  (null values are skipped); a list of anything when ``x`` is untyped or a list itself.
- literals.

An ``OPTIONAL MATCH`` never narrows a variable bound before it: such a clause keeps its rows, so
the variable keeps the class and the nullability it had (a name that was a value, from
``WITH ... AS`` or ``UNWIND``, stays untyped), and only a name it binds for the first time is typed,
as nullable. A ``MATCH`` filters, so it narrows.

The resolver, though, does narrow a name by every pattern, and what it reports about the name
afterwards (the classes it may have, the relations of a relationship) derives from that. So a name
that an ``OPTIONAL MATCH`` narrowed (with a label or a relation type) is marked; the mark follows
it through later clauses and ``WITH x AS y`` renames, and the fields of a marked node
(attributes, ``id``, ``alias``, ...) and the qualifiers of a marked relationship stay untyped. The
entity itself keeps the class of its first label, which the typer tracks on its own.

Everything else stays untyped: ``sum`` and ``avg``, ``coalesce``, ``CASE``, arithmetic,
relationships, and values that went through ``WITH ... AS`` (a bare ``WITH v`` keeps ``v``). A
declaration types those.

This module imports the query engine (the resolver and the syntax tree); it is only needed where a
query is resolved, so the modules that read catalogs import ``return_types`` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

from graph_cli.kinetic.return_types import ColumnType, attribute_type
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.ast import (
    Const,
    CountStar,
    Expr,
    FunctionCall,
    Match,
    Property,
    Unwind,
    Variable,
    With,
)
from graph_cli.query.cypher.resolve import ResolvedQuery, VarInfo

# System fields of a node: the kind of their value. ``id`` and ``alias`` are references.
_SYSTEM_KINDS: Final = {
    "id": "ref",
    "alias": "ref",
    "type": "string",
    "record_status": "string",
    "tx_id": "string",
    "content_hash": "string",
    "ontology_version": "string",
    "rev": "integer",
    "tx_seq": "integer",
    "created_at": "datetime",
    "recorded_at": "datetime",
}
_ALWAYS_NULLABLE: Final = frozenset({"alias"})


@dataclass(frozen=True, slots=True)
class _Bound:
    """A node variable of a ``MATCH``: the class its label names (``None``: unknown) and whether
    the first pattern that bound it was an ``OPTIONAL MATCH``."""

    of_class: str | None
    optional: bool


def infer_columns(resolved: ResolvedQuery, model: CompiledModel) -> tuple[ColumnType | None, ...]:
    """The type of each column of ``resolved`` that the query makes certain, ``None`` for the
    others, in column order. A query that returns ``*`` has no columns to type."""
    projection = resolved.query.returns.projection
    if projection.star:
        return ()
    typer = _Typer(resolved, model)
    return tuple(typer.of(item.expr) for item in projection.items)


class _Typer:
    def __init__(self, resolved: ResolvedQuery, model: CompiledModel) -> None:
        self.model = model
        self.scope: dict[str, VarInfo] = {}
        self.bound: dict[str, _Bound] = {}
        self.rels: set[str] = set()  # relationship variables of one hop
        self.live: set[str] = set()  # the names in scope now (``scope`` also holds stale ones)
        self.touched: set[str] = set()  # live names an OPTIONAL MATCH narrowed (see above)
        for clause, introduced in zip(resolved.query.clauses, resolved.introduced, strict=True):
            if isinstance(clause, Match):
                self.match(clause, introduced)
                continue
            self.scope.update(introduced)
            if isinstance(clause, With):
                self.with_(clause)
            elif isinstance(clause, Unwind):
                self.live.add(clause.variable)

    def match(self, clause: Match, introduced: Mapping[str, VarInfo]) -> None:
        """A ``MATCH`` binds and narrows; an ``OPTIONAL MATCH`` only binds the names that were not
        in scope (its rows are kept when the pattern fails, so a name bound before keeps what it
        was: no new class, no narrower relations, no change of nullability)."""
        before = frozenset(self.live)

        def settled(name: str | None) -> bool:
            return clause.optional and name in before

        if clause.optional:
            # only a label or a relation type narrows what the resolver knows about a name
            self.touched.update(
                name
                for part in clause.patterns
                for name in (
                    *(n.variable for n in part.nodes if n.labels),
                    *(r.variable for r in part.rels if r.types),
                )
                if name in before
            )
        for name, info in introduced.items():
            if not settled(name):
                self.scope[name] = info
        for part in clause.patterns:
            for node in part.nodes:
                if node.variable is None or settled(node.variable):
                    continue
                single = len(node.labels) == 1 and len(node.labels[0]) == 1
                of_class = node.labels[0][0] if single else None
                known = self.bound.get(node.variable)
                if known is None:
                    self.bound[node.variable] = _Bound(of_class, clause.optional)
                elif known.of_class is None and of_class is not None:
                    self.bound[node.variable] = _Bound(of_class, known.optional)
            self.rels.update(
                r.variable
                for r in part.rels
                if r.variable and r.length is None and not settled(r.variable)
            )
            self.live.update(n.variable for n in part.nodes if n.variable)
            self.live.update(r.variable for r in part.rels if r.variable)

    def with_(self, clause: With) -> None:
        """A node or relationship variable survives a ``WITH`` that passes it on by its own name;
        one that is renamed or computed on is a value from then on."""
        projection = clause.projection
        kept = dict(self.bound) if projection.star else {}
        rels = set(self.rels) if projection.star else set()
        live = set(self.live) if projection.star else set()
        touched = set(self.touched) if projection.star else set()
        for item in projection.items:
            live.add(item.name)
            if isinstance(item.expr, Variable) and item.expr.name in self.touched:
                touched.add(item.name)
            else:
                touched.discard(item.name)
            if isinstance(item.expr, Variable) and item.expr.name == item.name:
                if item.name in self.bound:
                    kept[item.name] = self.bound[item.name]
                if item.name in self.rels:
                    rels.add(item.name)
            else:
                kept.pop(item.name, None)
                rels.discard(item.name)
        self.bound, self.rels, self.live, self.touched = kept, rels, live, touched

    def of(self, e: Expr) -> ColumnType | None:
        match e:
            case Const(value):
                return _literal(value)
            case CountStar():
                return ColumnType("integer", nullable=False)
            case FunctionCall("count"):
                return ColumnType("integer", nullable=False)
            case FunctionCall("collect", (argument,)):
                return self.collected(self.of(argument))
            case Variable(name) if name in self.bound:
                bound = self.bound[name]
                return ColumnType("entity", of_class=bound.of_class, nullable=bound.optional)
            case Property(Variable(name), key) if name in self.bound:
                return self.field(name, key)
            case Property(Variable(name), key) if name in self.rels:
                return self.qualifier(name, key)
        return None

    @staticmethod
    def collected(element: ColumnType | None) -> ColumnType:
        if element is None or element.many:
            return ColumnType("any", many=True, nullable=False)
        return replace(element, many=True, nullable=False)

    def field(self, name: str, key: str) -> ColumnType | None:
        info = self.scope.get(name)
        if info is None or info.kind != "node" or name in self.touched:
            return None
        bound = self.bound[name]
        system = _SYSTEM_KINDS.get(key)
        if system is not None:
            return ColumnType(
                system,
                of_class=bound.of_class if system == "ref" else None,
                nullable=bound.optional or key in _ALWAYS_NULLABLE,
            )
        classes = tuple(sorted(self.model.classes)) if info.types is None else info.types
        found = {
            attribute_type(self.model, a)
            for c in classes
            if (a := self.model.classes[c].attributes.get(key)) is not None
        }
        return next(iter(found)) if len(found) == 1 else None

    def qualifier(self, name: str, key: str) -> ColumnType | None:
        info = self.scope.get(name)
        if info is None or info.kind != "rel" or name in self.touched:
            return None
        relations = tuple(sorted(self.model.relations)) if info.types is None else info.types
        found = {
            attribute_type(self.model, q)
            for r in relations
            if (q := self.model.relations[r].qualifiers.get(key)) is not None
        }
        return next(iter(found)) if len(found) == 1 else None


def _literal(value: object) -> ColumnType | None:
    kind = (
        "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "decimal"
        if isinstance(value, Decimal)
        else "string"
        if isinstance(value, str)
        else None
    )
    return None if kind is None else ColumnType(kind, nullable=False)
