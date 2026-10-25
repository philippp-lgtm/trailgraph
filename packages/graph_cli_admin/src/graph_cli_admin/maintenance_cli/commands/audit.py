"""``graph-admin audit verify|anchor``."""

from __future__ import annotations

from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, array, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    ROOT_ERRORS,
    admin_context,
)
from graph_cli_admin.services_admin import audit


def verify(state: CommandState, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    verification = audit.verify(ctx)
    state.warnings += verification.warnings
    verification.raise_on_errors()
    return Result(data=verification.data)


def anchor(state: CommandState, data_root: str | None) -> Result:
    return Result(data=audit.anchor(admin_context(state, data_root)))


VERIFY_OUTPUT: Final = obj(  # services_admin.audit.verify
    {
        "counts": ref("finding_counts"),
        "graphs": array(obj({"id": STRING, "clean": BOOLEAN, "seq": INTEGER})),
        "recorded": BOOLEAN,
        "findings": array(ref("finding")),
    }
)
ANCHOR_OUTPUT: Final = obj(  # services_admin.audit.anchor; audit.events.anchor
    {
        "seq": INTEGER,
        "anchors": array(obj({"graph_id": STRING, "seq": INTEGER, "hash": STRING})),
    }
)


SPECS: Final = (
    CommandSpec(
        path=("audit", "verify"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="Check every audit chain, tx log, revision file and manifest pin.",
        params=(DATA_ROOT,),
        examples=(
            Example("graph-admin audit verify", "Verify the whole installation."),
            Example("graph-admin audit verify --json", "The findings as JSON."),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS, "admin.integrity_errors"),
        handler=verify,
        output=VERIFY_OUTPUT,
        description="Exits 1 (admin.integrity_errors) if any error finding exists; warnings"
        " and info findings (such as interrupted writes) do not fail.",
    ),
    CommandSpec(
        path=("audit", "anchor"),
        kind="write",
        surface="trace",
        action="maint.audit_anchor",
        summary="Record the current head of every graph stream in the installation stream.",
        params=(DATA_ROOT,),
        examples=(
            Example("graph-admin audit anchor", "Anchor every graph stream."),
            Example("graph-admin audit anchor --json", "The anchored heads as JSON."),
        ),
        errors=(*ROOT_ERRORS, *CHANGE_ERRORS),
        handler=anchor,
        output=ANCHOR_OUTPUT,
    ),
)
