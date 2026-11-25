"""``graph get`` (one entity) and ``graph view`` (an object view): the handlers, the parameters of
``view``, the output schemas and the specs."""

from __future__ import annotations

from typing import Final

from graph_cli.app_cli.commands.common import AS_OF, GRAPH, context
from graph_cli.app_cli.commands.read.shared import (
    FIELDS,
    FULL,
    REF,
    REF_ERRORS,
    as_of_flag,
    open_graph,
    pinned,
)
from graph_cli.app_cli.commands.view_text import view_hints, view_text
from graph_cli.app_cli.output_schemas import STRINGS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import ANY, INTEGER, obj, ref
from graph_cli.cli_kit.runner import CommandState, missing_argument
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.plan import split_assignment
from graph_cli.services.read import refuse_affordances_with_rev


def get(
    state: CommandState,
    ref: str,
    rev: int | None,
    fields: tuple[str, ...],
    full: bool,
    affordances: bool,
    graph: str | None,
    as_of: str | None,
) -> Result:
    if affordances and rev is not None:
        raise refuse_affordances_with_rev()
    point = as_of_flag(as_of)
    args: dict[str, JsonValue] = {"ref": ref} if rev is None else {"ref": ref, "rev": rev}
    if affordances:
        args["affordances"] = True  # only when set: the log and audit args stay as they were
    if full:
        args["full"] = True
    view = open_graph(state, graph, "read.get", "get", args, point)
    ctx = context(state)
    if rev is not None and point is None:
        # An earlier revision is history: read.get and read.history (B8.15); with
        # --as-of, opening the view authorized both.
        request = RequestMeta.of("get", {"graph": view.entry.name, **args})
        ctx.reader.authorize(
            ctx.actor, "read.history", f"graph:{view.entry.id}", request, view.entry
        )
    data, warnings = ctx.reader.get(
        view, ref, rev=rev, fields=fields, affordances=affordances, full=full
    )
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


def view(
    state: CommandState,
    ref: str | None,
    name: str | None,
    param: tuple[str, ...],
    cursor: str | None,
    page_bytes: int | None,
    graph: str | None,
    as_of: str | None,
) -> Result:
    """An object view: the generated card of one entity (stage 1), a published entity view, or a
    published set view paged by bytes (stage 2). The request names the view and the parameter
    names, never their values."""
    point = as_of_flag(as_of)
    if ref is None and name is None:  # REF was required: the answer is what it was
        context(state).select(graph)  # a graph that is not there is reported first, as before
        raise missing_argument("ref")
    pairs = [split_assignment(text, "--param") for text in param]
    args: dict[str, JsonValue] = {}
    if ref is not None:  # only what was given: the request of ``graph view REF`` is as it was
        args["ref"] = ref
    if name is not None:
        args["name"] = name
    if pairs:
        args["param"] = sorted({key for key, _ in pairs})
    reading = open_graph(state, graph, "read.get", "view", args, point)
    data, warnings = context(state).reader.view(
        reading,
        ref,
        name=name,
        pairs=pairs,
        cursor=cursor,
        page_bytes=page_bytes,
        as_of=point is not None,
    )
    state.warnings += warnings
    return Result(
        data=data,
        graph=reading.envelope_graph(),
        hints=view_hints(
            data,
            reading.entry.name,
            name=name,
            params=param,
            page_bytes=page_bytes,
            pinned=pinned(reading, point),
        ),
        text=view_text(data),
    )


# ``graph view``: the entity is optional (a set view has none), the rest picks a published view.
VIEW_REF: Final = ParamSpec(
    "ref",
    "entity_ref",
    "Full id, alias or trailing hex of the id; leave out for a set view.",
    positional=True,
)


VIEW_NAME: Final = ParamSpec(
    "--name",
    "string",
    "A published view of the graph; card is the generated card, which is also the default.",
)


VIEW_PARAM: Final = ParamSpec(
    "--param",
    "assignment",
    "name=value for a parameter of the view, read by its declared type.",
    repeatable=True,
    pairs="multimap",
)


VIEW_CURSOR: Final = ParamSpec(
    "--cursor", "string", "The next of the previous page of a set view; same --name and --param."
)


VIEW_PAGE_BYTES: Final = ParamSpec(
    "--page-bytes",
    "integer",
    "Page size of a set view in bytes (1 to 262144); the view's own size, else 12288.",
)


GET_OUTPUT: Final = obj(
    {
        "entity": ref("entity"),
        "current_rev": INTEGER,
        "superseded_by": STRINGS,
        "retracted_by": STRINGS,
    },
    {"affordances": obj(extra=ANY)},  # only with --affordances
)


