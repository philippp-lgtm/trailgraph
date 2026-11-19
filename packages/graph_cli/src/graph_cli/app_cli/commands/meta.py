"""``graph whoami``, ``graph graphs`` and ``graph describe commands`` (contracts B2.10, B6.12,
B11)."""

from __future__ import annotations

from typing import Final

from graph_cli import __version__
from graph_cli.app_cli.commands.common import GLOBAL_OPTIONS, PROGRAM, context
from graph_cli.app_cli.output_schemas import DEFS, NULLABLE_STRING, STRINGS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.output_schemas import BOOLEAN, INTEGER, STRING, array, enum, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import RequestMeta

_OBLIGATIONS: Final = obj(  # access_control.effective.write_obligations
    extra=obj(optional={"require_reason": BOOLEAN, "max_batch": INTEGER})
)
_VERB_RIGHT: Final = {"anyOf": [{"const": True}, array(STRING)]}  # true | the allowed terms
WHOAMI_OUTPUT: Final = obj(
    {
        "actor": ref("actor"),
        "permissions": obj(
            {
                "policy": STRING,
                "policy_hash": STRING,
                "scope": NULLABLE_STRING,
                "allowed": STRINGS,
                "obligations": _OBLIGATIONS,
                "graphs": array(
                    obj(
                        {
                            "graph": STRING,
                            "level": enum("none", "read", "write"),
                            "withheld": STRINGS,
                            "types": obj(extra=STRING),
                            "obligations": _OBLIGATIONS,
                        },
                        {
                            # access_control.effective.write_allowance:
                            # {type: {verb: true | [term]}}
                            "writes": obj(extra=obj(extra=_VERB_RIGHT)),
                            # access_control.effective.visibility: the visible concrete types
                            "reads": STRINGS,
                            # access_control.effective.marking_allowance: the markings held,
                            # when the graph's model uses some the caller lacks
                            "markings": STRINGS,
                        },
                    )
                ),
            }
        ),
    }
)
GRAPHS_OUTPUT: Final = obj(
    {
        "items": array(
            obj(
                {
                    "id": STRING,
                    "name": STRING,
                    "state": STRING,
                    "ontology": ref("ontology_ref"),
                    "title": NULLABLE_STRING,
                    "language": NULLABLE_STRING,
                }
            )
        ),
        "hidden_count": INTEGER,
    }
)


def whoami(state: CommandState) -> Result:
    ctx = context(state)  # builds nothing on disk; works without a data root
    allowed = ctx.pointer.graphs if ctx.pointer is not None else None
    return Result(data=ctx.reader.whoami(ctx.actor, allowed=allowed))


def graphs(state: CommandState) -> Result:
    ctx = context(state)
    allowed = ctx.pointer.graphs if ctx.pointer is not None else None
    items, hidden, warnings = ctx.reader.graphs(
        ctx.actor, RequestMeta.of("graphs", {}), allowed=allowed
    )
    state.warnings += warnings
    return Result(data={"items": items, "hidden_count": hidden})


def describe_commands(state: CommandState, output_schemas: bool) -> Result:
    from graph_cli.app_cli.commands import all_specs

    return Result(data=inventory(all_specs(), output_schemas=output_schemas))


def inventory(
    specs: tuple[CommandSpec, ...], *, output_schemas: bool = False
) -> dict[str, JsonValue]:
    """The app inventory; ``output_schemas`` adds the schemas of the answers (B11.10)."""
    return build_inventory(
        specs,
        program=PROGRAM,
        version=__version__,
        surface="app",
        global_options=GLOBAL_OPTIONS,
        remote=True,
        output_defs=DEFS if output_schemas else None,
    )


SPECS: Final = (
    CommandSpec(
        path=("whoami",),
        kind="meta",
        surface="meta",
        action=None,
        summary="Show who is acting: the identity facets and what the policy allows.",
        params=(),
        examples=(
            Example("graph whoami", "Show the acting identity."),
            Example("graph whoami --json", "The identity and permissions as JSON."),
        ),
        errors=("config.invalid", "backend.unavailable"),
        handler=whoami,
        output=WHOAMI_OUTPUT,
    ),
    CommandSpec(
        path=("graphs",),
        kind="read",
        surface="retrieve",
        action="read.list",
        summary="List the graphs this project may use.",
        params=(),
        examples=(
            Example("graph graphs", "List the visible graphs."),
            Example(
                "graph graphs --json",
                "The graphs as JSON with titles and languages, and the hidden count.",
            ),
        ),
        errors=("config.invalid", "config.forbidden_key", "access.denied"),
        handler=graphs,
        output=GRAPHS_OUTPUT,
    ),
    CommandSpec(
        path=("describe", "commands"),
        kind="meta",
        surface="meta",
        action=None,
        summary="Describe every command of this program (graph_cli.inventory/1).",
        params=(
            ParamSpec(
                "--output-schemas",
                "flag",
                "Add the JSON Schema of every command's data and their shared $defs.",
            ),
        ),
        examples=(
            Example("graph describe commands --json", "The command inventory."),
            Example("graph describe commands", "The inventory as text."),
            Example(
                "graph describe commands --output-schemas --json",
                "The inventory with the schema of every answer.",
            ),
        ),
        errors=(),
        handler=describe_commands,
        remote="client",
        output=ref("inventory"),
    ),
)
