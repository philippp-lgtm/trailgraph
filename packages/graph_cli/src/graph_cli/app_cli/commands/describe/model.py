"""``graph describe types|modules|module|search|type|relations|schema|fields|lifecycle``: the
ontology side of a graph (contracts B11)."""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, GRAPH_ERRORS, command_line, next_page
from graph_cli.app_cli.commands.describe.shared import (
    MODULE_FILTER,
    MODULE_OPTION,
    describe_spec,
    function_call,
    open_view,
    visible_actions,
    visible_functions,
    visible_views,
)
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, STRINGS, model_document
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    obj,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.json_access import as_list, as_object, req_list, req_str
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT
from graph_cli.services import describe
from graph_cli.services.read import Page
from graph_cli.services.schema_search import SEARCH_TERM_KINDS, search_model

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
    MODULE_FILTER,
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
    MODULE_FILTER,
)


def describe_types(state: CommandState, module: str | None, graph: str | None) -> Result:
    view = open_view(state, graph, "describe types")
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
        section("Functions:", [f"  {function_call(c)}  {req_str(c, 'summary')}" for c in calls])
    if "actions" in item:
        runs = [as_object(v, "action") for v in req_list(item, "actions")]
        section("Actions:", [f"  {function_call(r)}  {req_str(r, 'summary')}" for r in runs])
    return "\n".join(lines) + "\n"


def describe_modules(state: CommandState, graph: str | None) -> Result:
    view = open_view(state, graph, "describe modules")
    data = describe.describe_modules(
        view.entry, view.model, visible_functions(state, view), visible_actions(state, view)
    )
    return Result(data=data, graph=view.envelope_graph(), text=_modules_text(data))


def describe_module(state: CommandState, module: str, graph: str | None) -> Result:
    view = open_view(state, graph, "describe module")
    data = describe.describe_module(
        view.entry, view.model, module, visible_functions(state, view), visible_actions(state, view)
    )
    return Result(data=data, graph=view.envelope_graph(), text=_module_text(data))


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
    view = open_view(state, graph, "describe search")
    data, total = search_model(
        view.entry,
        view.model,
        text,
        kinds=kind,
        module=module,
        limit=limit,
        offset=offset,
        functions=visible_functions(state, view),
        actions=visible_actions(state, view),
        views=visible_views(state, view),
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
    view = open_view(state, graph, "describe type")
    return Result(
        data=describe.describe_type(view.entry, view.model, type), graph=view.envelope_graph()
    )


def describe_relations(state: CommandState, module: str | None, graph: str | None) -> Result:
    view = open_view(state, graph, "describe relations")
    return Result(
        data=describe.describe_relations(view.entry, view.model, module=module),
        graph=view.envelope_graph(),
    )


def describe_fields(state: CommandState, type: str, graph: str | None) -> Result:
    view = open_view(state, graph, "describe fields")
    return Result(
        data=describe.describe_fields(view.entry, view.model, type), graph=view.envelope_graph()
    )


def describe_lifecycle(state: CommandState, type: str, graph: str | None) -> Result:
    view = open_view(state, graph, "describe lifecycle")
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
    view = open_view(state, graph, "describe schema")
    data = describe.describe_schema(view.entry, view.model, compact=compact, module=module)
    return Result(data=data, graph=view.envelope_graph(), text=_schema_text(data))


SPECS: Final = (
    describe_spec(
        "types",
        "List the types of the selected graph's ontology.",
        describe_types,
        with_type=False,
        output=model_document({"types": array(_TYPE_ITEM)}, MODULE_FILTER),
        examples=(
            Example("graph describe types", "All types with summaries."),
            Example("graph describe types --graph <graph> --json", "Types of one graph."),
            Example("graph describe types --module <module>", "Only the types of one module."),
        ),
        options=(MODULE_OPTION,),
    ),
    describe_spec(
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
    describe_spec(
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
            "Search the model by text: types, relations, fields, enum values, states, "
            "functions, actions, views."
        ),
        description=(
            "Looks at names, CURIEs, summaries and descriptions of the model, and at the "
            "names, summaries and purpose of the graph's functions, actions and views, never "
            "at graph data (that is graph search). Ignores case, accents and punctuation; "
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
            Example(
                "graph describe search <value> --kind view",
                "Only the published views that fit the words.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "term.unknown"),
        handler=describe_search,
        output=_SEARCH,
        limits={"limit": MAX_LIMIT},
    ),
    describe_spec(
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
    describe_spec(
        "relations",
        "List the relations of the selected graph's ontology.",
        describe_relations,
        with_type=False,
        output=model_document({"relations": array(_RELATION)}, MODULE_FILTER),
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
    describe_spec(
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
    describe_spec(
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
    describe_spec(
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
