"""JSON Schema 2020-12 of the cells of a function's result columns (``kinetic.return_types``), as
the tool manifest renders them for one caller.

A cell is what ``graph run`` shows: a decimal and a date as text, a node as the lean entity object
(``services.read.json_shapes.entity_json``), a language-tagged text as ``{lang, value}``, a list
as an array. Everything that depends on what the caller sees comes from the model it is given,
which for a limited caller is the filtered one: the values of an enum, the attributes of an entity
class. A type whose enum or class the model lacks (an entry that no longer fits the ontology)
loses that detail and stays valid for the cells it still describes.

A column with no type is ``{}`` marked ``x-graph-cli: {typed: false}``: any JSON value. Every
column schema carries the column's summary as ``description``.
"""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.output_schemas import standalone
from graph_cli.cli_kit.output_schemas import ref
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.ids import ENTITY_REF_PATTERN
from graph_cli.kinetic.return_types import ColumnType, attribute_type
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import DECIMAL_RE

EXTENSION: Final = "x-graph-cli"
NULL: Final[JsonObject] = {"type": "null"}
_SIMPLE: Final = {"string": "string", "integer": "integer", "boolean": "boolean"}
# A decimal cell is plain notation (``values.decimal_text``); an exponent shows only beyond its bound,
# so the grammar of a decimal takes an optional one here (not as a parameter).
_DECIMAL_CELL: Final = DECIMAL_RE.pattern.removesuffix("$") + "([eE][+-]?[0-9]+)?$"
_ENTITY_REQUIRED: Final = ("id", "alias", "type", "rev", "record_status", "attrs", "edges")


def untyped(summary: str) -> JsonObject:
    """The schema of a column nothing types: any JSON value."""
    return {"description": summary, EXTENSION: {"typed": False}}


def column_schema(column: ColumnType | None, summary: str, model: CompiledModel) -> JsonObject:
    """The schema of the cells of a column, ``nullable`` ones as ``anyOf [T, null]``."""
    if column is None:
        return untyped(summary)
    value = _value(column, model)
    schema: JsonObject = {"type": "array", "items": value} if column.many else value
    if column.nullable:
        return {"anyOf": [schema, NULL], "description": summary}
    return {**schema, "description": summary}


def _value(column: ColumnType, model: CompiledModel) -> JsonObject:
    """The schema of one cell value, without the list around a ``many`` column."""
    kind = column.kind
    if kind in _SIMPLE:
        return {"type": _SIMPLE[kind]}
    if kind == "decimal":
        return {"type": "string", "pattern": _DECIMAL_CELL}
    if kind == "date":
        return {"type": "string", "format": "date"}
    if kind == "datetime":
        return {"type": "string", "format": "date-time"}
    if kind == "enum":
        found = model.enums.get(column.enum) if column.enum is not None else None
        schema: dict[str, JsonValue] = {"type": "string"}
        if found is not None:
            schema["enum"] = [v.name for v in found.values]
        return schema
    if kind == "ref":
        schema = {"type": "string", "pattern": ENTITY_REF_PATTERN}
        if column.of_class is not None:
            schema[EXTENSION] = {"class": column.of_class}
        return schema
    if kind == "lang":
        return {
            "type": "object",
            "properties": {"lang": {"type": "string"}, "value": {"type": "string"}},
            "required": ["lang", "value"],
        }
    if kind == "entity":
        return entity_schema(column.of_class, model)
    return {}


def entity_schema(of_class: str | None, model: CompiledModel) -> JsonObject:
    """The lean entity object a ``RETURN v`` shows: the always shown keys, ``attrs`` typed by the
    attributes of the class the caller sees (an entity of a subclass may carry more, so the object
    stays open) and ``edges``. The class is the label of the pattern (``x-graph-cli.class``)."""
    base = standalone(ref("entity"))
    properties = dict(_object(base["properties"]))
    cls = model.classes.get(of_class) if of_class is not None else None
    if cls is not None:
        attrs: dict[str, JsonValue] = {}
        for name, attribute in sorted(cls.attributes.items()):
            found = attribute_type(model, attribute)
            one = _value(ColumnType(found.kind, enum=found.enum), model)
            attrs[name] = {"type": "array", "items": one} if found.many else one
        properties["attrs"] = {"type": "object", "properties": attrs}
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": properties,
        "required": list(_ENTITY_REQUIRED),
    }
    if of_class is not None:
        schema[EXTENSION] = {"class": of_class}
    return schema


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value
