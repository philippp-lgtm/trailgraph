"""``graph whoami``, ``graph graphs`` and ``graph describe ...`` (contracts B2.10, B6.12, B11)."""

from __future__ import annotations

from typing import Final

from graph_cli import __version__
from graph_cli.app_cli.commands.common import (
    GLOBAL_OPTIONS,
    GRAPH,
    PROGRAM,
    context,
)
from graph_cli.app_cli.output_schemas import DEFS, NULLABLE_STRING, STRINGS, model_document
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.inventory import build_inventory
from graph_cli.cli_kit.output_schemas import (
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
from graph_cli.services import describe
from graph_cli.services.read import GraphRead

TYPE_ARG: Final = ParamSpec(
    "type", "term", "Type CURIE or unique local name.", required=True, positional=True
)
_OBLIGATIONS: Final = obj(  # access_control.effective.write_obligations
    extra=obj(optional={"require_reason": BOOLEAN, "max_batch": INTEGER})
)
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
                        }
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
    }
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


def describe_types(state: CommandState, graph: str | None) -> Result:
    view = _view(state, graph, "describe types")
    return Result(data=describe.describe_types(view.entry, view.model), graph=view.envelope_graph())


def describe_type(state: CommandState, type: str, graph: str | None) -> Result:
    view = _view(state, graph, "describe type")
    return Result(
        data=describe.describe_type(view.entry, view.model, type), graph=view.envelope_graph()
    )


def describe_relations(state: CommandState, graph: str | None) -> Result:
    view = _view(state, graph, "describe relations")
    return Result(
        data=describe.describe_relations(view.entry, view.model), graph=view.envelope_graph()
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


def describe_schema(state: CommandState, compact: bool, graph: str | None) -> Result:
    view = _view(state, graph, "describe schema")
    data = describe.describe_schema(view.entry, view.model, compact=compact)
    return Result(data=data, graph=view.envelope_graph(), text=_schema_text(data))


def _describe(
    name: str,
    summary: str,
    handler: object,
    *,
    with_type: bool,
    examples: tuple[Example, ...],
    output: Schema,
    options: tuple[ParamSpec, ...] = (),
) -> CommandSpec:
    params = (TYPE_ARG, *options, GRAPH) if with_type else (*options, GRAPH)
    errors = (*GRAPH_ERRORS, "term.unknown", "query.ambiguous_term") if with_type else GRAPH_ERRORS
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
        output=model_document(
            {
                "types": array(
                    obj({"curie": STRING, "abstract": BOOLEAN, "extends": NULLABLE_STRING, **_DOC})
                )
            }
        ),
        examples=(
            Example("graph describe types", "All types with summaries."),
            Example("graph describe types --graph <graph> --json", "Types of one graph."),
        ),
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
        output=model_document({"relations": array(_RELATION)}),
        examples=(
            Example("graph describe relations", "All relations."),
            Example("graph describe relations --json", "Relations as JSON."),
        ),
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
        ),
        options=(
            ParamSpec("--compact", "flag", "Only labels and relationship patterns, no properties."),
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
