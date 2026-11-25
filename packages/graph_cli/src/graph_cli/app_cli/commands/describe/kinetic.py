"""``graph describe functions|function|actions|action|views|view``: the kinetic side of a graph
(contracts B11)."""

from __future__ import annotations

import json
from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, GRAPH_ERRORS, PROGRAM
from graph_cli.app_cli.commands.describe.shared import (
    ACCESS_MARKS,
    INCLUDE_NOT_ALLOWED,
    MODULE_FILTER,
    MODULE_OPTION,
    NOT_ALLOWED_COUNT,
    access_lines,
    describe_spec,
    function_call,
    item_mark,
    not_allowed_line,
    open_view,
    split_actions,
    split_functions,
    split_views,
)
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, STRINGS, model_document
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    ANY,
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    nullable,
    obj,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_str
from graph_cli.kinetic.return_types import COLUMN_KINDS
from graph_cli.services import describe

_FUNCTION_ITEM: Final = {
    "name": STRING,
    "module": NULLABLE_STRING,
    "summary": STRING,
    "signature": STRING,
    "status": enum("active", "deprecated"),
}
_TOUCHED: Final = array(obj({"curie": STRING, "module": NULLABLE_STRING}))
# A parameter as a caller who may use the entry sees it; one who may not gets the first six keys and
# ``required`` (no default value, no example value): ``services.describe.restricted_detail``.
_PARAM: Final = obj(
    {
        "name": STRING,
        "type": STRING,
        "class": NULLABLE_STRING,
        "enum": NULLABLE_STRING,
        "many": BOOLEAN,
        "summary": STRING,
    },
    {"default": ANY, "optional": BOOLEAN, "required": BOOLEAN},
)
# A result column of a function: its type against the caller's model (``type`` null: nothing makes
# it certain), the class of a ``ref`` or an ``entity``, the enum of an ``enum``, a list or not and
# whether a value may be null.
_COLUMN: Final = obj(
    {
        "column": STRING,
        "summary": STRING,
        "type": nullable(enum(*COLUMN_KINDS)),
        "class": NULLABLE_STRING,
        "enum": NULLABLE_STRING,
        "many": BOOLEAN,
        "nullable": BOOLEAN,
    }
)
# What the definition of an entry that may not be used leaves out (it is optional here, and always
# there for an entry that may be used).
_FUNCTION: Final = model_document(
    {
        "function": obj(
            {
                **_FUNCTION_ITEM,
                "use_when": STRING,
                "replaced_by": NULLABLE_STRING,
                "params": array(_PARAM),
            },
            {
                "featured": BOOLEAN,
                "returns": array(_COLUMN),
                "examples": array(obj({"question": STRING, "params": obj(extra=ANY)})),
                "touches": obj({"types": _TOUCHED, "relations": _TOUCHED}),
                "query": STRING,
                "hash": STRING,
                **ACCESS_MARKS,
            },
        )
    }
)
_ACTION_ITEM: Final = {
    "name": STRING,
    "module": NULLABLE_STRING,
    "summary": STRING,
    "signature": STRING,
    "status": enum("active", "deprecated"),
}
_ACTION: Final = model_document(
    {
        "action": obj(
            {
                **_ACTION_ITEM,
                "use_when": STRING,
                "replaced_by": NULLABLE_STRING,
                "reason": enum("optional", "required"),
                "params": array(_PARAM),
            },
            {
                "featured": BOOLEAN,
                "preconditions": array(
                    obj({"id": STRING, "on": STRING, "summary": STRING, "expr": ANY})
                ),
                "examples": array(obj({"request": STRING})),
                "ops": array(obj(extra=ANY)),
                "touches": obj({"types": _TOUCHED, "relations": _TOUCHED}),
                "needs": array(obj({"action": STRING, "type": STRING})),
                "hash": STRING,
                **ACCESS_MARKS,
            },
        )
    }
)
_VIEW_ITEM: Final = {
    "name": STRING,
    "module": NULLABLE_STRING,
    "summary": STRING,
    "signature": STRING,
    "status": enum("active", "deprecated"),
}
_VIEW_ITEM_OPTIONAL: Final = {"takes": STRING, "default": BOOLEAN}  # entity views / defaults only
_LISTED: Final = {**MODULE_FILTER, **NOT_ALLOWED_COUNT}
_VIEW: Final = model_document(
    {
        "view": obj(
            {
                **_VIEW_ITEM,
                "use_when": STRING,
                "replaced_by": NULLABLE_STRING,
                "takes": NULLABLE_STRING,  # null: a set view
                "default": BOOLEAN,
                "params": array(_PARAM),
            },
            {
                "max_bytes": nullable(INTEGER),
                "page_bytes": nullable(INTEGER),
                "sections": array(obj({"kind": STRING, "id": STRING}, extra=ANY)),
                "examples": array(obj({"question": STRING, "params": obj(extra=ANY)})),
                "touches": obj(
                    {
                        "types": _TOUCHED,
                        "relations": _TOUCHED,
                        "attributes": STRINGS,
                        "functions": STRINGS,
                    }
                ),
                "call": STRING,
                "hash": STRING,
                **ACCESS_MARKS,
            },
        )
    }
)


