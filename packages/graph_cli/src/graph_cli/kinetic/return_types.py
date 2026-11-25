"""The types of the result columns of a function: what a column can hold and how declared and
inferred types meet.

A function says what its columns hold in two ways. An author may declare a ``type`` per column
(``string integer decimal boolean date datetime enum ref entity lang any``, with ``many``,
``enum`` and ``class``). The query also tells some columns by itself: ``return_inference`` types
exactly those, and only where the text of the query makes the type certain. This module holds the
shared data: ``ColumnType``, the check that a declared type does not contradict an inferred one
(``contradicts``), their merge (``merge``: where both exist the inferred nullability and the
declared class, for a node of no known class, are kept) and the type of an attribute
(``attribute_type``).

Pure and light: it imports neither the query engine nor storage, so the readers of published
catalogs can use it. The compiled entry stores only what an author declared, so existing catalogs
keep their bytes; the types of the other columns are inferred again from the stored query wherever
they are shown (``compile.column_types``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel, QualifierDef
from graph_cli.ontology_layer.vocabulary import LANG_BASES

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
    kind, list or not, or another enum or class where the inference names one. ``any`` is the top
    type, so a declared ``any`` (of the same list-ness) says nothing false."""
    if declared.kind == "any":
        return declared.many != inferred.many
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
