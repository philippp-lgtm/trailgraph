"""The server's OpenAPI documents, built as its routes build them, and a validator for an
answer against one response of a document."""

from typing import Any

from jsonschema import Draft202012Validator

from graph_cli.app_cli.commands import all_specs as app_specs
from graph_cli.app_cli.commands.meta import inventory as app_inventory
from graph_cli_admin.maintenance_cli import hints
from graph_cli_admin.maintenance_cli.commands import all_specs as admin_specs
from graph_cli_admin.maintenance_cli.commands.meta import inventory as admin_inventory
from graph_cli_admin.services_admin import error_codes
from graph_cli_server.admin_api import COMMANDS, served
from graph_cli_server.openapi import openapi
from support.output_schemas import closed

# The goldens do not change with the package version.
VERSION = "0.0.0"


def app_document(version: str = VERSION) -> dict[str, Any]:
    return openapi(app_inventory(app_specs(), output_schemas=True), version=version)


def admin_document(version: str = VERSION) -> dict[str, Any]:
    error_codes.register()  # as AdminApi does
    hints.register()
    declared = tuple(served(spec) for spec in admin_specs())
    return openapi(
        admin_inventory(declared, output_schemas=True),
        version=version,
        commands=COMMANDS,
        admin=True,
    )


def answer_validator(
    document: dict[str, Any], path: str, method: str, status: str
) -> Draft202012Validator:
    """The JSON schema of one response, closed like the suite's checks; its references
    resolve against the document's components."""
    response = document["paths"][path][method]["responses"][status]
    schema = response["content"]["application/json"]["schema"]
    return Draft202012Validator(closed({**schema, "components": document["components"]}))
