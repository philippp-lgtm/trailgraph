"""Client work around the hooked commands of the remote mode (contracts B14.10).

- ``apply FILE|-`` and ``query -|--file``: the input is read here, with the
  local errors, and uploaded; the server reads the upload.
- ``file add PATH``: the file is checked and uploaded here, with its origin
  under this machine's ``[roots]``.
- ``file path``: the stored file is downloaded into this machine's file
  cache ``<state>/files/<graph id>/<hex>`` (``catalog.file_cache``: mode
  0400, hash checked, removed after a day unused) and that path is answered;
  ``--origin`` resolves the origin with this machine's roots instead.
- ``file check``: the server lists the files and their content hashes; the
  origins are checked here.
- ``start``: the server answers the graphs and counts; the program and the skills are
  judged and refreshed here, and the text is rendered from the merged answer.

Everything else about these commands (authorization, the write, the
answer) happens on the server. The server uses an upload up once, so a
hooked command that writes nothing (``query``, a dry run) is sent again with
its uploads when the connection fails (``resent``); a write is sent once.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from graph_cli.app_cli.commands.common import services
from graph_cli.app_cli.remote import forward, new_key
from graph_cli.catalog.remote import RemoteSession, envelope_of, error_of, protocol
from graph_cli.cli_kit.argv import api_args
from graph_cli.cli_kit.envelope import Forwarded, Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import read_documents
from graph_cli.kernel.errors import GraphCliError

if TYPE_CHECKING:
    from graph_cli.app_cli.remote import Hook

KEY_OPTION: Final = "--idempotency-key"
DIGEST_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTOR_HEADER: Final = "Graph-Cli-Actor"


def upload(
    session: RemoteSession,
    content: bytes | Path,
    name: str,
    *,
    origin: Mapping[str, JsonValue] | None = None,
    with_origin: bool = False,
) -> dict[str, JsonValue]:
    """Stage ``content`` on the server; the reference a file parameter takes."""
    headers = {"Content-Type": "application/octet-stream"}
    if isinstance(content, Path):
        with content.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            answer = session.client.call(
                "PUT", "/v1/uploads", body=handle, length=size, headers=headers
            )
    else:
        answer = session.client.call("PUT", "/v1/uploads", body=content, headers=headers)
    if answer.status != 200:
        raise error_of(envelope_of(answer))
    value = answer.json()
    digest = value.get("upload") if isinstance(value, Mapping) else None
    if not isinstance(digest, str):
        raise protocol(answer.status, "the upload was not confirmed")
    reference: dict[str, JsonValue] = {"upload": digest, "name": name}
    if with_origin:
        reference["origin"] = dict(origin) if origin is not None else None
    return reference


def resent(
    spec: CommandSpec,
    args: dict[str, JsonValue],
    send: Callable[[dict[str, JsonValue]], Result],
) -> Result:
    """``send(args)``: the uploads and the command, once. A command that writes nothing is
    sent again, with its uploads, when the connection fails: the server uses an upload up
    once, so the command alone cannot be repeated. A write is sent once (a lost answer is
    ``remote.outcome_unknown``). Each try gets its own copy of ``args``."""
    writes = spec.kind == "write" and args.get("--dry-run") is not True
    for attempt in range(1 if writes else 2):
        try:
            return send(dict(args))
        except GraphCliError as exc:
            if exc.code != "remote.unreachable" or writes or attempt:
                raise
    raise GraphCliError("internal.error", "no attempt was made")


# ---- apply, query --------------------------------------------------------------


def apply_hook(
    session: RemoteSession, spec: CommandSpec, state: CommandState, args: dict[str, JsonValue]
) -> Result:
    from graph_cli.app_cli.commands.apply import read_input

    file = str(args["file"])
    data = read_input(file)
    generate = args.get("--dry-run") is not True and KEY_OPTION not in args and _single(data)
    if generate:
        args[KEY_OPTION] = new_key()

    def send(args: dict[str, JsonValue]) -> Result:
        args["file"] = upload(session, data, file)
        return forward(session, spec, state, args, key=False, retry=False)

    return resent(spec, args, send)


def _single(data: bytes) -> bool:
    """One document without its own key: the key option can make it safe to repeat."""
    try:
        documents = read_documents(data)
    except GraphCliError:
        return False  # the server reports it, as locally
    return (
        len(documents) == 1
        and isinstance(documents[0], Mapping)
        and documents[0].get("idempotency_key") is None
    )


def query_hook(
    session: RemoteSession, spec: CommandSpec, state: CommandState, args: dict[str, JsonValue]
) -> Result:
    from graph_cli.app_cli.commands.read.query import query_text
    from graph_cli.app_cli.commands.read.shared import STDIN

    text, file = args.get("text"), args.get("--file")
    if file is None and text != STDIN:
        return forward(session, spec, state, args)
    source = query_text(
        str(text) if text is not None else None, str(file) if file is not None else None
    )
    args.pop("text", None)

    def send(args: dict[str, JsonValue]) -> Result:
        args["--file"] = upload(session, source.encode(), str(file) if file else STDIN)
        return forward(session, spec, state, args, retry=False)

    return resent(spec, args, send)


# ---- files ---------------------------------------------------------------------


def file_add_hook(
    session: RemoteSession, spec: CommandSpec, state: CommandState, args: dict[str, JsonValue]
) -> Result:
    from graph_cli.services.files import unreadable

    app = services(state)
    full = (
        app.path(str(args["path"]))
        if not Path(str(args["path"])).is_absolute()
        else Path(str(args["path"]))
    )
    try:
        status = os.stat(full)
    except OSError as exc:
        raise unreadable(full, exc.strerror or str(exc)) from exc
    if not stat.S_ISREG(status.st_mode):
        raise unreadable(full, "not a regular file")
    origin = None
    if args.get("--no-origin") is not True:
        _, _, roots = app.remote_roots()
        origin = roots.origin_of(full.resolve())

    def send(args: dict[str, JsonValue]) -> Result:
        args["path"] = upload(session, full, str(full), origin=origin, with_origin=True)
        return forward(session, spec, state, args, retry=False)

    return resent(spec, args, send)


def file_path_hook(
    session: RemoteSession, spec: CommandSpec, state: CommandState, args: dict[str, JsonValue]
) -> Result:
    origin = args.pop("--origin", False) is True
    document, _, _ = session.command(spec.path, api_args(args), text=False, write=None)
    data = document.get("data")
    if document.get("ok") is not True or not isinstance(data, Mapping):
        return Result(forwarded=Forwarded(document))
    _, roots, file_roots = services(state).remote_roots()
    found = dict(data)
    where = found.get("origin")
    if isinstance(where, Mapping):
        found["origin"] = {**where, "resolved": _resolved(file_roots, where)}
    ref = str(args["ref"])
    if origin:
        found["path"] = str(_origin_path(file_roots, where, ref, str(found.get("id"))))
    else:
        graph = document.get("graph")
        graph_id = graph.get("id") if isinstance(graph, Mapping) else None
        found["path"] = str(
            _download(
                session, roots.state_root, str(graph_id), str(found["id"]), str(found["hash"])
            )
        )
    return Result(forwarded=Forwarded({**document, "data": found}))


def _resolved(file_roots: object, where: Mapping[str, JsonValue]) -> JsonValue:
    """The origin on this machine, ``None`` for a root not set here; an origin from the
    server that is not a relative path under its root resolves nowhere."""
    from graph_cli.catalog.file_roots import FileRoots
    from graph_cli.ontology_layer.values import local_path_problem

    assert isinstance(file_roots, FileRoots)
    if local_path_problem({"root": where.get("root"), "path": where.get("path")}) is not None:
        return None
    try:
        return str(file_roots.resolve(where))
    except GraphCliError as exc:
        if exc.code != "file.root_unknown":
            raise
        return None


def _origin_path(file_roots: object, where: JsonValue, ref: str, entity: str) -> Path:
    """As ``file path --origin`` locally: the origin on this machine, which must be there."""
    from graph_cli.catalog.file_roots import FileRoots

    assert isinstance(file_roots, FileRoots)
    from graph_cli.ontology_layer.values import local_path_problem

    origin = (
        {"root": where.get("root"), "path": where.get("path")}
        if isinstance(where, Mapping)
        else None
    )
    if origin is None or local_path_problem(origin) is not None:
        raise GraphCliError(
            "file.origin_missing", f"{ref} has no recorded origin", details={"id": entity}
        )
    resolved = file_roots.resolve(origin)
    if not resolved.is_file():
        raise GraphCliError(
            "file.origin_missing",
            f"the origin of {ref} is not there: {resolved}",
            details={"id": entity, "path": str(resolved)},
        )
    return resolved


def _download(
    session: RemoteSession, state_root: Path, graph_id: str, entity: str, digest: str
) -> Path:
    """The stored file in this machine's file cache (``catalog.file_cache``), downloaded
    unless a copy with its hash is there already."""
    from graph_cli.catalog.file_cache import StateFileCache
    from graph_cli.kernel.ids import parse_id

    try:  # the server names what becomes a path here: only ids and a hash
        parse_id(graph_id)
        parse_id(entity)
    except (GraphCliError, ValueError):
        raise protocol(200, "the answer names no graph or file id") from None
    if not DIGEST_RE.match(digest):
        raise protocol(200, "the answer names no content hash")
    actor = base64.urlsafe_b64encode(json.dumps(dict(session.actor)).encode()).decode()
    status: list[int] = []

    def fill(sink: BinaryIO) -> None:
        answer = session.client.call(
            "GET",
            f"/v1/graphs/{graph_id}/files/{entity}",
            headers={ACTOR_HEADER: actor.rstrip("=")},
            sink=sink,
        )
        status.append(answer.status)
        if answer.status != 200:
            raise error_of(envelope_of(answer))

    cache = StateFileCache(state_root)
    cache.prune()
    return cache.copy(
        graph_id,
        digest,
        fill,
        mismatch=lambda: protocol(
            status[-1] if status else 200, "the downloaded file does not have its hash"
        ),
    )


def file_check_hook(
    session: RemoteSession, spec: CommandSpec, state: CommandState, args: dict[str, JsonValue]
) -> Result:
    from graph_cli.services.files import CHECK_STATUSES, hash_file

    document, _, _ = session.command(spec.path, api_args(args), text=False, write=None)
    data = document.get("data")
    if document.get("ok") is not True or not isinstance(data, Mapping):
        return Result(forwarded=Forwarded(document))
    _, _, file_roots = services(state).remote_roots()
    items: list[JsonValue] = []
    counts = dict.fromkeys(CHECK_STATUSES, 0)
    listed = data.get("items")
    for value in listed if isinstance(listed, list) else []:
        item = dict(value) if isinstance(value, Mapping) else {}
        where = item.get("origin")
        if isinstance(where, Mapping):
            resolved = _resolved(file_roots, where)
            item["origin"] = {**where, "resolved": resolved}
            if resolved is None:
                item["status"] = "root_unknown"
            else:
                digest = hash_file(Path(str(resolved)))
                if digest is None:
                    item["status"] = "missing"
                else:
                    item["status"] = "same" if digest == item.get("hash") else "changed"
        status = str(item.get("status"))
        counts[status] = counts.get(status, 0) + 1
        items.append(item)
    counted: dict[str, JsonValue] = dict(counts)
    return Result(
        forwarded=Forwarded({**document, "data": {**data, "items": items, "counts": counted}})
    )


# ---- start ---------------------------------------------------------------------


def start_hook(
    session: RemoteSession, spec: CommandSpec, state: CommandState, args: dict[str, JsonValue]
) -> Result:
    """The graphs and what the token may use come from the server; the program and the skills
    are judged and refreshed here (``--check`` and ``--dir`` stay on this side). The text is
    rendered here from the merged answer."""
    from graph_cli.app_cli.commands.start import client_part, holds_back, render

    sent = {key: value for key, value in args.items() if key == "--graph"}
    document, _, _ = session.command(spec.path, api_args(sent), text=False, write=None)
    data = document.get("data")
    if document.get("ok") is not True or not isinstance(data, Mapping):
        return Result(forwarded=Forwarded(document))
    directory = args.get("--dir")
    check = args.get("--check") is True
    here = client_part(
        state,
        check=check,
        directory=directory if isinstance(directory, str) else None,
        session=session,
    )
    program = here["program"]
    if isinstance(program, Mapping) and program.get("state") in ("current", "outdated"):
        # the step says it; the warning is the same news
        state.warnings[:] = [w for w in state.warnings if w.code != "remote.version_differs"]
    merged: dict[str, JsonValue] = {**here, **data}
    warnings = document.get("warnings")
    held = isinstance(program, Mapping) and holds_back(program, check)
    if not held and isinstance(warnings, list):
        # claims were read before the refresh: a stale copy was found and has just been replaced
        warnings = [
            w for w in warnings if not (isinstance(w, Mapping) and w.get("code") == "skills.stale")
        ]
        document = {**document, "warnings": warnings}
    return Result(forwarded=Forwarded({**document, "data": merged}), text=render(merged))


HOOKS: Final[Mapping[tuple[str, ...], Hook]] = {
    ("start",): start_hook,
    ("apply",): apply_hook,
    ("query",): query_hook,
    ("file", "add"): file_add_hook,
    ("file", "path"): file_path_hook,
    ("file", "check"): file_check_hook,
}
