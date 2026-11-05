"""``graph file add``, ``graph file path`` and ``graph file check`` (files slice 1).

A file is stored in the graph by ``file add`` (a ``files:File`` entity plus
its bytes); entities then point to it with ``files:from_file`` through the
ordinary write commands. graph_cli never reads what is in a file. PATH is
one argument: spaces and other characters need no quoting beyond the shell's.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, PROGRAM, context, services
from graph_cli.app_cli.commands.meta import GRAPH_ERRORS
from graph_cli.app_cli.commands.read import REF_ERRORS
from graph_cli.app_cli.commands.write import (
    COMMON,
    SET,
    SET_JSON,
    WRITE_ERRORS,
    check_reason_and_key,
    merge_values,
    parse_json_values,
    parse_texts,
    usage,
)
from graph_cli.app_cli.output_schemas import NULLABLE_STRING, WRITE_RESULT
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.vocabulary import FILE_TYPE
from graph_cli.query.plan import DEFAULT_LIMIT, MAX_LIMIT
from graph_cli.services.read import GraphRead, graph_json

FILE_ERRORS: Final = ("file.unreadable", "file.too_large", "file.module_missing")


def add(
    state: CommandState,
    path: str,
    type: str | None,
    title: str | None,
    set: tuple[str, ...],
    set_json: tuple[str, ...],
    media_type: str | None,
    source_url: str | None,
    retrieved_at: str | None,
    no_origin: bool,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    from graph_cli.services.files import FileAddRequest  # loaded by file add only

    check_reason_and_key(reason, idempotency_key)
    texts, values = parse_texts(set, "--set"), parse_json_values(set_json, "--set-json")
    merge_values(texts, values, "--set-json")
    given = Path(path)
    full = given if given.is_absolute() else services(state).cwd / given
    ctx = context(state)
    entry = ctx.select(graph)
    args: dict[str, JsonValue] = {
        "graph": entry.name,
        "path": path,
        "type": type,
        "title": title,
        "set": list(set),
        "set_json": list(set_json),
        "media_type": media_type,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "no_origin": no_origin,
        "dry_run": dry_run,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }
    result = ctx.files.add(
        FileAddRequest(
            actor=ctx.actor,
            graph=str(entry.id),
            path=full,
            request=RequestMeta.of("file add", args),
            type=type,
            title=title,
            attrs=values,
            attrs_text=texts,
            media_type=media_type,
            source_url=source_url,
            retrieved_at=retrieved_at,
            no_origin=no_origin,
            reason=reason,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )
    )
    state.warnings += result.warnings
    return Result(data=result.to_json(), graph=graph_json(result.entry, result.seq))


def _open(
    state: CommandState, graph: str | None, command: str, args: dict[str, JsonValue]
) -> GraphRead:
    ctx = context(state)
    entry = ctx.select(graph)
    request = RequestMeta.of(command, {"graph": entry.name, **args})
    view = ctx.reader.open(ctx.actor, entry, "read.get", request)
    state.warnings += view.warnings
    return view


def path(state: CommandState, ref: str, origin: bool, graph: str | None) -> Result:
    args: dict[str, JsonValue] = {"ref": ref, "origin": origin}
    view = _open(state, graph, "file path", args)
    ctx = context(state)
    request = RequestMeta.of("file path", {"graph": view.entry.name, **args})
    data, warnings = ctx.files.path(view, ref, origin=origin, actor=ctx.actor, request=request)
    state.warnings += warnings
    return Result(data=data, graph=view.envelope_graph())


def check(
    state: CommandState, refs: tuple[str, ...], limit: int, offset: int, graph: str | None
) -> Result:
    if not 1 <= limit <= MAX_LIMIT:
        raise usage("--limit", f"expected 1 to {MAX_LIMIT}", limit)
    if offset < 0:
        raise usage("--offset", "expected 0 or more", offset)
    args: dict[str, JsonValue] = {"refs": list(refs), "limit": limit, "offset": offset}
    view = _open(state, graph, "file check", args)
    ctx = context(state)
    request = RequestMeta.of("file check", {"graph": view.entry.name, **args})
    data, page, warnings = ctx.files.check(
        view, refs, limit=limit, offset=offset, actor=ctx.actor, request=request
    )
    state.warnings += warnings
    page_json = page.to_json()
    hints: tuple[dict[str, JsonValue], ...] = ()
    next_offset = page_json["next_offset"]
    if not refs and isinstance(next_offset, int):
        line = [PROGRAM, "file", "check", "--graph", view.entry.name]
        line += ["--limit", str(limit), "--offset", str(next_offset)]
        hints = ({"text": "More results available", "command": shlex.join(line)},)
    return Result(data=data, graph=view.envelope_graph(), page=page_json, hints=hints)


_FILE_ENTITY: Final = obj(
    {"id": NULLABLE_STRING, "alias": NULLABLE_STRING, "type": STRING, "rev": INTEGER}
)
ADD_OUTPUT: Final = obj(  # services.files.FileAddResult.to_json
    {
        **WRITE_RESULT,
        "entity": _FILE_ENTITY,
        "file": obj(
            {
                "hash": STRING,
                "size": INTEGER,
                "media_type": NULLABLE_STRING,
                "file_name": NULLABLE_STRING,
                "origin": nullable(obj({"root": STRING, "path": STRING})),
            }
        ),
        "existing": BOOLEAN,
    }
)
# ``path`` is null in the server's answer (the bytes are at GET /v1/graphs/{graph}/files/
# {ref}); the remote client downloads them and answers its own path.
PATH_OUTPUT: Final = obj(
    {
        "id": STRING,
        "alias": NULLABLE_STRING,
        "type": STRING,
        "hash": STRING,
        "size": INTEGER,
        "media_type": NULLABLE_STRING,
        "path": NULLABLE_STRING,
        "origin": nullable(ref("file_origin")),
    }
)
# services.files.CHECK_STATUSES; reads do not load the file service at startup
CHECK_STATUSES: Final = ("same", "changed", "missing", "root_unknown", "no_origin")
CHECK_OUTPUT: Final = obj(
    {
        "items": array(
            obj(
                {
                    "id": STRING,
                    "alias": NULLABLE_STRING,
                    "status": enum(*CHECK_STATUSES),
                    "origin": nullable(ref("file_origin")),
                    "hash": NULLABLE_STRING,
                }
            )
        ),
        "counts": obj(dict.fromkeys(CHECK_STATUSES, INTEGER)),
    }
)

FILE_REF: Final = ParamSpec(
    "ref",
    "entity_ref",
    f"A {FILE_TYPE} entity: full id, alias or trailing hex of the id.",
    required=True,
    positional=True,
)

SPECS: Final = (
    CommandSpec(
        path=("file", "add"),
        kind="write",
        surface="act",
        action="write.create",
        summary="Store a file as a files:File entity; known content is not stored twice.",
        description=(
            "graph_cli stores the bytes and never reads what is in them. Link entities to the "
            "file with files:from_file and the page qualifier. The origin is recorded relative "
            "to a root of [roots] in config.toml."
        ),
        params=(
            ParamSpec("path", "file", "The file to store.", required=True, positional=True),
            ParamSpec("--type", "term", f"{FILE_TYPE} or a subclass of it."),
            ParamSpec("--title", "string", "Name for people; the file name without extension."),
            SET,
            SET_JSON,
            ParamSpec("--media-type", "string", "Media type instead of the detected one."),
            ParamSpec("--source-url", "string", "Address the file was downloaded from."),
            ParamSpec("--retrieved-at", "string", "Date of the download, YYYY-MM-DD."),
            ParamSpec("--no-origin", "flag", "Do not record where the file came from."),
            *COMMON,
        ),
        examples=(
            Example("graph file add <file>", "Store a file."),
            Example(
                "graph file add <file> --type <type> --title <value> --json",
                "Store it as a subclass with a title.",
            ),
        ),
        errors=(*WRITE_ERRORS, *FILE_ERRORS),
        handler=add,
        output=ADD_OUTPUT,
        remote="hooked",
    ),
    CommandSpec(
        path=("file", "path"),
        kind="read",
        surface="retrieve",
        action="read.get",
        summary="Show the local path of a stored file (read only), or of its origin.",
        description=(
            "Read the stored copy at this path; never edit, move or delete it. With --origin "
            "the path is where the file came from on this machine."
        ),
        params=(
            FILE_REF,
            ParamSpec("--origin", "flag", "The origin on this machine instead of the stored copy."),
            GRAPH,
        ),
        examples=(
            Example("graph file path <id>", "Where to read the stored file."),
            Example("graph file path <id> --origin --json", "Where it came from."),
        ),
        errors=(
            *REF_ERRORS,
            "file.module_missing",
            "file.no_local_path",
            "file.root_unknown",
            "file.origin_missing",
            "file.erased",
            "backend.corrupt",
        ),
        handler=path,
        output=PATH_OUTPUT,
        remote="hooked",
    ),
    CommandSpec(
        path=("file", "check"),
        kind="read",
        surface="retrieve",
        action="read.get",
        summary="Compare the origins of stored files with their content: same, changed, missing.",
        description=(
            "Statuses: same, changed, missing, root_unknown, no_origin. On changed, store the "
            "new version with file add and supersede the old file with a reason."
        ),
        params=(
            ParamSpec(
                "refs",
                "entity_ref",
                "Files to check; all active files with an origin when omitted.",
                repeatable=True,
                positional=True,
            ),
            ParamSpec(
                "--limit", "integer", f"Page size, at most {MAX_LIMIT}.", default=DEFAULT_LIMIT
            ),
            ParamSpec("--offset", "integer", "Skip this many files.", default=0),
            GRAPH,
        ),
        examples=(
            Example("graph file check", "Check every file with an origin."),
            Example("graph file check <id> --json", "Check one file."),
        ),
        errors=(*GRAPH_ERRORS, "entity.not_found", "entity.ambiguous_ref", "file.module_missing"),
        handler=check,
        output=CHECK_OUTPUT,
        remote="hooked",
        limits={"limit": MAX_LIMIT},
    ),
)
