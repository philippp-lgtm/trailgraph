"""``graph-admin enroll create|list|revoke``: one-time codes for ``graph connect`` (B14.21)."""

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
from graph_cli_admin.maintenance_cli.output_schemas import ENROLLMENT
from graph_cli_admin.services_admin import enroll

REASON: Final = ParamSpec("--reason", "string", "Why; recorded in the audit.")


def create(
    state: CommandState,
    project: str,
    scope: str,
    expires_in: str | None,
    reason: str | None,
    data_root: str | None,
) -> Result:
    outcome = enroll.create(
        admin_context(state, data_root),
        project=project,
        scope=scope,
        expires_in=expires_in,
        reason=reason,
    )
    return Result(data=outcome.data, warnings=outcome.warnings, text=outcome.text)


def list_command(state: CommandState, data_root: str | None) -> Result:
    return Result(data=enroll.list_enrollments(admin_context(state, data_root)).data)


def revoke(state: CommandState, id: str, reason: str | None, data_root: str | None) -> Result:
    outcome = enroll.revoke(admin_context(state, data_root), enrollment_id=id, reason=reason)
    return Result(data=outcome.data)


# services_admin.enroll: the code only in the answer of create
CREATE_OUTPUT: Final = obj({**ENROLLMENT, "code": STRING})
LIST_OUTPUT: Final = obj({"enrollments": array(ref("enrollment"))})
REVOKE_OUTPUT: Final = obj({"id": STRING, "revoked": BOOLEAN, "hash_prefix": STRING})


SPECS: Final = (
    CommandSpec(
        path=("enroll", "create"),
        kind="write",
        surface="act",
        action="maint.token_create",
        summary="Create a one-time code that a client exchanges for a token of a project.",
        params=(
            ParamSpec(
                "--project",
                "string",
                "The project the token is bound to: it acts for it, verified.",
                required=True,
            ),
            ParamSpec(
                "--scope",
                "choice",
                "app: a token for the graph commands; admin: a project admin's token.",
                choices=SCOPES,
                default=APP_SCOPE,
            ),
            ParamSpec(
                "--expires-in",
                "string",
                "How long the code works: <n>m or <n>h, at most 24h; default 15m.",
            ),
            REASON,
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin enroll create --project <value>", "A code for one client."),
            Example(
                "graph-admin enroll create --project <value> --scope admin --expires-in <value>"
                " --json",
                "A code for a project admin, valid longer.",
            ),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS),
        handler=create,
        output=CREATE_OUTPUT,
        description="The code is shown only once and works once: graph connect <endpoint>"
        " --code <code> exchanges it for a token bound to the project and sets up the"
        " client's folder. Only a hash of the code is stored. create warns when the installed"
        " policy would give such a token nothing.",
    ),
    CommandSpec(
        path=("enroll", "list"),
        kind="read",
        surface="meta",
        action=None,
        summary="List the codes not used yet: id, project, scope, expiry, a hash prefix.",
        params=(DATA_ROOT,),
        examples=(
            Example("graph-admin enroll list", "Every unused code."),
            Example("graph-admin enroll list --json", "The codes as JSON."),
        ),
        errors=ROOT_ERRORS,
        handler=list_command,
        output=LIST_OUTPUT,
    ),
    CommandSpec(
        path=("enroll", "revoke"),
        kind="write",
        surface="act",
        action="maint.token_revoke",
        summary="Revoke an unused code; it can no longer be exchanged.",
        params=(
            ParamSpec(
                "id", "string", "The code's id from enroll list.", required=True, positional=True
            ),
            REASON,
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin enroll revoke <value>", "Revoke one code."),
            Example("graph-admin enroll revoke <value> --reason <value> --json", "With a reason."),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS, "admin.enrollment_not_found"),
        handler=revoke,
        output=REVOKE_OUTPUT,
    ),
)