def _functions_text(data: JsonObject) -> str:
    """One line per function: how it is called, module, summary."""
    lines = []
    for value in req_list(data, "functions"):
        item = as_object(value, "function")
        deprecated = " (deprecated)" if item["status"] == "deprecated" else ""
        module = f"  [{item['module']}]" if item["module"] else ""
        lines.append(
            f"{function_call(item)}{module}{deprecated}  {req_str(item, 'summary')}"
            f"{item_mark(item)}"
        )
    return "\n".join([*(lines or ["-"]), *not_allowed_line(data)]) + "\n"


def describe_functions(
    state: CommandState,
    module: str | None,
    include_deprecated: bool,
    include_not_allowed: bool,
    graph: str | None,
) -> Result:
    view = open_view(state, graph, "describe functions")
    allowed, denied = split_functions(state, view)
    data = describe.describe_functions(
        view.entry,
        view.model,
        allowed,
        module=module,
        include_deprecated=include_deprecated,
        denied=denied,
        include_not_allowed=include_not_allowed,
    )
    return Result(data=data, graph=view.envelope_graph(), text=_functions_text(data))


def _param_lines(item: JsonObject) -> list[str]:
    """The parameters of a function or an action, one line each (``-`` for none)."""
    lines = []
    for value in req_list(item, "params"):
        p = as_object(value, "param")
        default = "" if p.get("default") is None else f" (default {json.dumps(p['default'])})"
        kind = {"ref": f"ref {p['class']}", "enum": f"enum {p['enum']}"}.get(
            str(p["type"]), str(p["type"])
        )
        kind = f"list of {kind}" if p["many"] else kind
        lines.append(f"  {req_str(p, 'name')}: {kind}{default}  {req_str(p, 'summary')}")
    return lines or ["  -"]


def _column_head(column: JsonObject) -> str:
    """``name`` of an untyped column, ``name: type`` of a typed one (``list of date``), with
    ``(not null)`` where a value is never null."""
    kind = column.get("type")
    if kind is None:
        return req_str(column, "column")
    named = {"ref": column.get("class"), "entity": column.get("class"), "enum": column.get("enum")}
    text = f"{kind} {named[str(kind)]}" if named.get(str(kind)) else str(kind)
    text = f"list of {text}" if column.get("many") else text
    return f"{req_str(column, 'column')}: {text}{'' if column.get('nullable') else ' (not null)'}"


