"""``graph do``: run a published action, a named write the maintainer reviewed (actions plan A5).

``--param name=value`` is read by the type the action declares (repeat a name for a list);
a reference takes an id, alias or trailing hex digits, never a name. The service expands the
action in the state it finds under the graph lock, so ``--dry-run`` shows which edges an
``all`` unlink would remove before anything is written. The answer is the write result of
the change set the action made, plus the action's name and hash.
"""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, context
from graph_cli.app_cli.commands.write import WRITE_ERRORS, check_reason_and_key
from graph_cli.app_cli.output_schemas import WRITE_RESULT
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import STRING, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.query.plan import split_assignment
from graph_cli.services.read import graph_json


def do(
    state: CommandState,
    name: str,
    param: tuple[str, ...],
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    pairs = [split_assignment(text, "--param") for text in param]
    check_reason_and_key(reason, idempotency_key)
    ctx = context(state)
    entry = ctx.select(graph)
    result = ctx.actions.do(
        ctx.actor,
        entry,
        name,
        pairs,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    state.warnings += (*result.warnings, *result.write.warnings)
    return Result(data=result.to_json(), graph=graph_json(entry, result.write.seq))


DO_OUTPUT: Final = obj({**WRITE_RESULT, "action": obj({"name": STRING, "hash": STRING})})
DO_ERRORS: Final = (
    *WRITE_ERRORS,
    "action.not_found",
    "action.parameter_missing",
    "action.parameter_unknown",
    "action.parameter_invalid",
    "action.parameter_type",
    "action.precondition_failed",
    "action.nothing_to_do",
    "action.reason_required",
    "action.stale",
    "actions.slot_invalid",
)

SPECS: Final = (
    CommandSpec(
        path=("do",),
        kind="write",
        surface="act",
        action=None,  # each op is authorized by its own action (B6.3)
        summary="Run a named action of the graph: a reviewed write with typed parameters.",
        description=(
            "An action is a reviewed, named write the maintainer published; find one with "
            "describe search --kind action. Each --param is read by the type the action "
            "declares (repeat a name for a list). A reference parameter takes an id, alias or "
            "trailing hex digits of an entity of the declared class, never a name: look the "
            "name up first. Run with --dry-run first to see what it changes: it checks the "
            "action's preconditions in the current state and shows every entity it would "
            "write. The answer is like the other writes', plus the action's name and hash."
        ),
        params=(
            ParamSpec("name", "string", "The action name.", required=True, positional=True),
            ParamSpec(
                "--param",
                "assignment",
                "name=value for a parameter of the action, read by its declared type.",
                repeatable=True,
                pairs="multimap",
            ),
            GRAPH,
            ParamSpec(
                "--dry-run", "flag", "Check the preconditions and show the result without writing."
            ),
            ParamSpec("--reason", "string", "Why this change is made (some actions require it)."),
            ParamSpec("--idempotency-key", "string", "Key that makes a retry safe."),
        ),
        examples=(
            Example(
                "graph do <action> --param key=<value> --dry-run --json", "See what it changes."
            ),
            Example("graph do <action> --param key=<value> --json", "Run it."),
            Example(
                "graph do <action> --param key=<id> --reason <value> --json",
                "Run an action that needs a reason.",
            ),
        ),
        errors=DO_ERRORS,
        handler=do,
        output=DO_OUTPUT,
    ),
)
