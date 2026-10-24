"""``graph-admin token create|list|revoke``: bearer tokens of the server API (B14.11)."""

from __future__ import annotations

from typing import Final

from graph_cli.catalog.tokens import APP_SCOPE, SCOPES
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    ROOT_ERRORS,
    admin_context,
)
from graph_cli_admin.services_admin import tokens

NAME: Final = ParamSpec(
    "name", "string", "Token name: lowercase letters, digits, _ . -", required=True, positional=True
)
REASON: Final = ParamSpec("--reason", "string", "Why; recorded in the audit.")


def create(
    state: CommandState,
    name: str,
    project: str | None,
    expires: str | None,
    scope: str,
    reason: str | None,
    data_root: str | None,
) -> Result:
    outcome = tokens.create(
        admin_context(state, data_root),
        name=name,
        project=project,
        expires=expires,
        reason=reason,
        scope=scope,
    )
    return Result(data=outcome.data, warnings=outcome.warnings, text=outcome.text)


def list_command(state: CommandState, data_root: str | None) -> Result:
    return Result(data=tokens.list_tokens(admin_context(state, data_root)).data)


def revoke(state: CommandState, name: str, reason: str | None, data_root: str | None) -> Result:
    outcome = tokens.revoke(admin_context(state, data_root), name=name, reason=reason)
    return Result(data=outcome.data)


SPECS: Final = (
    CommandSpec(
        path=("token", "create"),
        kind="write",
        surface="act",
        action="maint.token_create",
        summary="Create a bearer token for the server API; the token is shown only once.",
        params=(
            NAME,
            ParamSpec(
                "--project",
                "string",
                "Bind the token to a project: requests then act for it, verified; an admin"
                " token then maintains only the graphs the project's scope says maintain for.",
            ),
            ParamSpec("--expires", "string", "Last valid day, YYYY-MM-DD (UTC)."),
            ParamSpec(
                "--scope",
                "choice",
                "app: the graph commands of the API; admin: the graph-admin commands.",
                choices=SCOPES,
                default=APP_SCOPE,
            ),
            REASON,
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin token create <value>", "A token for one client."),
            Example(
                "graph-admin token create <value> --project <value> --expires <value> --json",
                "A token bound to a project, valid until a date.",
            ),
            Example("graph-admin token create <value> --scope admin", "A token for the admin API."),
            Example(
                "graph-admin token create <value> --scope admin --project <value>",
                "A project admin: maintains the graphs the project's scope says maintain for.",
            ),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS, "admin.token_exists"),
        handler=create,
        description="Only a hash of the token is stored. The installed policy must grant"
        " the server entrypoint (or this token) a level; create warns when it grants nothing."
        " An admin token acts as a maintainer and must be one of the policy's maintainers;"
        " bound to a project, it is a project admin instead and needs no maintainers entry.",
    ),
    CommandSpec(
        path=("token", "list"),
        kind="read",
        surface="meta",
        action=None,
        summary="List the tokens: name, scope, project, expiry, creation time, a hash prefix.",
        params=(DATA_ROOT,),
        examples=(
            Example("graph-admin token list", "Every token."),
            Example("graph-admin token list --json", "The tokens as JSON."),
        ),
        errors=ROOT_ERRORS,
        handler=list_command,
    ),
    CommandSpec(
        path=("token", "revoke"),
        kind="write",
        surface="act",
        action="maint.token_revoke",
        summary="Revoke a token; the server refuses it from the next request on.",
        params=(NAME, REASON, DATA_ROOT),
        examples=(
            Example("graph-admin token revoke <value>", "Revoke one token."),
            Example("graph-admin token revoke <value> --reason <value> --json", "With a reason."),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS, "admin.token_not_found"),
        handler=revoke,
    ),
)