def _function_text(data: JsonObject) -> str:
    """The definition in reading order: head, parameters, columns, touched terms, query."""
    f = as_object(data["function"], "function")
    head = f"{function_call(f)}  [{f['module']}]" if f["module"] else function_call(f)
    lines = [head, *access_lines(f)]
    lines += [f"  {req_str(f, 'summary')}", f"  Use when: {req_str(f, 'use_when')}"]
    if f["status"] == "deprecated":
        replaced = f"; use {f['replaced_by']}" if f["replaced_by"] else ""
        lines.append(f"  Deprecated{replaced}")
    lines.append("Parameters:")
    lines.extend(_param_lines(f))
    if f.get("allowed") is False:  # what it returns and runs is for who may run it
        return "\n".join(lines) + "\n"
    lines.append("Returns:")
    for value in req_list(f, "returns"):
        r = as_object(value, "column")
        lines.append(f"  {_column_head(r)}  {req_str(r, 'summary')}")
    lines.append("Examples:")
    for value in req_list(f, "examples"):
        lines.append(f"  {req_str(as_object(value, 'example'), 'question')}")
    touches = as_object(f["touches"], "touches")
    for key, title in (("types", "Types"), ("relations", "Relations")):
        curies = [req_str(as_object(v, "term"), "curie") for v in req_list(touches, key)]
        lines.append(f"{title} touched: {', '.join(curies) or '-'}")
    lines.append("Query:")
    lines.extend(f"  {row}" for row in req_str(f, "query").splitlines())
    lines.append(f"Hash: {req_str(f, 'hash')}")
    return "\n".join(lines) + "\n"


def describe_function(state: CommandState, name: str, graph: str | None) -> Result:
    view = open_view(state, graph, "describe function")
    allowed, denied = split_functions(state, view)
    data = describe.describe_function(view.entry, view.model, allowed, name, denied)
    return Result(data=data, graph=view.envelope_graph(), text=_function_text(data))


def _actions_text(data: JsonObject) -> str:
    """One line per action: how it is called, module, summary."""
    lines = []
    for value in req_list(data, "actions"):
        item = as_object(value, "action")
        deprecated = " (deprecated)" if item["status"] == "deprecated" else ""
        module = f"  [{item['module']}]" if item["module"] else ""
        lines.append(
            f"{function_call(item)}{module}{deprecated}  {req_str(item, 'summary')}"
            f"{item_mark(item)}"
        )
    return "\n".join([*(lines or ["-"]), *not_allowed_line(data)]) + "\n"


def describe_actions(
    state: CommandState,
    module: str | None,
    include_deprecated: bool,
    include_not_allowed: bool,
    graph: str | None,
) -> Result:
    view = open_view(state, graph, "describe actions")
    allowed, denied = split_actions(state, view)
    data = describe.describe_actions(
        view.entry,
        view.model,
        allowed,
        module=module,
        include_deprecated=include_deprecated,
        denied=denied,
        include_not_allowed=include_not_allowed,
    )
    return Result(data=data, graph=view.envelope_graph(), text=_actions_text(data))


def _action_text(data: JsonObject) -> str:
    """The definition in reading order: head, parameters, preconditions, operations, needed
    rights, examples."""
    a = as_object(data["action"], "action")
    head = f"{function_call(a)}  [{a['module']}]" if a["module"] else function_call(a)
    lines = [head, *access_lines(a)]
    lines += [f"  {req_str(a, 'summary')}", f"  Use when: {req_str(a, 'use_when')}"]
    if a["status"] == "deprecated":
        replaced = f"; use {a['replaced_by']}" if a["replaced_by"] else ""
        lines.append(f"  Deprecated{replaced}")
    if a["reason"] == "required":
        lines.append("  Requires --reason")
    lines.append("Parameters:")
    lines.extend(_param_lines(a))
    if a.get("allowed") is False:  # what it checks and writes is for who may run it
        return "\n".join(lines) + "\n"
    lines.append("Preconditions:")
    for value in req_list(a, "preconditions"):
        c = as_object(value, "precondition")
        lines.append(f"  {req_str(c, 'id')} on {req_str(c, 'on')}: {req_str(c, 'summary')}")
    if not req_list(a, "preconditions"):
        lines.append("  -")
    lines.append("Operations:")
    lines.extend(f"  {json.dumps(op, sort_keys=True)}" for op in req_list(a, "ops"))
    lines.append("Needs:")
    for value in req_list(a, "needs"):
        n = as_object(value, "need")
        lines.append(f"  {req_str(n, 'action')} on {req_str(n, 'type')}")
    if not req_list(a, "needs"):
        lines.append("  -")
    lines.append("Examples:")
    for value in req_list(a, "examples"):
        lines.append(f"  {req_str(as_object(value, 'example'), 'request')}")
    lines.append(f"Hash: {req_str(a, 'hash')}")
    return "\n".join(lines) + "\n"


