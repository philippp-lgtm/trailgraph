"""``graph describe tools``: the tool manifest of a graph for an agent harness (tool manifest
plan TM1, TM3). The document is built by ``app_cli.tool_manifest``."""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, GRAPH_ERRORS, context
from graph_cli.app_cli.commands.describe.shared import (
    MODULE_OPTION,
    catalogs,
    open_view,
)
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, STRINGS, model_document
from graph_cli.app_cli.tool_manifest import TOOL_KINDS, Selection, build_manifest
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    OPEN,
    STRING,
    array,
    enum,
    nullable,
    obj,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.json_access import as_object, req_list, req_str
from graph_cli.services.describe_tools import KINDS
from graph_cli.services.tool_errors import ON_ERROR_VALUES

_ANNOTATIONS: Final = obj(
    {
        "read_only": BOOLEAN,
        "dry_run": BOOLEAN,
        "reason": nullable(enum("required", "optional")),
        "paging": nullable(enum("offset", "cursor")),
    }
)
# ``input_schema`` and ``output_schema`` are JSON Schema documents (2020-12), data here.
_TOOL: Final = obj(
    {
        "name": STRING,
        "kind": enum(*TOOL_KINDS),
        "entry": NULLABLE_STRING,
        "description": STRING,
        "input_schema": OPEN,
        "output_schema": OPEN,
        "annotations": _ANNOTATIONS,
        "status": enum("active", "deprecated"),
        "replaced_by": NULLABLE_STRING,
        "featured": BOOLEAN,
        "module": NULLABLE_STRING,
        "hash": STRING,
        "entry_hash": NULLABLE_STRING,
        "errors": STRINGS,
    }
)
_ERROR_INFO: Final = obj(
    {
        "category": STRING,
        "retryable": BOOLEAN,
        "summary": STRING,
        "on_error": enum(*ON_ERROR_VALUES),
    }
)
TOOLS_OUTPUT: Final = model_document(
    {
        "tools_format": INTEGER,
        "version": STRING,
        "tools": array(_TOOL),
        "errors": obj(extra=_ERROR_INFO),
    }
)


def _tools_text(data: JsonObject) -> str:
    """One line per tool: name, kind, module, the first line of its description."""
    lines = []
    for value in req_list(data, "tools"):
        tool = as_object(value, "tool")
        module = f"  [{tool['module']}]" if tool["module"] else ""
        deprecated = " (deprecated)" if tool["status"] == "deprecated" else ""
        first = req_str(tool, "description").partition("\n")[0]
        lines.append(f"{req_str(tool, 'name')}  {tool['kind']}{module}{deprecated}  {first}")
    return "\n".join(lines or ["-"]) + "\n"


def describe_tools(
    state: CommandState,
    tool: tuple[str, ...],
    kind: str | None,
    module: str | None,
    featured: bool,
    exclude_deprecated: bool,
    graph: str | None,
) -> Result:
    view = open_view(state, graph, "describe tools")
    found = catalogs(state, view)
    obligations = (
        context(state).reader.reason_obligations(view.actor, view.entry)
        if found.actions
        else frozenset[str]()
    )
    data = build_manifest(
        view.entry,
        view.model,
        functions=found.functions or (),
        actions=found.actions or (),
        views=found.views or (),
        obligations=obligations,
        selection=Selection(
            tools=tool,
            kinds=(kind,) if kind else (),
            module=module,
            featured=featured,
            exclude_deprecated=exclude_deprecated,
        ),
    )
    return Result(data=data, graph=view.envelope_graph(), text=_tools_text(data))


SPECS: Final = (
    CommandSpec(
        path=("describe", "tools"),
        kind="read",
        surface="model",
        action="read.describe",
        summary="List the functions, actions and views you may use as tools with JSON Schemas.",
        description=(
            "The tool manifest of the graph for an agent harness, rendered for you: one tool per "
            "function, action and view you may use, each with a JSON Schema (2020-12) of its "
            "input and of its answer, what it reads or writes, and the error codes it may "
            "return; errors tells what to do about each code. Enum values and classes are the "
            "ones you see. Deprecated entries are listed (status, replaced_by) unless "
            "--exclude-deprecated. --tool names the tools you want and refuses a name that is "
            "none of yours (tool.not_found with did_you_mean). version changes when anything "
            "listed changes; each tool's hash when that tool does."
        ),
        params=(
            ParamSpec(
                "--tool",
                "string",
                "Only this tool (run_NAME, do_NAME or view_NAME); repeat for several.",
                repeatable=True,
            ),
            ParamSpec(
                "--kind",
                "choice",
                "Only the tools of this kind of entry.",
                choices=KINDS,
            ),
            MODULE_OPTION,
            ParamSpec("--featured", "flag", "Only featured functions and actions (views stay)."),
            ParamSpec("--exclude-deprecated", "flag", "Leave out the tools of deprecated entries."),
            GRAPH,
        ),
        examples=(
            Example("graph describe tools --json", "Every tool you may use, with its schemas."),
            Example(
                "graph describe tools --tool run_<function> --tool do_<action> --json",
                "Only the tools a harness allows.",
            ),
            Example(
                "graph describe tools --kind function --featured --json",
                "The featured functions.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "term.unknown", "tool.not_found"),
        handler=describe_tools,
        output=TOOLS_OUTPUT,
        skill_table=False,  # for agent harnesses; shell agents have describe, run, do and view
    ),
)
