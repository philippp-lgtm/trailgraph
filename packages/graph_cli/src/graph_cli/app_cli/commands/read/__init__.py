"""``graph get``, ``graph history``, ``graph changes``, ``graph find``, ``graph lookup``,
``graph neighbors``, ``graph query`` and ``graph run``.

Contracts §4.3, B8.8, B8.12, B8.15, B9.7, B13 (Cypher); ``run``: named functions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.app_cli.commands.common import (
    AS_OF,
    GRAPH,
    GRAPH_ERRORS,
    command_line,
    context,
    next_page,
)
from graph_cli.app_cli.commands.read import history, query
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
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, STRINGS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import ANY, INTEGER, STRING, array, enum, nullable, obj, ref
from graph_cli.cli_kit.runner import CommandState, missing_argument
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.lookup import MATCH_KINDS, LookupSpec
from graph_cli.query.plan import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FindSpec,
    parse_rel_flag,
    split_assignment,
)
from graph_cli.query.search import SearchSpec
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


def find(
    state: CommandState,
    type: tuple[str, ...],
    where: tuple[str, ...],
    rel: tuple[str, ...],
    in_rel: tuple[str, ...],
    include_inactive: bool,
    sort: tuple[str, ...],
    fields: tuple[str, ...],
    full: bool,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = as_of_flag(as_of)
    args: dict[str, JsonValue] = {
        "type": list(type),
        "where": list(where),
        "rel": list(rel),
        "in_rel": list(in_rel),
        "include_inactive": include_inactive,
        "sort": list(sort),
        "fields": list(fields),
        "limit": limit,
        "offset": offset,
    }
    if full:
        args["full"] = True
    rels = [parse_rel_flag(text, "--rel") for text in rel]
    in_rels = [parse_rel_flag(text, "--in-rel") for text in in_rel]
    view = open_graph(state, graph, "read.find", "find", args, point)
    spec = FindSpec(
        types=type,
        where=where,
        include_inactive=include_inactive,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    data, page, warnings = context(state).reader.find(
        view, spec, rel_refs=rels, in_rel_refs=in_rels, fields=fields, full=full
    )
    state.warnings += warnings
    page_json = page.to_json()
    options = (
        ("--type", type),
        ("--where", where),
        ("--rel", rel),
        ("--in-rel", in_rel),
        ("--sort", sort),
        ("--fields", fields),
        *pinned(view, point),
    )
    switches = ("--include-inactive",) * include_inactive + ("--full",) * full

    def line(next_offset: int) -> str:
        return command_line("find", (), view.entry.name, options, switches, limit, next_offset)

    hints = next_page(page_json, line)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


MAX_LOOKUP_TEXTS: Final = 50
MAX_SEARCH_HINTS: Final = 5


def lookup(
    state: CommandState,
    text: tuple[str, ...],
    type: tuple[str, ...],
    for_use: str | None,
    for_source: str | None,
    match: str,
    ignore_accents: bool,
    include_inactive: bool,
    lang: str | None,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    if len(text) > MAX_LOOKUP_TEXTS:
        raise GraphCliError(
            "usage.invalid_argument",
            f"text: at most {MAX_LOOKUP_TEXTS} values, got {len(text)}",
            details={"argument": "text", "actual": len(text)},
        )
    point = as_of_flag(as_of)
    args: dict[str, JsonValue] = {
        "text": list(text),
        "type": list(type),
        "for": for_use,
        "for_source": for_source,
        "match": match,
        "ignore_accents": ignore_accents,
        "include_inactive": include_inactive,
        "lang": lang,
        "limit": limit,
        "offset": offset,
    }
    view = open_graph(state, graph, "read.find", "lookup", args, point)
    spec = LookupSpec(
        text=text[0],
        types=type,
        match=match,
        ignore_accents=ignore_accents,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
        lang=lang,
    )
    data, page = context(state).reader.lookup(
        view, spec, text, for_use=for_use, for_source=for_source
    )
    page_json = page.to_json()
    switches = tuple(
        flag
        for flag, on in (
            ("--ignore-accents", ignore_accents),
            ("--include-inactive", include_inactive),
        )
        if on
    )

    uses = (
        ("--for", (for_use,) if for_use else ()),
        ("--for-source", (for_source,) if for_source else ()),
    )

    def line(
        texts: tuple[str, ...],
        match_kind: str,
        next_offset: int | None = None,
        *,
        fit: bool = True,
    ) -> str:
        options = (
            ("--type", type),
            *(uses if fit else ()),
            ("--match", (match_kind,) if match_kind != "exact" else ()),
            ("--lang", (lang,) if lang else ()),
            *pinned(view, point),
        )
        return command_line("lookup", texts, view.entry.name, options, switches, limit, next_offset)

    counts = data["texts"]
    assert isinstance(counts, list)
    missing = tuple(str(c["text"]) for c in counts if isinstance(c, Mapping) and not c["total"])
    hints = next_page(page_json, lambda next_offset: line(text, match, next_offset))
    if missing and match == "exact":
        hints += (
            {
                "text": "No exact match; try names that start with the text"
                if len(counts) == 1
                else f"No exact match for {len(missing)} of {len(counts)}; try names that "
                "start with them",
                "command": line(missing, "prefix"),
            },
        )
    elif missing:
        options = (
            ("--type", type),
            *uses,
            ("--lang", (lang,) if lang else ()),
            *pinned(view, point),
        )
        hints += tuple(
            {
                "text": "No name starts with the text; search names and text fields",
                "command": command_line(
                    "search",
                    (value,),
                    view.entry.name,
                    options,
                    ("--include-inactive",) if include_inactive else (),  # no accent switch
                    DEFAULT_LIMIT,
                ),
            }
            for value in missing[:MAX_SEARCH_HINTS]
        )
    return Result(
        data=data,
        graph=view.envelope_graph(),
        page=page_json,
        hints=hints + _fit_hints(counts, for_use, for_source, lambda t: line(t, match, fit=False)),
    )


def _fit_hints(
    counts: list[JsonValue],
    for_use: str | None,
    for_source: str | None,
    unfitted: Callable[[tuple[str, ...]], str],
) -> tuple[dict[str, JsonValue], ...]:
    """Lookup hints about the intended use: how to narrow names with several exact
    candidates (no ``--for`` given), or that ``--for`` dropped every exact match."""
    rows = [c for c in counts if isinstance(c, Mapping)]
    if for_use is None and for_source is None:
        several = [str(c["text"]) for c in rows if int(str(c["exact"])) > 1]
        if not several:
            return ()
        which = "the name" if len(rows) == 1 else f"{len(several)} of {len(rows)} names"
        return (
            {
                "text": f"Several exact candidates for {which}: narrow them to what the entity is"
                " for with --for <relation> (the candidate is its target), --for"
                " <function>.<param> or --for-source <relation>, or tell them apart by type"
                " and facts",
                "command": None,
            },
        )
    dropped = tuple(
        str(c["text"]) for c in rows if not int(str(c["exact"])) and int(str(c["excluded"]))
    )
    if not dropped:
        return ()
    use = " and ".join(
        f"{flag} {value}"
        for flag, value in (("--for", for_use), ("--for-source", for_source))
        if value
    )
    which = "" if len(rows) == 1 else f" for {len(dropped)} of {len(rows)} names"
    return (
        {
            "text": f"No exact match fits {use}{which}; matches of other types are left out:"
            " see them without it",
            "command": unfitted(dropped),
        },
    )


def search(
    state: CommandState,
    text: str,
    type: tuple[str, ...],
    field: tuple[str, ...],
    include_inactive: bool,
    lang: str | None,
    limit: int,
    offset: int,
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = as_of_flag(as_of)
    args: dict[str, JsonValue] = {
        "text": text,
        "type": list(type),
        "field": list(field),
        "include_inactive": include_inactive,
        "lang": lang,
        "limit": limit,
        "offset": offset,
    }
    view = open_graph(state, graph, "read.find", "search", args, point)
    spec = SearchSpec(
        text=text,
        types=type,
        fields=field,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
        lang=lang,
    )
    data, page = context(state).reader.search(view, spec)
    page_json = page.to_json()
    language = ("--lang", (lang,) if lang else ())
    options = (("--type", type), ("--field", field), language, *pinned(view, point))
    switches = ("--include-inactive",) if include_inactive else ()

    def line(next_offset: int) -> str:
        return command_line(
            "search", (text,), view.entry.name, options, switches, limit, next_offset
        )

    hints = next_page(page_json, line)
    if not page.total and (type or field):
        hints = (
            {
                "text": "Nothing found; search every type and field",
                "command": command_line(
                    "search",
                    (text,),
                    view.entry.name,
                    (language, *pinned(view, point)),
                    switches,
                    DEFAULT_LIMIT,
                ),
            },
        )
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def neighbors(
    state: CommandState,
    ref: str,
    rel: tuple[str, ...],
    direction: str,
    depth: int,
    fields: tuple[str, ...],
    full: bool,
    graph: str | None,
    as_of: str | None,
) -> Result:
    point = as_of_flag(as_of)
    args: dict[str, JsonValue] = {
        "ref": ref,
        "rel": list(rel),
        "direction": direction,
        "depth": depth,
        "fields": list(fields),
    }
    if full:
        args["full"] = True
    view = open_graph(state, graph, "read.traverse", "neighbors", args, point)
    data, warnings = context(state).reader.neighbors(
        view, ref, rels=rel, direction=direction, depth=depth, fields=fields, full=full
    )
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


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

_FOUND: Final = {  # what lookup and search show of an entity
    "id": STRING,
    "alias": NULLABLE_STRING,
    "type": STRING,
    "record_status": STRING,
    "label": NULLABLE_STRING,
}
_LABEL_ROLES: Final = ("label", "alt_label", "hidden_label")
_MATCH: Final = {  # query.lookup.Match.to_json; search adds score and excerpt for value
    "attr": STRING,
    "role": enum(*_LABEL_ROLES),
    "kind": STRING,
    "lang": NULLABLE_STRING,
}
# search also reads attributes without a label role: the role is null there
_SEARCH_MATCH: Final = {**_MATCH, "role": nullable(enum(*_LABEL_ROLES))}
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
FIND_OUTPUT: Final = obj({"items": array(ref("entity"))})
LOOKUP_OUTPUT: Final = obj(
    {
        "items": array(
            obj(
                {
                    "text": STRING,
                    **_FOUND,
                    "match": obj({**_MATCH, "value": NULLABLE_STRING}),
                    "facts": nullable(  # query.lookup.fact_edges
                        array(
                            obj(
                                {
                                    "rel": STRING,
                                    "id": STRING,
                                    "alias": NULLABLE_STRING,
                                    "label": NULLABLE_STRING,
                                }
                            )
                        )
                    ),
                }
            )
        ),
        "texts": array(
            obj({"text": STRING, "total": INTEGER, "exact": INTEGER, "excluded": INTEGER})
        ),
        "fit": nullable(  # --for, --for-source: services.read.finding
            obj({"for": NULLABLE_STRING, "for_source": NULLABLE_STRING, "types": nullable(STRINGS)})
        ),
    }
)
SEARCH_OUTPUT: Final = obj(
    {
        "items": array(
            obj(
                {
                    **_FOUND,
                    "match": obj({**_SEARCH_MATCH, "score": INTEGER, "excerpt": NULLABLE_STRING}),
                    "fields": STRINGS,
                }
            )
        )
    }
)
NEIGHBORS_OUTPUT: Final = obj(
    {
        "start": STRING,
        "items": array(
            obj({"direction": enum("out", "in"), "edge": ref("edge"), "entity": ref("entity")})
        ),
    }
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
        errors=(
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
        ),
        handler=view,
        output=VIEW_OUTPUT,
    ),
    *history.SPECS,
    CommandSpec(
        path=("find",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Find entities by type, field filters and relations; paged.",
        params=(
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
            ParamSpec(
                "--where",
                "filter",
                "attr=value or attr:op=value (ne lt le gt ge contains prefix in exists lang).",
                repeatable=True,
            ),
            ParamSpec(
                "--rel", "string", "Outgoing relation, optionally =<id>[,<id>...].", repeatable=True
            ),
            ParamSpec(
                "--in-rel",
                "string",
                "Incoming relation, optionally =<id>[,<id>...].",
                repeatable=True,
            ),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec("--sort", "string", "Field to sort by, optionally :desc.", repeatable=True),
            FIELDS,
            FULL,
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph find --type <type> --where <attr>=<value>", "Filter by a field."),
            Example(
                "graph find --type <type> --fields <attr> --json",
                "Only one field of each entity.",
            ),
            Example(
                "graph find --type <type> --rel <relation>=<id> --json", "Filter by a relation."
            ),
            Example(
                "graph find --type <type> --rel <relation>=<id>,<id> --json",
                "An edge to any of the listed entities.",
            ),
            Example(
                "graph find --type <type> --as-of tx:<seq> --json",
                "The entities of a type as they were after that tx.",
            ),
        ),
        errors=(*REF_ERRORS, "term.unknown", "query.ambiguous_term"),
        handler=find,
        output=FIND_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("lookup",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Find entities by name: main, alternative and hidden labels; ranked candidates.",
        description=(
            "Matches case- and Unicode-insensitively. Several names are looked up at once, "
            f"at most {MAX_LOOKUP_TEXTS}; each argument is one name, so quote a name with "
            "spaces. Limit and offset apply per name; data.texts counts the candidates and "
            "exact matches of each. --for and --for-source keep the candidates whose type "
            "can fill a slot and count the others as excluded. A name with several candidates "
            "gives each up to three facts that tell them apart. Labels are not identifiers: "
            "use the returned id for get, writes and change sets."
        ),
        params=(
            ParamSpec(
                "text",
                "string",
                "The names to look for, one per argument.",
                required=True,
                positional=True,
                repeatable=True,
            ),
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
            ParamSpec(
                "--for",
                "string",
                "Only candidates that can fill this slot: a relation (the candidate is its "
                "target) or <function or action>.<param> (a ref parameter).",
                keyword="for_use",
            ),
            ParamSpec(
                "--for-source",
                "term",
                "Only candidates that can be the source of this relation.",
            ),
            ParamSpec(
                "--match",
                "choice",
                "Whole name, or names that start with the text.",
                choices=MATCH_KINDS,
                default="exact",
            ),
            ParamSpec(
                "--ignore-accents",
                "flag",
                "Also match with accents and other combining marks ignored.",
            ),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec(
                "--lang",
                "string",
                "Only names in this language (a BCP 47 tag or range such as de, or *); "
                "also picks the shown label.",
            ),
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph lookup <value>", "Entities named exactly so, any type."),
            Example(
                "graph lookup <value> '<value> <value>' --json",
                "Several names in one call, one per argument.",
            ),
            Example(
                "graph lookup <value> --type <type> --match prefix --json",
                "Names of one type that start with the text.",
            ),
            Example(
                "graph lookup <value> --for <relation> --json",
                "Only candidates that can be the target of the relation.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "temporal.as_of_unsupported",
            "term.unknown",
            "query.ambiguous_term",
            "term.no_labels",
            "function.not_found",
            "function.parameter_unknown",
            "action.parameter_unknown",
        ),
        handler=lookup,
        output=LOOKUP_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("search",),
        kind="read",
        surface="retrieve",
        action="read.find",
        summary="Search names and text fields of every type; ranked suggestions, typos allowed.",
        description=(
            "Ignores case, accents and punctuation; finds the text inside longer words and "
            "similar names. Results are suggestions: check one with get before you use its id."
        ),
        params=(
            ParamSpec("text", "string", "The words to look for.", required=True, positional=True),
            ParamSpec(
                "--type",
                "term",
                "Type CURIE or unique local name, subtypes included.",
                repeatable=True,
            ),
            ParamSpec("--field", "string", "Only this text field.", repeatable=True),
            ParamSpec("--include-inactive", "flag", "Include superseded and retracted entities."),
            ParamSpec(
                "--lang",
                "string",
                "Only text in this language (a BCP 47 tag or range such as de, or *); "
                "also picks the shown label.",
            ),
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many results.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph search <value>", "Names and text fields of any type."),
            Example(
                "graph search <value> --type <type> --field <attr> --json",
                "One field of one type.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "temporal.as_of_unsupported",
            "term.unknown",
            "query.ambiguous_term",
            "term.no_text_fields",
        ),
        handler=search,
        output=SEARCH_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("neighbors",),
        kind="read",
        surface="retrieve",
        action="read.traverse",
        summary="Show the entities connected to one entity, up to three hops.",
        params=(
            REF,
            ParamSpec("--rel", "term", "Only these relations.", repeatable=True),
            ParamSpec(
                "--direction",
                "choice",
                "Edge direction.",
                choices=("out", "in", "both"),
                default="both",
            ),
            ParamSpec("--depth", "integer", "Hops, 1 to 3.", default=1),
            FIELDS,
            FULL,
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph neighbors <id>", "Direct neighbors."),
            Example(
                "graph neighbors <id> --rel <relation> --direction out --depth 2 --json",
                "Two hops along one relation.",
            ),
        ),
        errors=(*REF_ERRORS, "term.unknown", "query.ambiguous_term"),
        handler=neighbors,
        output=NEIGHBORS_OUTPUT,
        limits={"depth": 3},
    ),
    *query.SPECS,
)
