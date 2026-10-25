"""The OpenAPI 3.1 documents of the server API, generated from the inventory (B14.2, B14.15).

A pure function of a ``graph_cli.inventory/1`` document with output schemas
(``describe commands --output-schemas``, B11.10): one operation per command the server
runs, its parameters as the ``args`` object of the request (canonical names with the
structured forms, the inventory names ``deprecated``), the envelope with the command's
``data`` as the answer, and one response per status its error codes reach (B14.5).
``components.schemas`` holds the inventory's ``$defs`` under their names and the ``data``
of every command as ``<operationId>_data``. Every error answer is also offered as
problem details (``application/problem+json``, B14.22 G8); a write that takes an
idempotency key declares the ``Idempotency-Key`` header (G7). Deterministic like the
inventory; ``public_url`` adds ``servers`` (G9). The app surface and the admin API
(``admin``) each get their own document, each with its own address; both describe
``/v1/dist/`` when the server hands out programs (``dist``), the app document
``/v1/enroll`` when the server exchanges enrollment codes (``enroll``, B14.21).
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Final

from graph_cli.catalog.dist import MANIFEST_SCHEMA
from graph_cli.cli_kit.argv import api_name
from graph_cli.cli_kit.output_schemas import (
    ANY,
    BOOLEAN,
    INTEGER,
    NULL,
    OPEN,
    STRING,
    Schema,
    array,
    enum,
    nullable,
    obj,
    one_of,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_obj, req_str
from graph_cli_server.http_status import reachable
from graph_cli_server.problems import PROBLEM_TYPE

API_VERSION: Final = "graph_cli.api/1"
_DEFS: Final = "#/$defs/"
_SCHEMAS: Final = "#/components/schemas/"
# Every error answer: the envelope with ``error`` set (``data`` only for a partial result).
_FAILED: Final = "failed_envelope"
_PROBLEM: Final = "problem"
_ERROR_TYPE: Final = "error_type"
_EXIT_HEADER: Final = "Graph-Cli-Exit-Code"
_RETRY_HEADER: Final = "Retry-After"
_KEY_HEADER: Final = "Idempotency-Key"
_KEY_OPTION: Final = "--idempotency-key"
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
# The structured forms of B14.22 G6 (``cli_kit.argv``).
_SCALAR: Final[Schema] = {"type": ["string", "number", "boolean"]}
_PAIR_NAME: Final[Schema] = {"type": "string", "pattern": "^[^=]+$"}
_FILTER: Final[Schema] = {
    "type": "object",
    "description": "field=value or field:op=value as an object; op defaults to eq.",
    "additionalProperties": False,
    "required": ["field", "value"],
    "properties": {
        "field": {"type": "string", "pattern": "^[^=:]+$"},
        "op": {"type": ["string", "null"], "pattern": "^[a-z]+$"},
        "value": {
            "description": "exists takes a boolean, in a non-empty array without , in its items.",
            "type": ["string", "number", "boolean", "array"],
            "items": {"type": ["string", "number", "boolean"]},
        },
    },
}
# The routes that are no command (``app.ServerApp._dispatch``).
HEALTH: Final[Schema] = obj({"ok": {"const": True}, "version": STRING, "api": STRING})
ERROR_TYPE: Final[Schema] = obj(
    {
        "code": STRING,
        "category": STRING,
        "retryable": BOOLEAN,
        "status": INTEGER,
        "exit_code": INTEGER,
        "summary": STRING,
    }
)
# RFC 9457 problem details of a failed envelope (``problems.problem``).
PROBLEM: Final[Schema] = obj(
    {
        "type": {"type": "string", "format": "uri-reference"},
        "title": STRING,
        "status": INTEGER,
        "detail": STRING,
        "code": STRING,
        "category": STRING,
        "retryable": BOOLEAN,
        "hint": {"anyOf": [STRING, NULL]},
        "details": OPEN,
        "exit_code": INTEGER,
        "warnings": {"type": "array", "items": {"$ref": _SCHEMAS + "warning"}},
    },
    {"data": ANY},
)
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
# ``catalog.dist.Manifest`` (B14.18): the programs ``/v1/dist/`` hands out.
_FILE_KEYS: Final[dict[str, Schema]] = {"file": STRING, "hash": _DIGEST, "size": INTEGER}
MANIFEST: Final[Schema] = obj(
    {
        "schema": {"const": MANIFEST_SCHEMA},
        "programs": obj(
            extra=obj(
                {
                    "distribution": STRING,
                    "version": STRING,
                    "build": _DIGEST,
                    "requires": array(STRING),
                    "access": enum("public", "admin"),
                    **_FILE_KEYS,
                }
            )
        ),
        "constraints": nullable(obj(_FILE_KEYS)),
    }
)
# ``POST /v1/enroll`` (``graph_cli_server.enroll``): the body, and the answer with
# ``check`` (what the code is for) or without (the new token, once).
_SCOPE: Final[Schema] = enum("app", "admin")
ENROLL_BODY: Final[Schema] = {
    **obj({"code": STRING}, {"label": nullable(STRING), "check": BOOLEAN}),
    "additionalProperties": False,
    "description": "At most 4 KiB.",
}
ENROLLED: Final[Schema] = one_of(
    obj(
        {
            "enrollment": obj(
                {
                    "id": STRING,
                    "project": STRING,
                    "scope": _SCOPE,
                    "expires_at": STRING,
                    "graphs": array(STRING),
                }
            )
        }
    ),
    obj(
        {
            "token": STRING,
            "name": STRING,
            "project": STRING,
            "scope": _SCOPE,
            "graphs": array(STRING),
        }
    ),
)
# The answer of the openapi.json routes: this document, read with an OpenAPI tool.
DOCUMENT: Final[Schema] = {
    "type": "object",
    "required": ["openapi", "info", "paths"],
    "properties": {"openapi": {"const": "3.1.0"}},
    "additionalProperties": True,
}
_SERVED: Final = ("server", "hooked")


def openapi(
    inventory: Mapping[str, JsonValue],
    *,
    version: str,
    commands: str = "/v1/commands/",
    admin: bool = False,
    public_url: str | None = None,
    dist: bool = False,
    enroll: bool = False,
) -> dict[str, JsonValue]:
    """``admin``: the admin API's document (its commands under ``commands``). ``dist`` and
    ``enroll``: the server serves those routes (the document describes only what it does)."""
    schemas = {name: _components(schema) for name, schema in req_obj(inventory, "$defs").items()}
    categories = {
        req_str(entry, "code"): ErrorCategory(req_str(entry, "category"))
        for entry in (as_object(e, "error_code") for e in req_list(inventory, "error_codes"))
    }
    _add(schemas, _FAILED, _failed(as_object(schemas["envelope"], "envelope")))
    _add(schemas, _PROBLEM, dict(PROBLEM))
    _add(schemas, _ERROR_TYPE, dict(ERROR_TYPE))
    upload = {"put": _operation("upload", "Stage bytes for a file parameter.", _json(UPLOADED))}
    listed = obj({"errors": {"type": "array", "items": _ref(_ERROR_TYPE)}})
    paths: dict[str, JsonValue] = {
        "/v1/health": {
            "get": _public(
                _operation("health", "Liveness and version; no token needed.", _json(HEALTH))
            )
        },
        "/v1/errors": {
            "get": _public(
                _operation("errors", "The error codes problem types point to.", _json(listed))
            )
        },
        "/v1/errors/{code}": {
            "parameters": _in_path("code"),
            "get": _public(
                _operation("error", "One error code of the registry.", _json(_ref(_ERROR_TYPE)))
            ),
        },
    }
    if dist:
        paths["/v1/dist/manifest"] = {
            "get": _public(
                _operation(
                    "dist_manifest",
                    "The programs the server hands out; no token needed.",
                    _json(MANIFEST),
                )
            )
        }
        file = _operation(
            "dist_file",
            "A file the manifest lists; the graph-admin wheel needs an admin token.",
            _bytes(dist=True),
        )
        paths["/v1/dist/{file}"] = {
            "parameters": _in_path("file"),
            "get": {**file, "security": [{}, {"bearer": []}]},
        }
    inventory_answer = _json(_ref("inventory"))
    document_answer = _json(DOCUMENT)
    if admin:
        paths["/v1/admin/inventory"] = {
            "get": _operation("inventory", "The admin inventory.", inventory_answer)
        }
        paths["/v1/admin/openapi.json"] = {
            "get": _operation("openapi", "This document.", document_answer)
        }
        paths["/v1/uploads"] = upload
    else:
        _add(schemas, "graph_skill_data", GRAPH_SKILL)
        paths.update(
            {
                "/v1/inventory": {
                    "get": _operation("inventory", "The command inventory.", inventory_answer)
                },
                "/v1/openapi.json": {
                    "get": _operation("openapi", "This document.", document_answer)
                },
                "/v1/uploads": upload,
                "/v1/graphs/{graph}/files/{ref}": {
                    "parameters": _in_path("graph", "ref"),
                    "get": _operation(
                        "file_bytes",
                        "The stored file of an entity, as file path.",
                        _bytes(),
                        unchanged=True,
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
    if enroll and not admin:
        _add(schemas, "enroll_data", ENROLLED)
        paths["/v1/enroll"] = {
            "post": _public(
                _operation(
                    "enroll",
                    "Exchange an enrollment code for a token, or check it; no token needed.",
                    _json(_answer(schemas, "enroll_data")),
                    errors=_responses(
                        {
                            400: ["usage.invalid_input", "usage.invalid_argument"],
                            401: ["remote.unauthorized"],
                            503: ["backend.lock_timeout"],
                        }
                    ),
                    body=dict(ENROLL_BODY),
                )
            )
        }
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
        if any(p.get("name") == _KEY_OPTION for p in params):
            operation["parameters"] = [_key_header()]
        operation["x-graph-cli-kind"] = command.get("kind")
        operation["x-graph-cli-remote"] = command.get("remote")
        paths[commands + "/".join(path)] = {"post": operation}
    title = "graph_cli server admin API" if admin else "graph_cli server API"
    document: dict[str, JsonValue] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version, "x-api": API_VERSION},
    }
    if public_url is not None:
        document["servers"] = [{"url": public_url}]
    document.update(
        components={
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "headers": {
                _EXIT_HEADER: {
                    "description": "The exit code the program would have (B9.4).",
                    "schema": dict(INTEGER),
                },
                _RETRY_HEADER: {
                    "description": "Seconds to wait before a retry; on a retryable error only.",
                    "schema": dict(INTEGER),
                },
            },
            "schemas": dict(sorted(schemas.items())),
        },
        security=[{"bearer": []}],
        paths=paths,
    )
    return document


def _operation(
    operation_id: str,
    summary: str,
    answer: dict[str, JsonValue],
    *,
    errors: dict[str, JsonValue] | None = None,
    body: dict[str, JsonValue] | None = None,
    unchanged: bool = False,
) -> dict[str, JsonValue]:
    """``errors``: one response per status the declared codes reach; ``default`` is every
    other error answer (a code no command declares, such as a malformed body).
    ``unchanged``: a ``304`` for a matching ``If-None-Match``."""
    responses: dict[str, JsonValue] = {"200": answer}
    if unchanged:
        responses["304"] = {
            "description": "Not Modified: If-None-Match names the ETag of the stored bytes.",
            "headers": {"ETag": {"schema": dict(STRING)}},
        }
    responses.update(errors or {})
    responses["default"] = {
        "description": "The envelope with the error, or problem details on request.",
        "content": _error_content(),
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
    return _responses(by_status)


def _responses(by_status: Mapping[int, list[str]]) -> dict[str, JsonValue]:
    """One error response per status, naming its codes."""
    responses: dict[str, JsonValue] = {}
    for status, listed in sorted(by_status.items()):
        headers: dict[str, JsonValue] = {_EXIT_HEADER: _header_ref(_EXIT_HEADER)}
        if status == 503:
            headers[_RETRY_HEADER] = _header_ref(_RETRY_HEADER)
        responses[str(status)] = {
            "description": f"{HTTPStatus(status).phrase}: {', '.join(listed)}.",
            "headers": headers,
            "content": _error_content(),
            "x-graph-cli-error-codes": list(listed),
        }
    return responses


def _error_content() -> dict[str, JsonValue]:
    """An error answer: the envelope, or problem details when ``Accept`` names them."""
    return {
        "application/json": {"schema": _ref(_FAILED)},
        PROBLEM_TYPE: {"schema": _ref(_PROBLEM)},
    }


def _header_ref(name: str) -> dict[str, JsonValue]:
    return {"$ref": f"#/components/headers/{name}"}


def _key_header() -> dict[str, JsonValue]:
    return {
        "name": _KEY_HEADER,
        "in": "header",
        "required": False,
        "description": (
            'The idempotency key as an RFC 9651 String ("<key>"), instead of'
            " idempotency_key in args; both must name the same key."
        ),
        "schema": {"type": "string", "pattern": '^"[A-Za-z0-9._:-]{1,128}"$'},
    }


def _public(operation: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """An operation that needs no token."""
    return {**operation, "security": []}


def _json(schema: Schema, *, exit_code: bool = False) -> dict[str, JsonValue]:
    response: dict[str, JsonValue] = {
        "description": "The answer.",
        "content": {"application/json": {"schema": dict(schema)}},
    }
    if exit_code:
        response["headers"] = {_EXIT_HEADER: _header_ref(_EXIT_HEADER)}
    return response


def _in_path(*names: str) -> list[JsonValue]:
    return [
        {"name": name, "in": "path", "required": True, "schema": dict(STRING)} for name in names
    ]


def _bytes(*, dist: bool = False) -> dict[str, JsonValue]:
    """The file download (``app.ServerApp._file``): the bytes, their hash and media type;
    ``dist``: a file of ``/v1/dist/`` (``app.ServerApp._dist``), an attachment."""
    if dist:
        return {
            "description": "The file's bytes.",
            "headers": {"Content-Disposition": {"schema": {"const": "attachment"}}},
            "content": {"application/octet-stream": {}},
        }
    return {
        "description": "The stored bytes.",
        "headers": {
            "ETag": {"description": "The content hash, quoted.", "schema": dict(STRING)},
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
    """``args`` by canonical name (B14.22 G5); the inventory name of an option is the same
    parameter, ``deprecated``, and a required one may come under either."""
    properties: dict[str, JsonValue] = {}
    required: list[JsonValue] = []
    either: list[JsonValue] = []
    for param in params:
        name = str(param["name"])
        canonical = api_name(name)
        schema = _param_schema(param)
        schema["description"] = param.get("summary")
        properties[canonical] = schema
        if canonical != name:
            properties[name] = {
                **schema,
                "description": f"The inventory name of {canonical}; never both.",
                "deprecated": True,
            }
        if param.get("required"):
            if canonical == name:
                required.append(name)
            else:
                either.append({"anyOf": [{"required": [canonical]}, {"required": [name]}]})
    args: dict[str, JsonValue] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
    if either:
        args["allOf"] = either
    body: dict[str, JsonValue] = {"args": args, "actor": {"type": "object"}}
    if not admin:
        body.update(pointer={"type": "object"}, graph_env={"type": ["string", "null"]})
    return {"type": "object", "additionalProperties": False, "properties": body}


def _param_schema(param: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The CLI form of a parameter, and its structured form where it has one (G6)."""
    schema = _value_schema(param)
    if param.get("type") == "filter":
        schema = {"oneOf": [schema, _FILTER]}
    if param.get("repeatable"):
        schema = {"type": "array", "items": schema}
    pairs = param.get("pairs")
    if pairs is not None:
        value: Schema = ANY if param.get("pair_values") == "json" else _SCALAR
        if pairs == "multimap":
            value = {"type": "array", "items": value}
        structured: Schema = {
            "type": "object",
            "propertyNames": _PAIR_NAME,
            "additionalProperties": value,
        }
        schema = {"oneOf": [schema, structured]}
    return schema


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
