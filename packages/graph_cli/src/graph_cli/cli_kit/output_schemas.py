"""Schemas of answers (contracts B9.3, B11.10): the envelope's parts, the inventory and
helpers to declare a command's ``data``.

A command declares the JSON Schema (2020-12) of its ``data`` in ``CommandSpec.output``.
Shapes used by more than one command are definitions with one home each, next to what
produces them: the envelope's parts and the inventory (both programs answer them) here,
the app's shapes in ``app_cli.output_schemas``, the maintenance shapes in
``graph_cli_admin.maintenance_cli.output_schemas``; a declaration points to them with
``ref``. A program merges its definitions with ``merge_defs``; the inventory carries them
as ``$defs`` (``describe commands --output-schemas``).

Published schemas are open: a consumer ignores keys it does not know (V.4) and shapes grow
additively within /1 (B9.3). The tests close every object that does not say
``additionalProperties`` itself, so a key the code adds without declaring it fails the suite.
Objects are never extended with ``allOf``: a closed branch would refuse its siblings' keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.cli_kit.envelope import ENVELOPE_SCHEMA
from graph_cli.kernel.canonical import JsonValue

type Schema = Mapping[str, JsonValue]

STRING: Final[Schema] = {"type": "string"}
INTEGER: Final[Schema] = {"type": "integer"}
BOOLEAN: Final[Schema] = {"type": "boolean"}
NULL: Final[Schema] = {"type": "null"}
ANY: Final[Schema] = {}  # any JSON value: attribute values, parameters, given values
OPEN: Final[Schema] = {"type": "object", "additionalProperties": True}  # free-form details


def ref(name: str) -> Schema:
    return {"$ref": f"#/$defs/{name}"}


def nullable(schema: Schema) -> Schema:
    return {"anyOf": [schema, NULL]}


def array(items: Schema) -> Schema:
    return {"type": "array", "items": items}


def enum(*values: str) -> Schema:
    return {"type": "string", "enum": list(values)}


def obj(
    required: Mapping[str, Schema] | None = None,
    optional: Mapping[str, Schema] | None = None,
    *,
    extra: Schema | None = None,
) -> Schema:
    """An object with ``required`` and ``optional`` keys; ``extra`` types other keys (a map),
    else the object lists every key it has."""
    properties = {**(required or {}), **(optional or {})}
    schema: dict[str, JsonValue] = {"type": "object"}
    if properties:
        schema["properties"] = dict(properties)
    if required:
        schema["required"] = list(required)
    if extra is not None:
        schema["additionalProperties"] = extra
    return schema


def one_of(*schemas: Schema) -> Schema:
    """Shapes that differ by option (``--dry-run``, ``--explain``): exactly one branch fits."""
    return {"oneOf": list(schemas)}


def merge_defs(*parts: Mapping[str, Schema]) -> dict[str, Schema]:
    """One ``$defs`` of several homes; a name defined twice differently is a bug."""
    merged: dict[str, Schema] = {}
    for part in parts:
        for name, schema in part.items():
            if name in merged and merged[name] != schema:
                raise ValueError(f"output schema definition {name!r} is defined twice")
            merged[name] = schema
    return merged


def _graph() -> Schema:
    """The envelope's ``graph`` (B9.2): ``services.read.graph_json`` and its admin twin
    ``services_admin.graphs.envelope_graph``."""
    return obj(
        {
            "id": STRING,
            "name": STRING,
            "state": STRING,
            "ontology": obj({"bundle": STRING, "version": STRING}),
            "seq": nullable(INTEGER),
        }
    )


def _envelope() -> Schema:
    """B9.2; ``data`` is the command's own."""
    return obj(
        {
            "schema": {"const": ENVELOPE_SCHEMA},
            "ok": BOOLEAN,
            "command": nullable(STRING),
            "graph": nullable(ref("graph")),
            "data": ANY,
            "page": nullable(ref("page")),
            "hints": array(ref("hint")),
            "warnings": array(ref("warning")),
            "error": nullable(ref("error")),
        }
    )


def _param() -> Schema:
    """``cli_kit.spec.ParamSpec.to_json``."""
    return obj(
        {
            "name": STRING,
            "type": STRING,
            "required": BOOLEAN,
            "repeatable": BOOLEAN,
            "summary": STRING,
        },
        {
            "choices": array(STRING),
            "default": ANY,
            "pairs": enum("map", "multimap"),
            "pair_values": enum("text", "json"),
        },
    )


def _inventory() -> Schema:
    """``cli_kit.inventory.build_inventory`` (B11.2, B11.3), of either program."""
    command = obj(
        {
            "path": array(STRING),
            "kind": enum("read", "write", "meta"),
            "surface": STRING,
            "action": nullable(STRING),
            "summary": STRING,
            "args": array(ref("param")),
            "options": array(ref("param")),
            "examples": array(obj({"command": STRING, "summary": STRING})),
            "errors": array(STRING),
            "exit_codes": array(INTEGER),
            "limits": obj(extra=ANY),
        },
        {"remote": enum("server", "hooked", "client", "local"), "output": OPEN},
    )
    return obj(
        {
            "schema": {"const": "graph_cli.inventory/1"},
            "program": STRING,
            "program_version": STRING,
            "surface": STRING,
            "global_options": array(ref("param")),
            "commands": array(command),
            "actions": array(STRING),
            "error_codes": array(obj({"code": STRING, "category": STRING, "exit": INTEGER})),
        },
        {"$defs": obj(extra=OPEN)},
    )


DEFS: Final[dict[str, Schema]] = {
    "envelope": _envelope(),
    "graph": _graph(),
    "error": obj(
        {
            "code": STRING,
            "category": STRING,
            "retryable": BOOLEAN,
            "message": STRING,
            "hint": nullable(STRING),
            "details": OPEN,
        }
    ),
    "warning": obj({"code": STRING, "message": STRING, "details": OPEN}),
    "hint": obj({"text": STRING, "command": nullable(STRING)}),
    "page": obj(
        {
            "total": INTEGER,
            "shown": INTEGER,
            "limit": INTEGER,
            "offset": INTEGER,
            "truncated": BOOLEAN,
            "next_offset": nullable(INTEGER),
        }
    ),
    "finding": obj(
        {
            "severity": enum("error", "warning", "info"),
            "code": STRING,
            "target": OPEN,
            "message": STRING,
        },
        {"expected": ANY, "actual": ANY, "rule_id": STRING, "hint": STRING, "connecting": OPEN},
    ),
    "param": _param(),
    "inventory": _inventory(),
}
