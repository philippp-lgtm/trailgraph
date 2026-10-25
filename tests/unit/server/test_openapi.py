"""U: the OpenAPI documents of the server API (contracts B14.2, B14.5, B14.22 G4).

Both documents pass the OpenAPI 3.1 validator, every reference resolves, every command
answers the envelope with its declared ``data`` and lists each code it declares under
every status that code reaches. The goldens show every change of the API in review.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
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
            assert response["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/failed_envelope"
            }
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
