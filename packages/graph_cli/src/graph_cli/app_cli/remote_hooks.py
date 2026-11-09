"""Client work around the hooked commands of the remote mode (contracts B14.10).

- ``apply FILE|-`` and ``query -|--file``: the input is read here, with the
  local errors, and uploaded; the server reads the upload.
- ``file add PATH``: the file is checked and uploaded here, with its origin
  under this machine's ``[roots]``.
- ``file path``: the stored file is downloaded into
  ``<state>/files/<graph id>/<hex>`` (read only, hash checked, kept as a
  cache) and that path is answered; ``--origin`` resolves the origin with
  this machine's roots instead.
- ``file check``: the server lists the files and their content hashes; the
  origins are checked here.

Everything else about these commands (authorization, the write, the
answer) happens on the server.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Mapping
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
    args["file"] = upload(session, data, file)
    return forward(session, spec, state, args, key=False)


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
    from graph_cli.app_cli.commands.read import STDIN, query_text

    text, file = args.get("text"), args.get("--file")
    if file is not None or text == STDIN:
        source = query_text(
            str(text) if text is not None else None, str(file) if file is not None else None
        )
        args.pop("text", None)
        args["--file"] = upload(session, source.encode(), str(file) if file else STDIN)
    return forward(session, spec, state, args)


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
    args["path"] = upload(session, full, str(full), origin=origin, with_origin=True)
    return forward(session, spec, state, args)


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
    """The stored file in this machine's cache, downloaded unless it is there already."""
    from graph_cli.catalog.file_copies import FileCopyDir
    from graph_cli.kernel.clock import SystemClock
    from graph_cli.kernel.ids import parse_id

    try:  # the server names what becomes a path here: only ids and a hash
        parse_id(graph_id)
        parse_id(entity)
    except (GraphCliError, ValueError):
        raise protocol(200, "the answer names no graph or file id") from None
    if not DIGEST_RE.match(digest):
        raise protocol(200, "the answer names no content hash")
    actor = base64.urlsafe_b64encode(json.dumps(dict(session.actor)).encode()).decode()

    def write(sink: BinaryIO) -> None:
        answer = session.client.call(
            "GET",
            f"/v1/graphs/{graph_id}/files/{entity}",
            headers={ACTOR_HEADER: actor.rstrip("=")},
            sink=sink,
        )
        if answer.status != 200:
            raise error_of(envelope_of(answer))

    def mismatch() -> GraphCliError:
        return protocol(200, "the downloaded file does not have its hash")

    return FileCopyDir(state_root / "files", SystemClock()).get(graph_id, digest, write, mismatch)


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


HOOKS: Final[Mapping[tuple[str, ...], Hook]] = {
    ("apply",): apply_hook,
    ("query",): query_hook,
    ("file", "add"): file_add_hook,
    ("file", "path"): file_path_hook,
    ("file", "check"): file_check_hook,
}
