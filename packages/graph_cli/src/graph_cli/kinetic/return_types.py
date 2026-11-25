"""The types of the result columns of a function: declared, inferred, and the two together.

A function says what its columns hold in two ways. An author may declare a ``type`` per column
(``string integer decimal boolean date datetime enum ref entity lang any``, with ``many``,
``enum`` and ``class``). The query also tells some columns by itself, and ``infer_columns`` types
exactly those, and only where the text of the query makes the type certain:

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
  integer, ``v.created_at`` and ``v.recorded_at`` a datetime, ``v.type``, ``v.record_status`` text.
- ``count(...)``: an integer, never null. ``collect(x)``: a list of what ``x`` is, never null
  (null values are skipped); a list of anything when ``x`` is untyped or a list itself.
- literals.

Everything else stays untyped: ``sum`` and ``avg``, ``coalesce``, ``CASE``, arithmetic,
relationships, and values that went through ``WITH ... AS`` (a bare ``WITH v`` keeps ``v``). A
declaration types those. Inference and declaration must not contradict each other
(``contradicts``); where both exist the inferred nullability and the declared class (for a node of
no known class) are kept (``merge``).

Pure: no storage. The compiled entry stores only what an author declared, so existing catalogs keep
their bytes; the types of the other columns are inferred again from the stored query wherever they
are shown (``compile.column_types``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel, QualifierDef
from graph_cli.ontology_layer.vocabulary import LANG_BASES
from graph_cli.query.cypher.ast import (
    Const,
    CountStar,
    Expr,
    FunctionCall,
    Match,
    Property,
    Variable,
    With,
)
from graph_cli.query.cypher.resolve import ResolvedQuery, VarInfo

COLUMN_KINDS: Final = (
    "string",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "ref",
    "entity",
    "lang",
    "any",
)
# Declared kinds that name a class (optional: a node of no known class has none) and one that
# names an enum (required).
CLASS_KINDS: Final = ("ref", "entity")
ENUM_KINDS: Final = ("enum",)

# The base types of attributes as a column holds them (a decimal travels as text, a date as text).
_BASE_KINDS: Final = {
    "string": "string",
    "text": "string",
    "iri": "string",
    "blob": "string",
    "duration": "string",
    "integer": "integer",
    "decimal": "decimal",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    # json, external_ref and local_path hold any JSON value or object
}
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
class ColumnType:
    """What a result column holds: ``kind`` (one of ``COLUMN_KINDS``), ``many`` for a list of
    it, ``enum`` of an ``enum``, ``of_class`` of a ``ref`` or an ``entity`` (``None``: any class)
    and whether the value may be null."""

    kind: str
    many: bool = False
    enum: str | None = None
    of_class: str | None = None
    nullable: bool = True

    @property
    def text(self) -> str:
        """The type as people read it: ``integer``, ``ref org:Person``, ``list of date``."""
        named = {"ref": self.of_class, "entity": self.of_class, "enum": self.enum}.get(self.kind)
        kind = f"{self.kind} {named}" if named else self.kind
        return f"list of {kind}" if self.many else kind

    def to_json(self) -> JsonObject:
        return {
            "type": self.kind,
            "class": self.of_class,
            "enum": self.enum,
            "many": self.many,
            "nullable": self.nullable,
        }


def contradicts(declared: ColumnType, inferred: ColumnType) -> bool:
    """Whether a declared type says something other than what the query makes certain: another
    kind, list or not, or another enum or class where the inference names one."""
    return (
        declared.kind != inferred.kind
        or declared.many != inferred.many
        or (inferred.enum is not None and declared.enum != inferred.enum)
        or (inferred.of_class is not None and declared.of_class != inferred.of_class)
    )


def merge(declared: ColumnType | None, inferred: ColumnType | None) -> ColumnType | None:
    """The type a column has when both exist and agree: the inferred one (it knows whether the
    value can be null), with the class or enum the declaration adds where the query names none;
    one of them when only that exists."""
    if inferred is None:
        return declared
    if declared is None:
        return inferred
    return replace(
        inferred,
        enum=inferred.enum or declared.enum,
        of_class=inferred.of_class or declared.of_class,
    )


def attribute_type(model: CompiledModel, attribute: AttributeDef | QualifierDef) -> ColumnType:
    """The column type of the values of an attribute or a qualifier (nullable as a column: an
    entity simply lacks an attribute that is unset)."""
    if attribute.kind == "enum":
        return ColumnType("enum", attribute.many, enum=attribute.type)
    base = model.value_base(attribute.kind, attribute.type)
    kind = "lang" if base in LANG_BASES else _BASE_KINDS.get(base, "any")
    return ColumnType(kind, attribute.many)


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
        for clause, introduced in zip(resolved.query.clauses, resolved.introduced, strict=True):
            self.scope.update(introduced)
            if isinstance(clause, Match):
                self.match(clause)
            elif isinstance(clause, With):
                self.with_(clause)

    def match(self, clause: Match) -> None:
        for part in clause.patterns:
            for node in part.nodes:
                if node.variable is None:
                    continue
                of_class = (
                    node.labels[0][0]
                    if len(node.labels) == 1 and len(node.labels[0]) == 1
                    else None
                )
                known = self.bound.get(node.variable)
                if known is None:
                    self.bound[node.variable] = _Bound(of_class, clause.optional)
                elif known.of_class is None and of_class is not None:
                    self.bound[node.variable] = _Bound(of_class, known.optional)
            self.rels.update(r.variable for r in part.rels if r.variable and r.length is None)

    def with_(self, clause: With) -> None:
        """A node or relationship variable survives a ``WITH`` that passes it on by its own name;
        one that is renamed or computed on is a value from then on."""
        projection = clause.projection
        kept = dict(self.bound) if projection.star else {}
        rels = set(self.rels) if projection.star else set()
        for item in projection.items:
            if isinstance(item.expr, Variable) and item.expr.name == item.name:
                if item.name in self.bound:
                    kept[item.name] = self.bound[item.name]
                if item.name in self.rels:
                    rels.add(item.name)
            else:
                kept.pop(item.name, None)
                rels.discard(item.name)
        self.bound, self.rels = kept, rels

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
        if info is None or info.kind != "node":
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
        if info is None or info.kind != "rel":
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