def describe_action(state: CommandState, name: str, graph: str | None) -> Result:
    view = open_view(state, graph, "describe action")
    allowed, denied = split_actions(state, view)
    data = describe.describe_action(view.entry, view.model, allowed, name, denied)
    return Result(data=data, graph=view.envelope_graph(), text=_action_text(data))


def _views_text(data: JsonObject) -> str:
    """One line per view: how it is called, module, what it takes, summary."""
    lines = []
    for value in req_list(data, "views"):
        item = as_object(value, "view")
        deprecated = " (deprecated)" if item["status"] == "deprecated" else ""
        module = f"  [{item['module']}]" if item["module"] else ""
        takes = f" takes {item['takes']}" if "takes" in item else " set view"
        default = ", default" if item.get("default") else ""
        lines.append(
            f"{function_call(item)}{module}{deprecated}  ({takes.strip()}{default})"
            f"  {req_str(item, 'summary')}{item_mark(item)}"
        )
    return "\n".join([*(lines or ["-"]), *not_allowed_line(data)]) + "\n"


def describe_views(
    state: CommandState,
    module: str | None,
    include_deprecated: bool,
    include_not_allowed: bool,
    graph: str | None,
) -> Result:
    view = open_view(state, graph, "describe views")
    allowed, denied = split_views(state, view)
    data = describe.describe_views(
        view.entry,
        view.model,
        allowed,
        module=module,
        include_deprecated=include_deprecated,
        denied=denied,
        include_not_allowed=include_not_allowed,
    )
    return Result(data=data, graph=view.envelope_graph(), text=_views_text(data))


def _view_call(item: JsonObject, graph: str) -> str:
    """How to call a view: the required parameters are given, the optional ones in brackets;
    ``REF`` and ``VALUE`` stand for what the caller supplies."""
    parts = [PROGRAM, "view"]
    if item["takes"] is not None:
        parts.append("REF")
    parts += ["--graph", graph, "--name", req_str(item, "name")]
    optional: list[str] = []
    for value in req_list(item, "params"):
        p = as_object(value, "param")
        option = f"--param {req_str(p, 'name')}=VALUE"
        if p["default"] is None:
            parts.append(option)
        else:
            optional.append(f"[{option}]")
    return " ".join([*parts, *optional])


def _section_line(section: JsonObject) -> str:
    """``id  kind  what it reads``."""
    kind = req_str(section, "kind")
    if kind == "list":
        what = ", ".join(str(t) for t in req_list(section, "type"))
    elif kind == "neighbors":
        what = f"{req_str(section, 'relation')} ({req_str(section, 'direction')})"
    elif kind == "rows":
        what = req_str(section, "function")
    else:
        what = ""
    title = f'  "{section["title"]}"' if section.get("title") else ""
    return f"  {req_str(section, 'id')}  {kind}{'  ' + what if what else ''}{title}"


def _names(value: JsonValue) -> str:
    """A field selection as the JSON has it: ``all`` or the attribute names."""
    return value if isinstance(value, str) else ", ".join(str(n) for n in as_list(value, "fields"))


