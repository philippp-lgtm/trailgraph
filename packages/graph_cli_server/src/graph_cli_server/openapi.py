"""The OpenAPI 3.1 document of the server API, generated from the inventory (contracts B14.2).

A pure function of the ``graph_cli.inventory/1`` document: one operation
per command the server runs, its parameters as the ``args`` object of the
request, the envelope as the answer. Deterministic like the inventory.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.json_access import as_list, as_object, req_list

API_VERSION: Final = "graph_cli.api/1"
_ENVELOPE: Final = "#/components/schemas/Envelope"
_UPLOAD: Final[dict[str, JsonValue]] = {
    "type": "object",
    "required": ["upload", "name"],
    "properties": {
        "upload": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "name": {"type": "string"},
        "origin": {"type": ["object", "null"]},
    },
}


def openapi(inventory: Mapping[str, JsonValue], *, version: str) -> dict[str, JsonValue]:
    paths: dict[str, JsonValue] = {
        "/v1/health": {"get": _operation("health", "Liveness and version; no token needed.")},
        "/v1/inventory": {"get": _operation("inventory", "The command inventory.")},
        "/v1/uploads": {"put": _operation("upload", "Stage bytes for a file parameter.")},
        "/v1/graphs/{graph}/files/{ref}": {
            "get": _operation("file_bytes", "The stored file of an entity, as file path.")
        },
        "/v1/graphs/{graph}/skill": {
            "post": _operation("graph_skill", "The graph's published skill, rendered.")
        },
    }
    for value in req_list(inventory, "commands"):
        command = as_object(value, "command")
        if command.get("remote") == "client":
            continue
        path = [str(part) for part in as_list(command["path"], "path")]
        params = [
            as_object(p, "param")
            for key in ("args", "options")
            for p in as_list(command.get(key, []), key)
        ]
        operation = _operation(
            "_".join(path), str(command.get("summary", "")), body=_args_schema(params)
        )
        operation["x-graph-cli-kind"] = command.get("kind")
        operation["x-graph-cli-remote"] = command.get("remote")
        paths["/v1/commands/" + "/".join(path)] = {"post": operation}
    return {
        "openapi": "3.1.0",
        "info": {"title": "graph_cli server API", "version": version, "x-api": API_VERSION},
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {"Envelope": _envelope_schema()},
        },
        "security": [{"bearer": []}],
        "paths": paths,
    }


def _operation(
    operation_id: str, summary: str, *, body: dict[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    operation: dict[str, JsonValue] = {
        "operationId": operation_id,
        "summary": summary,
        "responses": {
            "default": {
                "description": "The envelope; the status follows the error category.",
                "content": {"application/json": {"schema": {"$ref": _ENVELOPE}}},
            }
        },
    }
    if body is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": body}},
        }
    return operation


def _args_schema(params: list[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {}
    required: list[JsonValue] = []
    for param in params:
        name = str(param["name"])
        schema = _value_schema(param)
        if param.get("repeatable"):
            schema = {"type": "array", "items": schema}
        schema["description"] = param.get("summary")
        properties[name] = schema
        if param.get("required"):
            required.append(name)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "args": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            },
            "actor": {"type": "object"},
            "pointer": {"type": "object"},
            "graph_env": {"type": ["string", "null"]},
        },
    }


def _value_schema(param: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    kind = param.get("type")
    if kind == "flag":
        return {"type": "boolean"}
    if kind == "integer":
        return {"type": "integer"}
    if kind == "file":
        return dict(_UPLOAD)
    if kind == "choice":
        return {"type": "string", "enum": param.get("choices")}
    return {"type": "string"}


def _envelope_schema() -> dict[str, JsonValue]:
    keys = ["schema", "ok", "command", "graph", "data", "page", "hints", "warnings", "error"]
    return {
        "type": "object",
        "required": list(keys),
        "properties": {
            "schema": {"const": "graph_cli.envelope/1"},
            "ok": {"type": "boolean"},
            "error": {
                "type": ["object", "null"],
                "properties": {
                    key: {} for key in ("code", "category", "retryable", "message", "hint")
                },
            },
        },
    }
