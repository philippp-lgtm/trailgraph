"""The OpenAPI 3.1 documents of the server API, generated from the inventory (B14.2, B14.15).

A pure function of a ``graph_cli.inventory/1`` document with output schemas
(``describe commands --output-schemas``, B11.10): one operation per command the server
runs, its parameters as the ``args`` object of the request, the envelope with the
command's ``data`` as the answer, and one response per status its error codes reach
(B14.5). ``components.schemas`` holds the inventory's ``$defs`` under their names and the
``data`` of every command as ``<operationId>_data``. Deterministic like the inventory. The
app surface and the admin API (``admin``) each get their own document.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Final

from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    NULL,
    OPEN,
    STRING,
    Schema,
    obj,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_obj, req_str
from graph_cli_server.http_status import reachable

API_VERSION: Final = "graph_cli.api/1"
_DEFS: Final = "#/$defs/"
_SCHEMAS: Final = "#/components/schemas/"
# Every error answer: the envelope with ``error`` set (``data`` only for a partial result).
_FAILED: Final = "failed_envelope"
_EXIT_HEADER: Final = "Graph-Cli-Exit-Code"
# The remote client's own codes (B14.10): a client meets them, no server answers them.
_CLIENT_SIDE: Final = frozenset({"remote.unreachable", "remote.protocol", "remote.outcome_unknown"})
_DIGEST: Final[Schema] = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_UPLOAD: Final[Schema] = obj(
    {"upload": _DIGEST, "name": STRING}, {"origin": {"type": ["object", "null"]}}
)
_DIRECTORY: Final[Schema] = obj(
    {
        "files": {
            "type": "object",
            "description": "Relative path of every file to an upload of PUT /v1/uploads.",
            "additionalProperties": _DIGEST,
        },
        "name": STRING,
    }
)
# The routes that are no command (``app.ServerApp._dispatch``).
HEALTH: Final[Schema] = obj({"ok": {"const": True}, "version": STRING, "api": STRING})
UPLOADED: Final[Schema] = obj({"upload": _DIGEST, "size": INTEGER})
GRAPH_SKILL: Final[Schema] = obj(
    {
        "graph": obj({"id": STRING, "name": STRING}),
        "skill": obj(
            {
                "name": STRING,
                "kind": STRING,
                "files": obj(extra={"type": "string", "contentEncoding": "base64"}),
                "source": OPEN,
            }
        ),
    }
)
_SERVED: Final = ("server", "hooked")


def openapi(
    inventory: Mapping[str, JsonValue],
    *,
    version: str,
    commands: str = "/v1/commands/",
    admin: bool = False,
) -> dict[str, JsonValue]:
    """``admin``: the admin API's document (its commands under ``commands``)."""
    schemas = {name: _components(schema) for name, schema in req_obj(inventory, "$defs").items()}
    categories = {
        req_str(entry, "code"): ErrorCategory(req_str(entry, "category"))
        for entry in (as_object(e, "error_code") for e in req_list(inventory, "error_codes"))
    }
    _add(schemas, _FAILED, _failed(as_object(schemas["envelope"], "envelope")))
    upload = {"put": _operation("upload", "Stage bytes for a file parameter.", _json(UPLOADED))}
    paths: dict[str, JsonValue] = {
        "/v1/health": {
            "get": _operation("health", "Liveness and version; no token needed.", _json(HEALTH))
        },
    }
    inventory_answer = _json(_ref("inventory"))
    if admin:
        paths["/v1/admin/inventory"] = {
            "get": _operation("inventory", "The admin inventory.", inventory_answer)
        }
        paths["/v1/uploads"] = upload
    else:
        _add(schemas, "graph_skill_data", GRAPH_SKILL)
        paths.update(
            {
                "/v1/inventory": {
                    "get": _operation("inventory", "The command inventory.", inventory_answer)
                },
                "/v1/uploads": upload,
                "/v1/graphs/{graph}/files/{ref}": {
                    "parameters": _in_path("graph", "ref"),
                    "get": _operation(
                        "file_bytes", "The stored file of an entity, as file path.", _bytes()
                    ),
                },
                "/v1/graphs/{graph}/skill": {
                    "parameters": _in_path("graph"),
                    "post": _operation(
                        "graph_skill",
                        "The graph's published skill, rendered.",
                        _json(_answer(schemas, "graph_skill_data")),
                    ),
                },
            }
        )
    for value in req_list(inventory, "commands"):
        command = as_object(value, "command")
        if command.get("remote") not in _SERVED:
            continue
        path = [str(part) for part in as_list(command["path"], "path")]
        operation_id = "_".join(path)
        params = [
            as_object(p, "param")
            for key in ("args", "options")
            for p in as_list(command.get(key, []), key)
        ]
        _add(schemas, f"{operation_id}_data", _components(command["output"]))
        codes = [str(c) for c in as_list(command["errors"], "errors")]
        operation = _operation(
            operation_id,
            str(command.get("summary", "")),
            _json(_answer(schemas, f"{operation_id}_data"), exit_code=True),
            errors=_errors(codes, categories),
            body=_args_schema(params, admin),
        )
        operation["x-graph-cli-kind"] = command.get("kind")
        operation["x-graph-cli-remote"] = command.get("remote")
        paths[commands + "/".join(path)] = {"post": operation}
    title = "graph_cli server admin API" if admin else "graph_cli server API"
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version, "x-api": API_VERSION},
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "headers": {
                _EXIT_HEADER: {
                    "description": "The exit code the program would have (B9.4).",
                    "schema": dict(INTEGER),
                }
            },
            "schemas": dict(sorted(schemas.items())),
        },
        "security": [{"bearer": []}],
        "paths": paths,
    }