def _relation_entry(value: JsonValue) -> str:
    """One relation of a section as the JSON has it: ``all``, or the relation with its direction
    and, when it lists attributes of the neighbours, them."""
    item = as_object(value, "relation")
    if item["relation"] is None:
        return "all"
    text = f"{req_str(item, 'relation')} ({req_str(item, 'direction')})"
    fields = item.get("fields")
    return text if fields is None else f"{text} with {_names(fields)}"


def _section_detail(section: JsonObject) -> list[str]:
    """The fields and relations a section lists, one line each, when it names any."""
    lines = []
    if section.get("fields") is not None:
        lines.append(f"    fields: {_names(section['fields'])}")
    relations = section.get("relations")
    if relations:
        entries = [_relation_entry(r) for r in as_list(relations, "relations")]
        lines.append(f"    relations: {'; '.join(entries)}")
    return lines


def _view_text(data: JsonObject) -> str:
    """The definition in reading order: head, parameters, sections, examples, touched terms,
    how to call it."""
    v = as_object(data["view"], "view")
    head = f"{function_call(v)}  [{v['module']}]" if v["module"] else function_call(v)
    flavour = f"takes {v['takes']}" if v["takes"] is not None else "set view"
    if v["default"]:
        flavour += ", default"
    lines = [head, *access_lines(v)]
    lines += [f"  {req_str(v, 'summary')}", f"  Use when: {req_str(v, 'use_when')}"]
    lines.append(f"  {flavour}")
    if v["status"] == "deprecated":
        replaced = f"; use {v['replaced_by']}" if v["replaced_by"] else ""
        lines.append(f"  Deprecated{replaced}")
    lines.append("Parameters:")
    lines.extend(_param_lines(v))
    if v.get("allowed") is False:  # what it reads is for who may call it
        return "\n".join(lines) + "\n"
    lines.append("Sections:")
    for value in req_list(v, "sections"):
        section = as_object(value, "section")
        lines.append(_section_line(section))
        lines.extend(_section_detail(section))
    lines.append("Examples:")
    for value in req_list(v, "examples"):
        lines.append(f"  {req_str(as_object(value, 'example'), 'question')}")
    if not req_list(v, "examples"):
        lines.append("  -")
    touches = as_object(v["touches"], "touches")
    for key, title in (("types", "Types"), ("relations", "Relations")):
        curies = [req_str(as_object(t, "term"), "curie") for t in req_list(touches, key)]
        lines.append(f"{title} touched: {', '.join(curies) or '-'}")
    functions = [str(f) for f in req_list(touches, "functions")]
    lines.append(f"Functions run: {', '.join(functions) or '-'}")
    lines.append(f"Call: {req_str(v, 'call')}")
    lines.append(f"Hash: {req_str(v, 'hash')}")
    return "\n".join(lines) + "\n"


def describe_view(state: CommandState, name: str, graph: str | None) -> Result:
    view = open_view(state, graph, "describe view")
    allowed, denied = split_views(state, view)
    data = describe.describe_view(view.entry, view.model, allowed, name, denied)
    item = as_object(data["view"], "view")
    if item.get("allowed") is not False:  # how to call it is for who may
        data = {**data, "view": {**item, "call": _view_call(item, view.entry.name)}}
    return Result(data=data, graph=view.envelope_graph(), text=_view_text(data))