VIEW_OUTPUT: Final = ref("view_model")  # kinetic.card.ViewModel.to_json
VIEW_ERRORS: Final = (
    *REF_ERRORS,
    "usage.missing_argument",
    "usage.invalid_argument",
    "view.not_found",
    "view.takes_entity",
    "view.takes_no_entity",
    "view.class_mismatch",
    "view.stale",
    "view.parameter_missing",
    "view.parameter_unknown",
    "view.parameter_invalid",
    "view.parameter_type",
    "view.cursor_invalid",
    "view.cursor_stale",
)


SPECS: Final = (
    CommandSpec(
        path=("get",),
        kind="read",
        surface="retrieve",
        action="read.get",
        summary="Show one entity by id, alias or id suffix.",
        description=(
            "With --rev, the entity as it was at that revision; that also needs the "
            "permission to read history. --affordances adds what the current state allows; "
            "transitions and relations are model-level (not filtered by write rights), actions "
            "and functions are listed as describe actions and describe functions list them."
        ),
        params=(
            REF,
            ParamSpec("--rev", "integer", "Show this earlier revision instead of the current one."),
            FIELDS,
            FULL,
            ParamSpec(
                "--affordances",
                "flag",
                "Also show what the entity can do next: lifecycle transitions with their guards,"
                " relations with room left, and the actions and functions that take it as a"
                " parameter. Not with --rev.",
            ),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph get <id>", "Show one entity."),
            Example(
                "graph get <id> --affordances --json", "The entity and what applies to it next."
            ),
            Example("graph get <id> --graph <graph> --json", "The entity as JSON."),
            Example("graph get <id> --rev 2 --json", "The entity as it was at revision 2."),
            Example("graph get <id> --fields <attr>,edges --json", "Only one field and the edges."),
            Example("graph get <id> --full --json", "With the system fields and the edge ids."),
            Example(
                "graph get <id> --as-of tx:<seq> --json", "The entity as it was after that tx."
            ),
        ),
        errors=(*REF_ERRORS, "entity.revision_not_found"),
        handler=get,
        output=GET_OUTPUT,
    ),
    CommandSpec(
        path=("view",),
        kind="read",
        surface="retrieve",
        action="read.get",
        summary="Show what is known about one entity, or a published view, in one answer.",
        description=(
            "Without --name, the default view of the entity's class if the graph published "
            "one, else the generated card: key fields (label, lifecycle state, required and "
            "unique attributes; the other attributes with a value are only named), relations "
            "with counts, the first three neighbours by name and the count per state, stored "
            "files with their pages, and what applies next by name. At most 6 KiB; whatever is "
            "cut is counted (more, more_groups, other) and named in a hint. It is a state, not "
            "a record: use get for revision numbers and edge ids. --name card is always the "
            "generated card. --name NAME picks a published view: an entity view takes the REF "
            "of an entity of its class, a set view takes none and lists entities (and the "
            "rows of published functions) in pages of --page-bytes bytes (at most 262144); "
            "a page that does not reach the end has next and more, and --cursor with the "
            "same --name, --param and a graph that has not changed gives the next one "
            "(view.cursor_stale otherwise; --as-of tx:<seq> pages a state you started with). "
            "--param name=value gives a view parameter, read by its declared type. A set view "
            "needs read.find as well as read.get, a view that runs functions read.function; "
            "a view that names what the reader may not see is not found. Under --as-of the "
            "rows of functions are unavailable (the section answers view.section_unavailable)."
        ),
        params=(VIEW_REF, VIEW_NAME, VIEW_PARAM, VIEW_CURSOR, VIEW_PAGE_BYTES, GRAPH, AS_OF),
        examples=(
            Example("graph view <id>", "The default view of one entity, else its card."),
            Example("graph view <id> --json", "The same as JSON."),
            Example("graph view <id> --as-of tx:<seq> --json", "The card as it was after that tx."),
            Example(
                "graph view <id> --name card", "The generated card, whatever the graph publishes."
            ),
            Example(
                "graph view <id> --name <name> --param <param>=<value>", "A published entity view."
            ),
            Example("graph view --name <name> --page-bytes 65536", "One page of a set view."),
            Example(
                "graph view --name <name> --cursor <value>", "The next page, from a page's next."
            ),
        ),
        errors=VIEW_ERRORS,
        handler=view,
        output=VIEW_OUTPUT,
    ),
)
