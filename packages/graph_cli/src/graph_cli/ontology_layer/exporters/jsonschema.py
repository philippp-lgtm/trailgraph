"""JSON Schema (draft 2020-12) export of entity attributes (B7.4 ``exports/jsonschema``).

One schema per concrete class, describing the ``attrs`` object of its
revisions. Fidelity is deliberately partial: lifecycles become plain enums
(no transitions or guards), and rules, mutability, uniqueness, relations and
the no-float rule for ``json`` values are not expressed. The validator stays
the authority; the export is a convenience for external tools.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.values import DECIMAL_RE, DURATION_RE

DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

_UUID: Final = {
    "type": "string",
    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}
_BASE: Final[dict[str, dict[str, JsonValue]]] = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "iri": {"type": "string", "format": "iri"},
    "integer": {"type": "integer", "minimum": -MAX_SAFE_INT, "maximum": MAX_SAFE_INT},
    "decimal": {"type": "string", "pattern": DECIMAL_RE.pattern},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
    "datetime": {
        "type": "string",
        "format": "date-time",
        "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$",
    },
    "duration": {"type": "string", "format": "duration", "pattern": DURATION_RE.pattern},
    "json": {},
    "external_ref": {
        "type": "object",
        "properties": {"graph": _UUID, "entity": _UUID},
        "required": ["entity", "graph"],
        "additionalProperties": False,
    },
}
_FACET_KEYWORDS: Final = {
    "min": "minimum",
    "max": "maximum",
    "min_length": "minLength",
    "max_length": "maxLength",
    "pattern": "pattern",
}


def schema_file_name(curie: str) -> str:
    """``inv:Item`` -> ``inv.Item.json``."""
    return curie.replace(":", ".") + ".json"


def _value_schema(model: CompiledModel, attribute: AttributeDef) -> dict[str, JsonValue]:
    if attribute.kind == "enum":
        return {"enum": list(model.enums[attribute.type].value_names())}
    if attribute.kind == "lifecycle":
        return {"enum": list(model.lifecycles[attribute.type].state_names())}
    if attribute.kind == "base":
        return dict(_BASE[attribute.type])
    datatype = model.datatypes[attribute.type]
    schema = dict(_BASE[datatype.base])
    for facet, value in sorted(datatype.facets.items()):
        if facet == "single_line":
            if value is True:
                schema["not"] = {"pattern": "[\\r\\n]"}
        else:
            schema[_FACET_KEYWORDS[facet]] = value
    return schema


def _attribute_schema(model: CompiledModel, attribute: AttributeDef) -> dict[str, JsonValue]:
    value = _value_schema(model, attribute)
    schema: dict[str, JsonValue] = (
        {"type": "array", "items": value} if attribute.many else dict(value)
    )
    schema["description"] = attribute.doc.summary
    if attribute.default is not None:
        schema["default"] = attribute.default
    return schema


def export_jsonschema(model: CompiledModel) -> dict[str, dict[str, JsonValue]]:
    """File name -> schema document, for every concrete class."""
    namespaces = dict(model.prefixes)
    files: dict[str, dict[str, JsonValue]] = {}
    for curie, cls in sorted(model.classes.items()):
        if cls.abstract:
            continue
        prefix, _, name = curie.partition(":")
        files[schema_file_name(curie)] = {
            "$schema": DIALECT,
            # $id must not carry a fragment, so the namespace's "#" is dropped.
            "$id": f"{namespaces[prefix].partition('#')[0].rstrip('/')}/{name}/attrs",
            "$comment": f"attrs of {namespaces[prefix]}{name}",
            "title": curie,
            "description": cls.doc.summary,
            "type": "object",
            "properties": {
                attr: _attribute_schema(model, definition)
                for attr, definition in cls.attributes.items()
            },
            "required": sorted(a for a, d in cls.attributes.items() if d.required),
            "additionalProperties": False,
        }
    return files
