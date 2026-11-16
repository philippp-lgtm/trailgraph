"""JSON Schema (draft 2020-12) export of entity attributes (B7.4 ``exports/jsonschema``).

One schema per concrete class, describing the ``attrs`` object of its
revisions. Fidelity is deliberately partial: lifecycles become plain enums
(no transitions or guards), and rules, mutability, uniqueness, relations and
the no-float rule for ``json`` values are not expressed. The validator stays
the authority; the export is a convenience for external tools.

A ``blob``, ``local_path``, ``lang_string`` or ``lang_text`` value carries
``x-graph_cli-base``: the schema states the shape, not every rule (the
stored blob, the path segments, the full BCP 47 grammar and canonical case
of a tag). Facets of a language-tagged datatype apply to its ``value``.
A label role is the annotation ``x-graph_cli-role`` on the property (draft
2020-12 collects unknown keywords as annotations; ``$comment`` is not one).
A marked attribute carries ``x-graph_cli-markings`` (the marking CURIEs a reader must hold).
Validators in a strict mode, such as Ajv's, need the keywords registered.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import HASH_RE, MAX_SAFE_INT, JsonValue
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.values import DECIMAL_RE, DURATION_RE
from graph_cli.ontology_layer.vocabulary import FILE_ROOT_RE, LANG_BASES

DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
ROLE_KEYWORD: Final = "x-graph_cli-role"
MARKINGS_KEYWORD: Final = "x-graph_cli-markings"
BASE_KEYWORD: Final = "x-graph_cli-base"  # bases JSON Schema cannot fully express

_UUID: Final = {
    "type": "string",
    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}


def _lang(base: str) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "lang": {"type": "string", "pattern": "^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$"},
            "value": {"type": "string"},
        },
        "required": ["lang", "value"],
        "additionalProperties": False,
        BASE_KEYWORD: base,
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
    "blob": {"type": "string", "pattern": HASH_RE.pattern, BASE_KEYWORD: "blob"},
    "local_path": {
        "type": "object",
        "properties": {
            "root": {"type": "string", "pattern": FILE_ROOT_RE.pattern},
            "path": {"type": "string", "minLength": 1},
        },
        "required": ["path", "root"],
        "additionalProperties": False,
        BASE_KEYWORD: "local_path",
    },
    "lang_string": _lang("lang_string"),
    "lang_text": _lang("lang_text"),
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
    text = dict(_BASE["string"]) if datatype.base in LANG_BASES else schema
    for facet, value in sorted(datatype.facets.items()):
        if facet == "single_line":
            if value is True:
                text["not"] = {"pattern": "[\\r\\n]"}
        else:
            text[_FACET_KEYWORDS[facet]] = value
    if text is not schema:
        properties = schema["properties"]
        assert isinstance(properties, dict)
        schema["properties"] = {**properties, "value": text}
    return schema


def _attribute_schema(model: CompiledModel, attribute: AttributeDef) -> dict[str, JsonValue]:
    value = _value_schema(model, attribute)
    schema: dict[str, JsonValue] = (
        {"type": "array", "items": value} if attribute.many else dict(value)
    )
    schema["description"] = attribute.doc.summary
    if attribute.role is not None:
        schema[ROLE_KEYWORD] = attribute.role
    if attribute.markings:
        schema[MARKINGS_KEYWORD] = list(attribute.markings)
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
