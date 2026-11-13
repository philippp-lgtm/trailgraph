"""``graph-admin token create|list|revoke|role add|role remove``: bearer tokens of the server
API and their roles (B14.11)."""

from __future__ import annotations

from typing import Final

from graph_cli.catalog.tokens import APP_SCOPE, SCOPES
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, STRING, array, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    ROOT_ERRORS,
    admin_context,
)
from graph_cli_admin.maintenance_cli.output_schemas import STRINGS, TOKEN
from graph_cli_admin.services_admin import tokens

NAME: Final = ParamSpec(
    "name", "string", "Token name: lowercase letters, digits, _ . -", required=True, positional=True
)
REASON: Final = ParamSpec("--reason", "string", "Why; recorded in the audit.")
ROLES: Final = ParamSpec(
    "roles",
    "string",
    "Roles, declared by the installed installation policy when added.",
    required=True,
    repeatable=True,
    positional=True,
)


def create(
    state: CommandState,
    name: str,
    project: str | None,
    expires: str | None,
    scope: str,
    role: tuple[str, ...],
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
        roles=role,
    )
    return Result(data=outcome.data, warnings=outcome.warnings, text=outcome.text)


def list_command(state: CommandState, role: str | None, data_root: str | None) -> Result:
    return Result(data=tokens.list_tokens(admin_context(state, data_root), role=role).data)


def revoke(state: CommandState, name: str, reason: str | None, data_root: str | None) -> Result:
    outcome = tokens.revoke(admin_context(state, data_root), name=name, reason=reason)
    return Result(data=outcome.data)


def role_add(
    state: CommandState,
    name: str,
    roles: tuple[str, ...],
    reason: str | None,
    data_root: str | None,
) -> Result:
    outcome = tokens.update_roles(
        admin_context(state, data_root), name=name, roles=roles, add=True, reason=reason
    )
    return Result(data=outcome.data, warnings=outcome.warnings)


def role_remove(
    state: CommandState,
    name: str,
    roles: tuple[str, ...],
    reason: str | None,
    data_root: str | None,
) -> Result:
    outcome = tokens.update_roles(
        admin_context(state, data_root), name=name, roles=roles, add=False, reason=reason
    )
    return Result(data=outcome.data, warnings=outcome.warnings)


# services_admin.tokens: the secret only in the answer of create
CREATE_OUTPUT: Final = obj({**TOKEN, "token": STRING})
LIST_OUTPUT: Final = obj({"tokens": array(ref("token"))})
REVOKE_OUTPUT: Final = obj({"name": STRING, "revoked": BOOLEAN, "hash_prefix": STRING})
# ``roles`` is the full set after the change; ``changed`` is false when nothing changed
ROLE_OUTPUT: Final = obj(
    {"name": STRING, "roles": STRINGS, "changed": BOOLEAN, "hash_prefix": STRING}
)
ROLE_PARAM: Final = ParamSpec(
    "--role", "string", "A role of the token; repeatable.", repeatable=True, keyword="role"
)


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
            ROLE_PARAM,
            REASON,
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin token create <value>", "A token for one client."),
            Example(
                "graph-admin token create <value> --project <value> --expires <value> --json",
                "A token bound to a project, valid until a date.",
            ),
            Example(
                "graph-admin token create <value> --project <value> --role <value> --role <value>",
                "An app token that holds roles the policy grants and denies by.",
            ),
            Example("graph-admin token create <value> --scope admin", "A token for the admin API."),
            Example(
                "graph-admin token create <value> --scope admin --project <value>",
                "A project admin: maintains the graphs the project's scope says maintain for.",
            ),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS, "admin.token_exists", "admin.role_unknown"),
        handler=create,
        output=CREATE_OUTPUT,
        description="Only a hash of the token is stored. The installed policy must grant"
        " the server entrypoint (or this token) a level; create warns when it grants nothing."
        " An admin token acts as a maintainer and must be one of the policy's maintainers;"
        " bound to a project, it is a project admin instead and needs no maintainers entry."
        " Roles are for app tokens only and must be declared by the installed installation"
        " policy.",
    ),
    CommandSpec(
        path=("token", "list"),
        kind="read",
        surface="meta",
        action=None,
        summary="List the tokens: name, scope, project, roles, expiry, creation time, hash prefix.",
        params=(
            ParamSpec("--role", "string", "Only the tokens that hold this role."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin token list", "Every token."),
            Example("graph-admin token list --role <value>", "The tokens that hold a role."),
            Example("graph-admin token list --json", "The tokens as JSON."),
        ),
        errors=ROOT_ERRORS,
        handler=list_command,
        output=LIST_OUTPUT,
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
        output=REVOKE_OUTPUT,
    ),
    CommandSpec(
        path=("token", "role", "add"),
        kind="write",
        surface="act",
        action="maint.token_update",
        summary="Give an app token roles; a request with it is decided by them from the next on.",
        params=(NAME, ROLES, REASON, DATA_ROOT),
        examples=(
            Example("graph-admin token role add <value> <value>", "Give a token one role."),
            Example(
                "graph-admin token role add <value> <value> <value> --reason <value> --json",
                "Give it two, with a reason.",
            ),
        ),
        errors=(
            *ROOT_ERRORS,
            *CHANGE_ERRORS,
            "admin.token_not_found",
            "admin.role_unknown",
        ),
        handler=role_add,
        output=ROLE_OUTPUT,
        description="Every role must be declared by the installed installation policy. A role"
        " the token holds already changes nothing: no event, changed is false. Admin tokens"
        " take no roles.",
    ),
    CommandSpec(
        path=("token", "role", "remove"),
        kind="write",
        surface="act",
        action="maint.token_update",
        summary="Take roles from an app token; the next request with it no longer has them.",
        params=(NAME, ROLES, REASON, DATA_ROOT),
        examples=(
            Example("graph-admin token role remove <value> <value>", "Take one role away."),
            Example(
                "graph-admin token role remove <value> <value> --reason <value> --json",
                "With a reason.",
            ),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS, "admin.token_not_found"),
        handler=role_remove,
        output=ROLE_OUTPUT,
        description="A role the policy no longer declares can still be removed. A role the"
        " token does not hold changes nothing: no event, changed is false.",
    ),
)
