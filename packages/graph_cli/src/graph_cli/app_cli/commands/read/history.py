"""``graph history`` (the revisions of one entity) and ``graph changes`` (the change feed of the
graph): the handlers, the text of a feed page, the output schemas and the specs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.app_cli.commands.common import (
    AS_OF,
    GRAPH,
    GRAPH_ERRORS,
    Hint,
    command_line,
    context,
    next_page,
)
from graph_cli.app_cli.commands.read.shared import REF, REF_ERRORS, as_of_flag, open_graph, pinned
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, SOURCE, STRINGS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import ANY, BOOLEAN, INTEGER, STRING, array, nullable, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT, check_paging
from graph_cli.temporal_knowledge.changes import FEED_LIMIT, parse_since


def history(
    state: CommandState, ref: str, limit: int, offset: int, graph: str | None, as_of: str | None
) -> Result:
    point = as_of_flag(as_of)
    check_paging(limit, offset)
    args: dict[str, JsonValue] = {"ref": ref, "limit": limit, "offset": offset}
    view = open_graph(state, graph, "read.history", "history", args, point)
    ctx = context(state)
    data, page, warnings = ctx.reader.history(view, ctx.actor, ref, limit=limit, offset=offset)
    state.warnings += warnings
    page_json = page.to_json()

    def line(next_offset: int) -> str:
        options = pinned(view, point)
        return command_line("history", (ref,), view.entry.name, options, (), limit, next_offset)

    hints = next_page(page_json, line)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


def changes(state: CommandState, since: str, limit: int, graph: str | None) -> Result:
    """The change feed (change feed plan CF1): what changed in the whole graph after a
    position, one page, oldest first. Authorized by ``read.history`` like ``history``; no
    ``--as-of`` (the position is the "from", the head the "to")."""
    position = parse_since(since)
    check_paging(limit, 0)
    args: dict[str, JsonValue] = {"since": since, "limit": limit}
    view = open_graph(state, graph, "read.history", "changes", args, None)
    data = context(state).reader.changes(view, position, limit=limit)
    options = (("--since", (str(data["next"]),)),)
    hints: tuple[Hint, ...] = ()
    if data["more"]:
        line = command_line(
            "changes", (), view.entry.name, options, (), limit, default_limit=FEED_LIMIT
        )
        hints = ({"text": "More changes available", "command": line},)
    return Result(data=data, graph=view.envelope_graph(), hints=hints, text=_changes_text(data))


def _changes_text(data: dict[str, JsonValue]) -> str:
    """The text of a feed page (CF14): a header line per tx, a line per entity, a line for
    the migration of a migration tx."""
    items = data["items"]
    assert isinstance(items, list)
    if not items:
        return f"No changes after {data['since']}.\n"
    lines: list[str] = []
    for entry in items:
        assert isinstance(entry, Mapping)
        lines += _entry_lines(entry)
    return "".join(line + "\n" for line in lines)


def _entry_lines(entry: Mapping[str, JsonValue]) -> list[str]:
    head = [f"tx:{entry['tx_seq']}", str(entry["recorded_at"]), str(entry["command"])]
    action = entry.get("action")
    if isinstance(action, Mapping):
        head.append(str(action["name"]))
    if entry["own_session"]:
        head.append("(own session)")
    if entry.get("partial"):
        head.append("(continues)")
    lines = [" ".join(head)]
    if entry["reason"]:
        lines.append(f"  reason: {_one_line(str(entry['reason']))}")
    migration = entry.get("migration")
    if isinstance(migration, Mapping):
        text = f"  migration {migration['from']} -> {migration['to']}"
        if "summary" in migration:
            text += f": {_one_line(str(migration['summary']))}"
        lines.append(text)
    entities = entry["entities"]
    assert isinstance(entities, list)
    for item in entities:
        assert isinstance(item, Mapping)
        lines.append("  " + _entity_line(item))
    return lines


def _entity_line(item: Mapping[str, JsonValue]) -> str:
    text = f"{item['id']} {item['type']} rev {item['rev']}"
    if item["record_status"] != "active":
        text += f" ({item['record_status']})"
    changed = item["changed"]
    if not isinstance(changed, Mapping):
        return f"{text} created"
    edges = changed["edges"]
    assert isinstance(edges, Mapping)
    named = [
        ("record", changed["record"]),
        ("attrs", changed["attrs"]),
        *((f"edges {kind}", names) for kind, names in edges.items()),
    ]
    parts = [
        f"{label}: {', '.join(map(str, names))}"
        for label, names in named
        if isinstance(names, list) and names
    ]
    return f"{text} {'; '.join(parts)}" if parts else f"{text} no visible change"


def _one_line(text: str) -> str:
    return " ".join(text.split())


CHANGED: Final = nullable(  # temporal_knowledge.history.changed_names; null for a creation
    obj(
        {
            "record": STRINGS,
            "attrs": STRINGS,
            "edges": obj({"added": STRINGS, "removed": STRINGS, "changed": STRINGS}),
        }
    )
)


HISTORY_OUTPUT: Final = obj(
    {
        "id": STRING,
        "current_rev": INTEGER,
        "items": array(
            obj(
                {
                    "rev": INTEGER,
                    "tx_seq": INTEGER,
                    "recorded_at": STRING,
                    "record_status": STRING,
                    "command": NULLABLE_STRING,
                    "reason": NULLABLE_STRING,
                    "own_session": BOOLEAN,
                    "changed": CHANGED,
                },
                {
                    "action": obj({"name": STRING, "hash": STRING}),  # set by a ``do``
                    "source": SOURCE,  # set by a document that names its source run
                    # the revision a migration wrote
                    # (services.read.migration_effects.migration_item)
                    "migration": obj(
                        {
                            "from": NULLABLE_STRING,
                            "to": NULLABLE_STRING,
                            "renames": array(obj({"kind": STRING}, extra=ANY)),
                            "steps": array(obj({"kind": STRING, "index": INTEGER}, extra=ANY)),
                        },
                        {"summary": STRING},
                    ),
                },
            )
        ),
    }
)


# services.read.change_feed.change_page: the answer of ``graph changes`` (change feed plan
# section 4)
CHANGES_OUTPUT: Final = obj(
    {
        "since": STRING,
        "since_tx_id": NULLABLE_STRING,
        "head": INTEGER,
        "next": STRING,
        "next_tx_id": NULLABLE_STRING,
        "more": BOOLEAN,
        "view": NULLABLE_STRING,
        "items": array(
            obj(
                {
                    "tx_seq": INTEGER,
                    "tx_id": STRING,
                    "recorded_at": STRING,
                    "command": NULLABLE_STRING,
                    "reason": NULLABLE_STRING,
                    "own_session": BOOLEAN,
                    "entities": array(
                        obj(
                            {
                                "id": STRING,
                                "type": STRING,
                                "rev": INTEGER,
                                "record_status": STRING,
                                "changed": CHANGED,
                                "targets": STRINGS,
                            },
                            {"targets_more": INTEGER},  # targets beyond the cap
                        )
                    ),
                },
                {
                    "action": obj({"name": STRING, "hash": STRING}),  # set by a ``do``
                    "source": SOURCE,  # set by a document that names its source run
                    "migration": obj(  # a migration tx (services.read.change_feed._feed_migration)
                        {"from": NULLABLE_STRING, "to": NULLABLE_STRING}, {"summary": STRING}
                    ),
                    "partial": BOOLEAN,  # true: the tx goes on on the next page
                },
            )
        ),
    }
)


SPECS: Final = (
    CommandSpec(
        path=("history",),
        kind="read",
        surface="trace",
        action="read.history",
        summary="List how one entity changed, newest first: when, by which command and why.",
        description=(
            "Each revision names the fields and relations that changed, not their values; "
            "read a revision with get --rev. own_session tells whether your session wrote it."
        ),
        params=(
            REF,
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many revisions.", default=0),
            GRAPH,
            AS_OF,
        ),
        examples=(
            Example("graph history <id> --json", "The revisions of one entity."),
            Example("graph history <id> --limit 5 --json", "The five most recent revisions."),
        ),
        errors=REF_ERRORS,
        handler=history,
        output=HISTORY_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
    CommandSpec(
        path=("changes",),
        kind="read",
        surface="trace",
        action="read.history",
        summary="List what changed in the whole graph after a position, oldest first; paged.",
        description=(
            "The change feed: per tx the entities it changed, as names and ids, never values; "
            "read values with get or find. --since is tx:<seq> (tx:0 for the start) or an "
            "RFC 3339 time with offset. Continue with the next of the answer while more is "
            "true. A different view than the one stored means start again from tx:0, and so "
            "does a since_tx_id that differs from the stored next_tx_id. Needs permission to "
            "read history."
        ),
        params=(
            ParamSpec(
                "--since",
                "string",
                "Where to start: tx:<seq>, tx:<seq>/<entity id> (a next of the answer) or an "
                "RFC 3339 time with offset.",
                required=True,
            ),
            ParamSpec(
                "--limit",
                "integer",
                f"Entity items per page, at most {MAX_LIMIT}.",
                default=FEED_LIMIT,
            ),
            GRAPH,
        ),
        examples=(
            Example("graph changes --since tx:0 --json", "Every change from the start."),
            Example(
                "graph changes --since tx:<seq> --graph <graph> --json",
                "Continue after a position, the next of the last answer.",
            ),
            Example(
                "graph changes --since 2030-01-01T12:00:00Z --json", "What changed since a time."
            ),
        ),
        errors=(*GRAPH_ERRORS, "usage.invalid_argument"),
        handler=changes,
        output=CHANGES_OUTPUT,
        limits={"limit": MAX_LIMIT},
    ),
)