SPECS: Final = (
    describe_spec(
        "functions",
        "List the named functions of the graph with their parameters.",
        describe_functions,
        with_type=False,
        output=model_document({"functions": array(obj(_FUNCTION_ITEM, ACCESS_MARKS))}, _LISTED),
        examples=(
            Example("graph describe functions", "The functions: name, parameters, summary."),
            Example(
                "graph describe functions --module <module> --include-deprecated",
                "One module's functions, deprecated ones too.",
            ),
        ),
        options=(
            MODULE_OPTION,
            ParamSpec("--include-deprecated", "flag", "Also list deprecated functions."),
            INCLUDE_NOT_ALLOWED,
        ),
    ),
    CommandSpec(
        path=("describe", "function"),
        kind="read",
        surface="model",
        action="read.describe",
        summary="Describe one function: parameters, columns, touched terms and its query.",
        description=(
            "Shows what a function takes and returns, the types and relations its query "
            "touches and the normalized query text, which can be copied as the start of an "
            "ad-hoc query. A function the caller sees but may not run (no read.function) is shown "
            "marked allowed: false with what is missing."
        ),
        params=(
            ParamSpec("name", "string", "The function name.", required=True, positional=True),
            GRAPH,
        ),
        examples=(
            Example("graph describe function <function>", "Everything about one function."),
            Example(
                "graph describe function <function> --json", "The function as graph_cli.model/1."
            ),
        ),
        errors=(*GRAPH_ERRORS, "function.not_found"),
        handler=describe_function,
        output=_FUNCTION,
    ),
    describe_spec(
        "actions",
        "List the actions of the graph with their parameters.",
        describe_actions,
        with_type=False,
        output=model_document({"actions": array(obj(_ACTION_ITEM, ACCESS_MARKS))}, _LISTED),
        examples=(
            Example("graph describe actions", "The actions: name, parameters, summary."),
            Example(
                "graph describe actions --module <module> --include-deprecated",
                "One module's actions, deprecated ones too.",
            ),
        ),
        options=(
            MODULE_OPTION,
            ParamSpec("--include-deprecated", "flag", "Also list deprecated actions."),
            INCLUDE_NOT_ALLOWED,
        ),
    ),
    CommandSpec(
        path=("describe", "action"),
        kind="read",
        surface="model",
        action="read.describe",
        summary="Describe one action: parameters, preconditions, operations and needed rights.",
        description=(
            "Shows what an action takes, the conditions it checks, the operations it expands "
            "to, the types and relations it touches and the write rights it needs. An action "
            "the caller sees but may not run is shown marked allowed: false with what is "
            "missing."
        ),
        params=(
            ParamSpec("name", "string", "The action name.", required=True, positional=True),
            GRAPH,
        ),
        examples=(
            Example("graph describe action <action>", "Everything about one action."),
            Example("graph describe action <action> --json", "The action as graph_cli.model/1."),
        ),
        errors=(*GRAPH_ERRORS, "action.not_found"),
        handler=describe_action,
        output=_ACTION,
    ),
    describe_spec(
        "views",
        "List the published views of the graph with their parameters.",
        describe_views,
        with_type=False,
        output=model_document(
            {"views": array(obj(_VIEW_ITEM, {**_VIEW_ITEM_OPTIONAL, **ACCESS_MARKS}))}, _LISTED
        ),
        examples=(
            Example(
                "graph describe views", "The views: name, parameters, what they take, summary."
            ),
            Example(
                "graph describe views --module <module> --include-deprecated",
                "One module's views, deprecated ones too.",
            ),
        ),
        options=(
            MODULE_OPTION,
            ParamSpec("--include-deprecated", "flag", "Also list deprecated views."),
            INCLUDE_NOT_ALLOWED,
        ),
    ),
    CommandSpec(
        path=("describe", "view"),
        kind="read",
        surface="model",
        action="read.describe",
        summary="Describe one view: what it takes, its parameters and sections and how to call it.",
        description=(
            "Shows what a published view takes (an entity of a class, or nothing: a set view), "
            "its parameters, each section and what it reads, the types, relations and "
            "functions it touches and the command that calls it (graph view). A view is shown "
            "only to callers who see everything it names; one they may not use (no read.find "
            "or read.function) is marked allowed: false with what is missing."
        ),
        params=(
            ParamSpec("name", "string", "The view name.", required=True, positional=True),
            GRAPH,
        ),
        examples=(
            Example("graph describe view <view>", "Everything about one view."),
            Example("graph describe view <view> --json", "The view as graph_cli.model/1."),
        ),
        errors=(*GRAPH_ERRORS, "view.not_found"),
        handler=describe_view,
        output=_VIEW,
    ),
)