def _operation(
    operation_id: str,
    summary: str,
    answer: dict[str, JsonValue],
    *,
    errors: dict[str, JsonValue] | None = None,
    body: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """``errors``: one response per status the declared codes reach; ``default`` is every
    other error answer (a code no command declares, such as a malformed body)."""
    responses: dict[str, JsonValue] = {"200": answer, **(errors or {})}
    responses["default"] = {
        "description": "The envelope with the error.",
        "content": {"application/json": {"schema": _ref(_FAILED)}},
    }
    operation: dict[str, JsonValue] = {
        "operationId": operation_id,
        "summary": summary,
        "responses": responses,
    }
    if body is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": body}},
        }
    return operation


def _errors(codes: list[str], categories: Mapping[str, ErrorCategory]) -> dict[str, JsonValue]:
    """Per status the declared codes that reach it, with the envelope of the error."""
    by_status: dict[int, list[str]] = {}
    for code in codes:
        if code in _CLIENT_SIDE:
            continue
        for status in reachable(code, categories[code]):
            by_status.setdefault(status, []).append(code)
    responses: dict[str, JsonValue] = {}
    for status, listed in sorted(by_status.items()):
        responses[str(status)] = {
            "description": f"{HTTPStatus(status).phrase}: {', '.join(listed)}.",
            "headers": {_EXIT_HEADER: {"$ref": f"#/components/headers/{_EXIT_HEADER}"}},
            "content": {"application/json": {"schema": _ref(_FAILED)}},
            "x-graph-cli-error-codes": list(listed),
        }
    return responses


def _json(schema: Schema, *, exit_code: bool = False) -> dict[str, JsonValue]:
    response: dict[str, JsonValue] = {
        "description": "The answer.",
        "content": {"application/json": {"schema": dict(schema)}},
    }
    if exit_code:
        response["headers"] = {_EXIT_HEADER: {"$ref": f"#/components/headers/{_EXIT_HEADER}"}}
    return response


def _in_path(*names: str) -> list[JsonValue]:
    return [
        {"name": name, "in": "path", "required": True, "schema": dict(STRING)} for name in names
    ]


def _bytes() -> dict[str, JsonValue]:
    """The file download (``app.ServerApp._file``): the bytes, their hash and media type."""
    return {
        "description": "The stored bytes.",
        "headers": {
            "Graph-Cli-Content-Hash": {"schema": dict(_DIGEST)},
            "Graph-Cli-Media-Type": {
                "description": "Empty when the entity names none.",
                "schema": dict(STRING),
            },
        },
        "content": {"application/octet-stream": {}},
    }


def _answer(schemas: Mapping[str, JsonValue], data: str) -> dict[str, JsonValue]:
    """The envelope of a successful answer whose ``data`` is the component ``data``."""
    envelope = as_object(schemas["envelope"], "envelope")
    properties = dict(req_obj(envelope, "properties"))
    properties.update(ok={"const": True}, data=_ref(data), error=dict(NULL))
    return {**envelope, "properties": properties}


def _failed(envelope: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    properties = dict(req_obj(envelope, "properties"))
    properties.update(ok={"const": False}, error=_ref("error"))
    return {**envelope, "properties": properties}


def _add(schemas: dict[str, JsonValue], name: str, schema: JsonValue) -> None:
    if name in schemas:
        raise ValueError(f"component schema {name!r} is defined twice")
    schemas[name] = schema


def _ref(name: str) -> dict[str, JsonValue]:
    return {"$ref": _SCHEMAS + name}


def _components(schema: JsonValue) -> JsonValue:
    """A copy of an inventory schema whose references point into ``components``."""
    if isinstance(schema, list):
        return [_components(item) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    copy = {key: _components(value) for key, value in schema.items()}
    target = copy.get("$ref")
    if isinstance(target, str) and target.startswith(_DEFS):
        copy["$ref"] = _SCHEMAS + target.removeprefix(_DEFS)
    return copy


def _args_schema(params: list[Mapping[str, JsonValue]], admin: bool) -> dict[str, JsonValue]:
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
    body: dict[str, JsonValue] = {
        "args": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
        "actor": {"type": "object"},
    }
    if not admin:
        body.update(pointer={"type": "object"}, graph_env={"type": ["string", "null"]})
    return {"type": "object", "additionalProperties": False, "properties": body}


def _value_schema(param: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    kind = param.get("type")
    if kind == "flag":
        return dict(BOOLEAN)
    if kind == "integer":
        return dict(INTEGER)
    if kind == "file":
        return dict(_UPLOAD)
    if kind == "directory":
        return dict(_DIRECTORY)
    if kind == "choice":
        return {"type": "string", "enum": param.get("choices")}
    return dict(STRING)
