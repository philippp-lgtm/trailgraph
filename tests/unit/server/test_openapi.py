"""U: the OpenAPI documents of the server API (contracts B14.2, B14.5, B14.22 G4-G10).

Both documents pass the OpenAPI 3.1 validator, every reference resolves, every command
answers the envelope with its declared ``data`` and lists each code it declares under
every status that code reaches. The goldens show every change of the API in review.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from support.fixtures import GOLDENS, assert_golden
from support.openapi import admin_document, app_document

from graph_cli.kernel.error_codes import ErrorCategory, lookup_error_code
from graph_cli_server.http_status import reachable

DOCUMENTS = {"app": app_document, "admin": admin_document}
# The remote client's own codes: declared for the client, never answered by a server.
CLIENT_SIDE = {"remote.unreachable", "remote.protocol", "remote.outcome_unknown"}


@pytest.fixture(params=sorted(DOCUMENTS))
def surface(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def references(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def commands(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, item in document["paths"].items():
        if "/commands/" in path:
            yield path, item["post"]


def test_document_passes_the_validator_and_matches_its_golden(surface: str) -> None:
    document = DOCUMENTS[surface]()
    validate(document)
    assert_golden(
        GOLDENS / "server" / f"openapi_{surface}.json",
        (json.dumps(document, indent=2) + "\n").encode(),
    )


def test_every_reference_resolves(surface: str) -> None:
    document = DOCUMENTS[surface]()
    found = set(references(document))
    assert found
    for target in found:
        assert target.startswith("#/components/"), target
        node: Any = document
        for part in target.removeprefix("#/").split("/"):
            assert part in node, target
            node = node[part]


def test_a_command_answers_its_data_and_lists_every_code_by_status(surface: str) -> None:
    document = DOCUMENTS[surface]()
    schemas = document["components"]["schemas"]
    for path, operation in commands(document):
        operation_id = operation["operationId"]
        responses = operation["responses"]
        ok = responses["200"]["content"]["application/json"]["schema"]["properties"]
        assert ok["data"] == {"$ref": f"#/components/schemas/{operation_id}_data"}
        assert f"{operation_id}_data" in schemas
        listed: dict[str, set[int]] = {}
        for status, response in responses.items():
            if status == "200":
                continue
            assert response["content"] == {  # B14.22 G8: problem details on request
                "application/json": {"schema": {"$ref": "#/components/schemas/failed_envelope"}},
                "application/problem+json": {"schema": {"$ref": "#/components/schemas/problem"}},
            }
            if status == "503":
                assert "Retry-After" in response["headers"], path
            if status == "default":
                assert "x-graph-cli-error-codes" not in response, path
                continue
            for code in response["x-graph-cli-error-codes"]:
                listed.setdefault(code, set()).add(int(status))
        for code, statuses in listed.items():
            found = lookup_error_code(code)
            assert found is not None and found.category is not ErrorCategory.WARNING
            assert statuses == set(reachable(code, found.category)), (path, code)
        assert not CLIENT_SIDE & set(listed), path
        assert {"usage.invalid_argument", "internal.error", "remote.unauthorized"} <= set(listed), (
            path
        )


def test_unauthorized_is_401_without_a_token_and_403_for_another_scope() -> None:
    category = ErrorCategory.USAGE  # the category does not decide this code's status
    assert reachable("remote.unauthorized", category) == (401, 403)
    assert reachable("usage.invalid_argument", category) == (400,)


def test_a_failed_answer_carries_the_error_and_a_successful_one_none() -> None:
    schemas = app_document()["components"]["schemas"]
    failed = schemas["failed_envelope"]["properties"]
    assert failed["ok"] == {"const": False}
    assert failed["error"] == {"$ref": "#/components/schemas/error"}
    create = app_document()["paths"]["/v1/commands/create"]["post"]["responses"]["200"]
    ok = create["content"]["application/json"]["schema"]["properties"]
    assert ok["ok"] == {"const": True} and ok["error"] == {"type": "null"}


def request_validator(document: dict[str, Any], path: str) -> Draft202012Validator:
    body = document["paths"][path]["post"]["requestBody"]
    return Draft202012Validator(body["content"]["application/json"]["schema"])


def test_request_bodies_take_canonical_names_and_structured_forms() -> None:
    """B14.22 G5, G6: what ``cli_kit.argv.to_argv`` accepts, the document describes."""
    app = app_document()
    create = request_validator(app, "/v1/commands/create")
    item = {"type": "inv:Item", "set": {"label": "Drill", "quantity": 2}, "dry_run": True}
    assert create.is_valid({"args": {**item, "edge": {"inv:located_in": ["a"]}}})
    assert create.is_valid({"args": {"type": "inv:Item", "--set": ["label=Drill"]}})
    assert not create.is_valid({"args": {"type": "inv:Item", "set": {"label": {"a": 1}}}})
    assert not create.is_valid({"args": {"type": "inv:Item", "edge": {"inv:located_in": "a"}}})
    assert not create.is_valid({"args": {"set": {"label": "Drill"}}})
    properties = app["paths"]["/v1/commands/create"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["args"]["properties"]
    assert properties["--set"]["deprecated"] is True and "deprecated" not in properties["set"]
    find = request_validator(app, "/v1/commands/find")
    where = [{"field": "quantity", "op": "ge", "value": 2}, "label=Drill"]
    assert find.is_valid({"args": {"where": where}})
    assert not find.is_valid({"args": {"where": [{"field": "a:b", "value": "x"}]}})
    query = request_validator(app, "/v1/commands/query")
    assert query.is_valid({"args": {"text": "RETURN $n", "param": {"n": {"any": ["json"]}}}})
    # a required option under either name, as the server takes it
    verify = request_validator(admin_document(), "/v1/admin/commands/files/verify")
    assert verify.is_valid({"args": {"graph": "example_graph"}})
    assert verify.is_valid({"args": {"--graph": "example_graph"}})
    assert not verify.is_valid({"args": {}})


def test_writes_with_a_key_declare_the_header(surface: str) -> None:
    """B14.22 G7: exactly the commands that take ``--idempotency-key`` take the header."""
    from graph_cli.app_cli.commands import all_specs

    document = DOCUMENTS[surface]()
    takes = {
        "/".join(spec.path)
        for spec in all_specs()
        if any(param.name == "--idempotency-key" for param in spec.params)
    }
    found: set[str] = set()
    for path, operation in commands(document):
        for param in operation.get("parameters", []):
            assert param["name"] == "Idempotency-Key" and param["in"] == "header"
            assert param["required"] is False
            found.add(path.split("/commands/", 1)[1])
    assert found == (takes if surface == "app" else set())
    assert surface == "admin" or {"create", "apply", "update"} <= found


def test_public_routes_and_servers(surface: str) -> None:
    """Health and the error registry need no token; ``public_url`` gives ``servers``."""
    document = DOCUMENTS[surface]()
    for path in ("/v1/health", "/v1/errors", "/v1/errors/{code}"):
        assert document["paths"][path]["get"]["security"] == [], path
    assert "servers" not in document
    problem = document["components"]["schemas"]["problem"]
    assert {"type", "title", "status", "detail", "code", "exit_code"} <= set(problem["required"])
    assert app_document_with("https://graph.example.org")["servers"] == [
        {"url": "https://graph.example.org"}
    ]


def app_document_with(public_url: str) -> dict[str, Any]:
    from graph_cli.app_cli.commands import all_specs
    from graph_cli.app_cli.commands.meta import inventory
    from graph_cli_server.openapi import openapi

    found = openapi(
        inventory(all_specs(), output_schemas=True), version="0.0.0", public_url=public_url
    )
    validate(found)
    return found
