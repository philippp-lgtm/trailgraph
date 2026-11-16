"""``graph whoami``, ``graph graphs`` and ``graph describe ...`` (contracts B2.10, B6.12, B11)."""

from __future__ import annotations

import json
from typing import Final

from graph_cli import __version__
from graph_cli.app_cli.commands.common import (
    GLOBAL_OPTIONS,
    GRAPH,
    PROGRAM,
    command_line,
    context,
    next_page,
)
from graph_cli.app_cli.output_schemas import DEFS, NULLABLE_STRING, STRINGS, model_document
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.output_schemas import (
    ANY,
    BOOLEAN,
    INTEGER,
    STRING,
    Schema,
    array,
    enum,
    obj,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_str
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT
from graph_cli.services import describe
from graph_cli.services.read import GraphRead, Page
from graph_cli.services.schema_search import SEARCH_TERM_KINDS, search_model

TYPE_ARG: Final = ParamSpec(
    "type", "term", "Type CURIE or unique local name.", required=True, positional=True
)
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
_DOC: Final = {"summary": STRING, "deprecated": BOOLEAN}
_RELATION_OUT: Final = obj(
    {
        "curie": STRING,
        "range": STRINGS,
        "cardinality": STRING,
        "inverse_label": NULLABLE_STRING,
        "qualifiers": STRINGS,
        **_DOC,
    }
)
_RELATION_IN: Final = obj(
    {
        "curie": STRING,
        "from": STRINGS,
        "cardinality": STRING,
        "on_target_inactive": STRING,
        "inverse_label": NULLABLE_STRING,
        **_DOC,
    }
)
_TYPE: Final = obj(
    {
        "curie": STRING,
        "abstract": BOOLEAN,
        "extends": NULLABLE_STRING,
        "module": STRING,
        "ancestors": STRINGS,
        "subtypes": STRINGS,
        "alias_prefix": NULLABLE_STRING,
        "summary": STRING,
        "description": NULLABLE_STRING,
        "deprecated": BOOLEAN,
        "fields": array(ref("field")),
        "lifecycles": array(ref("lifecycle")),
        "relations_out": array(_RELATION_OUT),
        "relations_in": array(_RELATION_IN),
        "rules": array(obj({"id": STRING, "severity": STRING, "when": STRING, **_DOC})),
    }
)
_RELATION: Final = obj(
    {
        "curie": STRING,
        "domain": STRINGS,
        "range": STRINGS,
        "cardinality": obj({"out": STRING, "in": STRING}),
        "characteristics": STRINGS,
        "on_target_inactive": STRING,
        "inverse_label": NULLABLE_STRING,
        "qualifiers": STRINGS,
        **_DOC,
    }
)
_PROPERTIES: Final = {  # absent with --compact
    "properties": array(obj({"name": STRING, "base": STRING, "many": BOOLEAN}))
}
MODULE_OPTION: Final = ParamSpec(
    "--module", "string", "Only the terms of this module (id or prefix); see describe modules."
)
MODULE_ARG: Final = ParamSpec(
    "module",
    "string",
    "Module id or prefix (see describe modules).",
    required=True,
    positional=True,
)
_MODULE_FILTER: Final = {"module": STRING}  # a filtered answer names the module it is limited to
_TYPE_ITEM: Final = obj({"curie": STRING, "abstract": BOOLEAN, "extends": NULLABLE_STRING, **_DOC})
_MODULE_LINK: Final = obj(
    {
        "relation": STRING,
        "module": STRING,
        "other_modules": STRINGS,
        "from": STRINGS,
        "to": STRINGS,
        **_DOC,
    }
)
_MODULE_HEAD: Final = {
    "id": STRING,
    "prefix": STRING,
    "version": STRING,
    "summary": STRING,
    "builtin": BOOLEAN,
}
_FUNCTION_COUNT: Final = {"functions": INTEGER}
_ACTION_COUNT: Final = {"actions": INTEGER}
_FUNCTION_ITEM: Final = {
    "name": STRING,
    "module": NULLABLE_STRING,
    "summary": STRING,
    "signature": STRING,
    "status": enum("active", "deprecated"),
}
_TOUCHED: Final = array(obj({"curie": STRING, "module": NULLABLE_STRING}))
_FUNCTION: Final = model_document(
    {
        "function": obj(
            {
                **_FUNCTION_ITEM,
                "use_when": STRING,
                "replaced_by": NULLABLE_STRING,
                "featured": BOOLEAN,
                "params": array(
                    obj(
                        {
                            "name": STRING,
                            "type": STRING,
                            "class": NULLABLE_STRING,
                            "enum": NULLABLE_STRING,
                            "many": BOOLEAN,
                            "default": ANY,
                            "summary": STRING,
                        }
                    )
                ),
                "returns": array(obj({"column": STRING, "summary": STRING})),
                "examples": array(obj({"question": STRING, "params": obj(extra=ANY)})),
                "touches": obj({"types": _TOUCHED, "relations": _TOUCHED}),
                "query": STRING,
                "hash": STRING,
            }
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
                "featured": BOOLEAN,
                "reason": enum("optional", "required"),
                "params": array(
                    obj(
                        {
                            "name": STRING,
                            "type": STRING,
                            "class": NULLABLE_STRING,
                            "enum": NULLABLE_STRING,
                            "many": BOOLEAN,
                            "default": ANY,
                            "summary": STRING,
                        },
                        {"optional": BOOLEAN},
                    )
                ),
                "preconditions": array(
                    obj({"id": STRING, "on": STRING, "summary": STRING, "expr": ANY})
                ),
                "examples": array(obj({"request": STRING})),
                "ops": array(obj(extra=ANY)),
                "touches": obj({"types": _TOUCHED, "relations": _TOUCHED}),
                "needs": array(obj({"action": STRING, "type": STRING})),
                "hash": STRING,
            }
        )
    }
)
_MODULES: Final = model_document(
    {
        "modules": array(
            obj(
                {
                    **_MODULE_HEAD,
                    "types": INTEGER,
                    "relations": INTEGER,
                    "links": array(obj({"module": STRING, "out": INTEGER, "in": INTEGER})),
                },
                {
                    **_FUNCTION_COUNT,  # only when the caller may see the graph's functions
                    **_ACTION_COUNT,  # likewise its actions
                },
            )
        )
    }
)
_MODULE: Final = model_document(
    {
        "module": obj(
            {
                **_MODULE_HEAD,
                "types": array(_TYPE_ITEM),
                "relations": array(
                    obj(
                        {
                            "curie": STRING,
                            "domain": STRINGS,
                            "range": STRINGS,
                            "cardinality": obj({"out": STRING, "in": STRING}),
                            **_DOC,
                        }
                    )
                ),
                "links_out": array(_MODULE_LINK),
                "links_in": array(_MODULE_LINK),
                "rules": array(obj({"id": STRING, "severity": STRING, **_DOC})),
            },
            {
                "functions": array(  # only when the caller may see the graph's functions
                    obj({"name": STRING, "summary": STRING, "signature": STRING})
                ),
                "actions": array(  # likewise its actions
                    obj({"name": STRING, "summary": STRING, "signature": STRING})
                ),
            },
        )
    }
)
_SEARCH: Final = model_document(
    {
        "text": STRING,
        "items": array(
            obj(
                {
                    "kind": enum(*SEARCH_TERM_KINDS),
                    "curie": STRING,
                    "of": NULLABLE_STRING,
                    "module": STRING,
                    "summary": NULLABLE_STRING,
                    "deprecated": BOOLEAN,
                    "match": obj(
                        {
                            "field": enum(
                                "name", "curie", "summary", "description", "use_when", "touches"
                            ),
                            "how": enum("exact", "prefix", "word", "contains", "phrase", "words"),
                        }
                    ),
                }
            )
        ),
    },
    _MODULE_FILTER,
)
_SCHEMA: Final = model_document(
    {
        "labels": array(
            obj(
                {"label": STRING, "type": STRING, "abstract": BOOLEAN, "supertypes": STRINGS},
                _PROPERTIES,
            )
        ),
        "relationships": array(
            obj({"pattern": STRING, "relation": STRING, "type": STRING}, _PROPERTIES)
        ),
        "node_fields": STRINGS,
        "relationship_fields": STRINGS,
    },
    _MODULE_FILTER,
)
GRAPH_ERRORS: Final = (
    "graph.not_selected",
    "graph.not_found",
    "graph.not_in_project",
    "config.invalid",
    "config.forbidden_key",
    "access.denied",
    "ontology.artifact_missing",
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


def _view(state: CommandState, graph: str | None, command: str) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    view = ctx.reader.open(
        ctx.actor, entry, "read.describe", RequestMeta.of(command, {"graph": entry.name})
    )
    state.warnings += view.warnings
    return view


def describe_types(state: CommandState, module: str | None, graph: str | None) -> Result:
    view = _view(state, graph, "describe types")
    return Result(
        data=describe.describe_types(view.entry, view.model, module=module),
        graph=view.envelope_graph(),
    )


def _count(n: JsonValue, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _modules_text(data: JsonObject) -> str:
    """One block per module: id, prefix, version, counts, summary and the modules it links to."""
    lines: list[str] = []
    for value in req_list(data, "modules"):
        item = as_object(value, "module")
        built = " (built-in)" if item["builtin"] else ""
        lines.append(
            f"{req_str(item, 'id')} [{req_str(item, 'prefix')}] {req_str(item, 'version')}{built}"
            f"  {_count(item['types'], 'type')}, {_count(item['relations'], 'relation')}"
            + (f", {_count(item['functions'], 'function')}" if "functions" in item else "")
            + (f", {_count(item['actions'], 'action')}" if "actions" in item else "")
        )
        lines.append(f"  {req_str(item, 'summary')}")
        links = [as_object(v, "link") for v in req_list(item, "links")]
        if links:
            lines.append(
                "  links: "
                + ", ".join(f"{req_str(k, 'module')} ({k['out']} out, {k['in']} in)" for k in links)
            )
    return "\n".join(lines) + "\n"


def _ends(item: JsonObject, key: str) -> str:
    return "|".join(str(c) for c in req_list(item, key))


def _link_line(link: JsonObject, module_id: str) -> str:
    """``from -rel-> to  [other modules]``; the declaring module only when it is neither end."""
    others = [str(m) for m in req_list(link, "other_modules")]
    where = ", ".join(others) or "any"
    owner = req_str(link, "module")
    if owner != module_id and owner not in others:
        where += f"; declared in {owner}"
    return f"  {_ends(link, 'from')} -{req_str(link, 'relation')}-> {_ends(link, 'to')}  [{where}]"


def _function_call(item: JsonObject) -> str:
    """``name(signature)``: how a function or an action is called."""
    return f"{req_str(item, 'name')}({req_str(item, 'signature')})"


def _module_text(data: JsonObject) -> str:
    """The module head, then its types, relations, cross-module links and rules; an empty
    section shows ``-``."""
    item = as_object(data["module"], "module")
    built = " (built-in)" if item["builtin"] else ""
    lines = [
        f"{req_str(item, 'id')} [{req_str(item, 'prefix')}] {req_str(item, 'version')}{built}",
        f"  {req_str(item, 'summary')}",
    ]

    def section(title: str, rows: list[str]) -> None:
        lines.append(title)
        lines.extend(rows or ["  -"])

    types = []
    for value in req_list(item, "types"):
        t = as_object(value, "type")
        extends = f" extends {t['extends']}" if t["extends"] else ""
        abstract = " (abstract)" if t["abstract"] else ""
        types.append(f"  {req_str(t, 'curie')}{abstract}{extends}  {req_str(t, 'summary')}")
    section("Types:", types)
    relations = []
    for value in req_list(item, "relations"):
        r = as_object(value, "relation")
        relations.append(
            f"  {_ends(r, 'domain')} -{req_str(r, 'curie')}-> {_ends(r, 'range')}"
            f"  {req_str(r, 'summary')}"
        )
    section("Relations inside the module:", relations)
    for key, title in (
        ("links_out", "Links to other modules:"),
        ("links_in", "Links from other modules:"),
    ):
        section(
            title,
            [_link_line(as_object(v, "link"), req_str(item, "id")) for v in req_list(item, key)],
        )
    rules = []
    for value in req_list(item, "rules"):
        rule = as_object(value, "rule")
        rules.append(
            f"  {req_str(rule, 'id')} ({req_str(rule, 'severity')})  {req_str(rule, 'summary')}"
        )
    section("Rules:", rules)
    if "functions" in item:
        calls = [as_object(v, "function") for v in req_list(item, "functions")]
        section("Functions:", [f"  {_function_call(c)}  {req_str(c, 'summary')}" for c in calls])
    if "actions" in item:
        runs = [as_object(v, "action") for v in req_list(item, "actions")]
        section("Actions:", [f"  {_function_call(r)}  {req_str(r, 'summary')}" for r in runs])
    return "\n".join(lines) + "\n"


def _functions(state: CommandState, view: GraphRead) -> tuple[FunctionEntry, ...] | None:
    """The graph's functions the caller may see (``read.function``), else ``None``."""
    return context(state).reader.functions(view.actor, view.entry)


def _actions(state: CommandState, view: GraphRead) -> tuple[ActionEntry, ...] | None:
    """The graph's actions the caller may run (all their write rights), else ``None``."""
    return context(state).reader.actions(view.actor, view.entry)


def describe_modules(state: CommandState, graph: str | None) -> Result:
    view = _view(state, graph, "describe modules")
    data = describe.describe_modules(
        view.entry, view.model, _functions(state, view), _actions(state, view)
    )
    return Result(data=data, graph=view.envelope_graph(), text=_modules_text(data))


def describe_module(state: CommandState, module: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe module")
    data = describe.describe_module(
        view.entry, view.model, module, _functions(state, view), _actions(state, view)
    )
    return Result(data=data, graph=view.envelope_graph(), text=_module_text(data))


def _functions_text(data: JsonObject) -> str:
    """One line per function: how it is called, module, summary."""
    lines = []
    for value in req_list(data, "functions"):
        item = as_object(value, "function")
        deprecated = " (deprecated)" if item["status"] == "deprecated" else ""
        module = f"  [{item['module']}]" if item["module"] else ""
        lines.append(f"{_function_call(item)}{module}{deprecated}  {req_str(item, 'summary')}")
    return "\n".join(lines or ["-"]) + "\n"


def describe_functions(
    state: CommandState, module: str | None, include_deprecated: bool, graph: str | None
) -> Result:
    view = _view(state, graph, "describe functions")
    data = describe.describe_functions(
        view.entry,
        view.model,
        _functions(state, view),
        module=module,
        include_deprecated=include_deprecated,
    )
    return Result(data=data, graph=view.envelope_graph(), text=_functions_text(data))


def _param_lines(item: JsonObject) -> list[str]:
    """The parameters of a function or an action, one line each (``-`` for none)."""
    lines = []
    for value in req_list(item, "params"):
        p = as_object(value, "param")
        default = "" if p["default"] is None else f" (default {json.dumps(p['default'])})"
        kind = {"ref": f"ref {p['class']}", "enum": f"enum {p['enum']}"}.get(
            str(p["type"]), str(p["type"])
        )
        kind = f"list of {kind}" if p["many"] else kind
        lines.append(f"  {req_str(p, 'name')}: {kind}{default}  {req_str(p, 'summary')}")
    return lines or ["  -"]


def _function_text(data: JsonObject) -> str:
    """The definition in reading order: head, parameters, columns, touched terms, query."""
    f = as_object(data["function"], "function")
    head = f"{_function_call(f)}  [{f['module']}]" if f["module"] else _function_call(f)
    lines = [head, f"  {req_str(f, 'summary')}", f"  Use when: {req_str(f, 'use_when')}"]
    if f["status"] == "deprecated":
        replaced = f"; use {f['replaced_by']}" if f["replaced_by"] else ""
        lines.append(f"  Deprecated{replaced}")
    lines.append("Parameters:")
    lines.extend(_param_lines(f))
    lines.append("Returns:")
    for value in req_list(f, "returns"):
        r = as_object(value, "column")
        lines.append(f"  {req_str(r, 'column')}  {req_str(r, 'summary')}")
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
    view = _view(state, graph, "describe function")
    data = describe.describe_function(view.entry, view.model, _functions(state, view), name)
    return Result(data=data, graph=view.envelope_graph(), text=_function_text(data))


def _actions_text(data: JsonObject) -> str:
    """One line per action: how it is called, module, summary."""
    lines = []
    for value in req_list(data, "actions"):
        item = as_object(value, "action")
        deprecated = " (deprecated)" if item["status"] == "deprecated" else ""
        module = f"  [{item['module']}]" if item["module"] else ""
        lines.append(f"{_function_call(item)}{module}{deprecated}  {req_str(item, 'summary')}")
    return "\n".join(lines or ["-"]) + "\n"


def describe_actions(
    state: CommandState, module: str | None, include_deprecated: bool, graph: str | None
) -> Result:
    view = _view(state, graph, "describe actions")
    data = describe.describe_actions(
        view.entry,
        view.model,
        _actions(state, view),
        module=module,
        include_deprecated=include_deprecated,
    )
    return Result(data=data, graph=view.envelope_graph(), text=_actions_text(data))


def _action_text(data: JsonObject) -> str:
    """The definition in reading order: head, parameters, preconditions, operations, needed
    rights, examples."""
    a = as_object(data["action"], "action")
    head = f"{_function_call(a)}  [{a['module']}]" if a["module"] else _function_call(a)
    lines = [head, f"  {req_str(a, 'summary')}", f"  Use when: {req_str(a, 'use_when')}"]
    if a["status"] == "deprecated":
        replaced = f"; use {a['replaced_by']}" if a["replaced_by"] else ""
        lines.append(f"  Deprecated{replaced}")
    if a["reason"] == "required":
        lines.append("  Requires --reason")
    lines.append("Parameters:")
    lines.extend(_param_lines(a))
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
    view = _view(state, graph, "describe action")
    data = describe.describe_action(view.entry, view.model, _actions(state, view), name)
    return Result(data=data, graph=view.envelope_graph(), text=_action_text(data))


def _search_text(data: JsonObject) -> str:
    """One line per hit: kind, CURIE, module, summary and how it matched."""
    lines = []
    for value in req_list(data, "items"):
        item = as_object(value, "hit")
        match = as_object(item["match"], "match")
        deprecated = " (deprecated)" if item["deprecated"] else ""
        summary = f"  {item['summary']}" if item["summary"] else ""
        lines.append(
            f"{req_str(item, 'kind')}  {req_str(item, 'curie')}  [{req_str(item, 'module')}]"
            f"{deprecated}{summary}  <{req_str(match, 'field')}: {req_str(match, 'how')}>"
        )
    return "\n".join(lines or ["-"]) + "\n"


def describe_search(
    state: CommandState,
    text: str,
    kind: tuple[str, ...],
    module: str | None,
    limit: int,
    offset: int,
    graph: str | None,
) -> Result:
    view = _view(state, graph, "describe search")
    data, total = search_model(
        view.entry,
        view.model,
        text,
        kinds=kind,
        module=module,
        limit=limit,
        offset=offset,
        functions=_functions(state, view),
        actions=_actions(state, view),
    )
    page = Page(total, len(req_list(data, "items")), limit, offset).to_json()
    options = (("--kind", kind), ("--module", (module,) if module else ()))

    def line(next_offset: int) -> str:
        return command_line(
            "describe search", (text,), view.entry.name, options, (), limit, next_offset
        )

    hints = next_page(page, line)
    if not total and (kind or module):
        hints = (
            {
                "text": "Nothing found; search every kind and module",
                "command": command_line(
                    "describe search", (text,), view.entry.name, (), (), DEFAULT_LIMIT
                ),
            },
        )
    return Result(
        data=data, graph=view.envelope_graph(), page=page, hints=hints, text=_search_text(data)
    )


def describe_type(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe type")
    return Result(
        data=describe.describe_type(view.entry, view.model, type), graph=view.envelope_graph()
    )


def describe_relations(state: CommandState, module: str | None, graph: str | None) -> Result:
    view = _view(state, graph, "describe relations")
    return Result(
        data=describe.describe_relations(view.entry, view.model, module=module),
        graph=view.envelope_graph(),
    )


def describe_fields(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe fields")
    return Result(
        data=describe.describe_fields(view.entry, view.model, type), graph=view.envelope_graph()
    )


def describe_lifecycle(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe lifecycle")
    return Result(
        data=describe.describe_lifecycle(view.entry, view.model, type), graph=view.envelope_graph()
    )


def _schema_text(data: JsonObject) -> str:
    """Pattern lines, then each label with its properties: what a query may write."""

    def names(item: JsonObject) -> list[str]:
        listed = as_list(item.get("properties", []), "properties")  # absent with --compact
        return [req_str(as_object(p, "property"), "name") for p in listed]

    lines = ["Relationships:"]
    for value in req_list(data, "relationships"):
        item = as_object(value, "relationship")
        props = names(item)
        lines.append(
            f"  {req_str(item, 'pattern')}" + (f"  {{{', '.join(props)}}}" if props else "")
        )
    lines.append("Labels:")
    for value in req_list(data, "labels"):
        item = as_object(value, "label")
        lines.append(f"  (:{req_str(item, 'label')})  {', '.join(names(item)) or '-'}")
    fields = ", ".join(str(f) for f in req_list(data, "node_fields"))
    lines.append(f"Every node also has: {fields}; every relationship: edge_id.")
    return "\n".join(lines) + "\n"


def describe_schema(
    state: CommandState, compact: bool, module: str | None, graph: str | None
) -> Result:
    view = _view(state, graph, "describe schema")
    data = describe.describe_schema(view.entry, view.model, compact=compact, module=module)
    return Result(data=data, graph=view.envelope_graph(), text=_schema_text(data))


def _describe(
    name: str,
    summary: str,
    handler: object,
    *,
    with_type: bool,
    with_module: bool = False,
    examples: tuple[Example, ...],
    output: Schema,
    options: tuple[ParamSpec, ...] = (),
) -> CommandSpec:
    params = (TYPE_ARG, *options, GRAPH) if with_type else (*options, GRAPH)
    if with_module:
        params = (MODULE_ARG, GRAPH)
    errors = (
        (*GRAPH_ERRORS, "term.unknown", "query.ambiguous_term")
        if with_type
        else (*GRAPH_ERRORS, "term.unknown")
        if with_module or MODULE_OPTION in options
        else GRAPH_ERRORS
    )
    return CommandSpec(
        path=("describe", name),
        kind="read",
        surface="model",
        action="read.describe",
        summary=summary,
        params=params,
        examples=examples,
        errors=errors,
        handler=handler,  # type: ignore[arg-type]
        output=output,
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
    _describe(
        "types",
        "List the types of the selected graph's ontology.",
        describe_types,
        with_type=False,
        output=model_document({"types": array(_TYPE_ITEM)}, _MODULE_FILTER),
        examples=(
            Example("graph describe types", "All types with summaries."),
            Example("graph describe types --graph <graph> --json", "Types of one graph."),
            Example("graph describe types --module <module>", "Only the types of one module."),
        ),
        options=(MODULE_OPTION,),
    ),
    _describe(
        "modules",
        "List the modules (subject areas) of the graph's ontology and how they link.",
        describe_modules,
        with_type=False,
        output=_MODULES,
        examples=(
            Example("graph describe modules", "The subject areas: what each holds, how they link."),
            Example("graph describe modules --json", "The modules as graph_cli.model/1."),
        ),
    ),
    _describe(
        "module",
        "Describe one module: its types, relations, links to other modules and rules.",
        describe_module,
        with_type=False,
        with_module=True,
        output=_MODULE,
        examples=(
            Example("graph describe module <module>", "Everything one subject area holds."),
            Example("graph describe module <module> --json", "The module as graph_cli.model/1."),
        ),
    ),
    CommandSpec(
        path=("describe", "search"),
        kind="read",
        surface="model",
        action="read.describe",
        summary=(
            "Search the ontology by text: types, relations, fields, enum values, states, "
            "functions, actions."
        ),
        description=(
            "Looks at names, CURIEs, summaries and descriptions of the model, and at the "
            "names, summaries and purpose of the graph's functions and actions, never at "
            "graph data (that is graph search). Ignores case, accents and punctuation; "
            "best matches first."
        ),
        params=(
            ParamSpec("text", "string", "The words to look for.", required=True, positional=True),
            ParamSpec(
                "--kind",
                "choice",
                "Only this kind of term.",
                repeatable=True,
                choices=SEARCH_TERM_KINDS,
            ),
            MODULE_OPTION,
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
        ),
        examples=(
            Example("graph describe search <value>", "Terms of the model that match the words."),
            Example(
                "graph describe search <value> --kind attribute --module <module> --json",
                "Only fields, only in one module.",
            ),
            Example(
                "graph describe search <value> --kind function",
                "Only the named functions that fit the words.",
            ),
            Example(
                "graph describe search <value> --kind action",
                "Only the actions that fit the words.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "term.unknown"),
        handler=describe_search,
        output=_SEARCH,
        limits={"limit": MAX_LIMIT},
    ),
    _describe(
        "functions",
        "List the named functions of the graph with their parameters.",
        describe_functions,
        with_type=False,
        output=model_document({"functions": array(obj(_FUNCTION_ITEM))}, _MODULE_FILTER),
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
            "ad-hoc query. Only callers who may read functions see them."
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
    _describe(
        "actions",
        "List the actions of the graph with their parameters.",
        describe_actions,
        with_type=False,
        output=model_document({"actions": array(obj(_ACTION_ITEM))}, _MODULE_FILTER),
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
            "to, the types and relations it touches and the write rights it needs. Only "
            "callers who may run an action see it."
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
    _describe(
        "type",
        "Describe one type: fields, lifecycles, relations and rules.",
        describe_type,
        with_type=True,
        output=model_document({"type": _TYPE}),
        examples=(
            Example("graph describe type <type>", "Everything about one type."),
            Example("graph describe type <type> --json", "The type as graph_cli.model/1."),
        ),
    ),
    _describe(
        "relations",
        "List the relations of the selected graph's ontology.",
        describe_relations,
        with_type=False,
        output=model_document({"relations": array(_RELATION)}, _MODULE_FILTER),
        examples=(
            Example("graph describe relations", "All relations."),
            Example("graph describe relations --json", "Relations as JSON."),
            Example(
                "graph describe relations --module <module>",
                "The relations that declare or touch one module.",
            ),
        ),
        options=(MODULE_OPTION,),
    ),
    _describe(
        "schema",
        "Show the model as graph query sees it: labels, relationship patterns, properties.",
        describe_schema,
        with_type=False,
        output=_SCHEMA,
        examples=(
            Example("graph describe schema", "Patterns and labels to write a query with."),
            Example("graph describe schema --json", "The schema as graph_cli.model/1."),
            Example("graph describe schema --compact --json", "Labels and patterns only."),
            Example(
                "graph describe schema --module <module> --compact",
                "Patterns of one module and its links, no properties.",
            ),
        ),
        options=(
            ParamSpec("--compact", "flag", "Only labels and relationship patterns, no properties."),
            MODULE_OPTION,
        ),
    ),
    _describe(
        "fields",
        "List the fields of one type with datatypes and limits.",
        describe_fields,
        with_type=True,
        output=model_document({"type": STRING, "fields": array(ref("field"))}),
        examples=(
            Example("graph describe fields <type>", "Fields of one type."),
            Example("graph describe fields <type> --json", "Fields as JSON."),
        ),
    ),
    _describe(
        "lifecycle",
        "Show the lifecycles of a type, or one lifecycle by name.",
        describe_lifecycle,
        with_type=True,
        output=model_document({"lifecycles": array(ref("lifecycle"))}, {"type": STRING}),
        examples=(
            Example("graph describe lifecycle <type>", "States and transitions of a type."),
            Example("graph describe lifecycle <type> --json", "Lifecycles as JSON."),
        ),
    ),
)
