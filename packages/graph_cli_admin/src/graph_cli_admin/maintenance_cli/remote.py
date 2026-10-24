"""The remote mode of ``graph-admin``: commands answered by the admin API (contracts B14.15).

On a host whose config says ``mode = "remote"`` the click tree is the
same as locally, so argv is parsed here and usage errors need no server.
Every command the server runs gets a handler that uploads its inputs and
sends its parsed parameters by inventory name; the server's answer is
returned as it came (``Result.forwarded``):

- a file parameter (a policy) is uploaded;
- a directory parameter (an ontology, a skill) is uploaded file by file
  (every regular file below it; anything else is refused here) and named
  by its relative paths;
- ``graph skill render`` checks ``--out`` here, and writes the files the
  server answers into ``OUT/graph-<graph>/``.

``client`` commands keep their own handler, ``local`` ones too, which
refuse in remote mode (``admin.remote_mode``). ``--data-root`` names a
local data root, so it is refused as well. Admin writes carry no
idempotency key and are never retried: a lost answer says to check the
state before running the command again.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from graph_cli.app_cli.remote import command_line
from graph_cli.app_cli.remote_hooks import upload
from graph_cli.catalog.remote import RemoteSession, Write, protocol
from graph_cli.cli_kit.argv import request_args
from graph_cli.cli_kit.envelope import Forwarded, Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import SERVED, CommandSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.skills.tree import MAX_NAME, NAME_RE, write_tree
from graph_cli_admin.maintenance_cli.commands.common import DATA_ROOT

MAX_FILES: Final = 1000  # files of one uploaded directory, as the server takes them
RENDER: Final = ("graph", "skill", "render")
OUT: Final = "--out"


def remote_specs(specs: Sequence[CommandSpec]) -> tuple[CommandSpec, ...]:
    """The declarations with the remote handlers in place of the local ones."""
    return tuple(
        replace(spec, handler=_handler(spec)) if spec.remote in SERVED else spec for spec in specs
    )


def _handler(spec: CommandSpec) -> Callable[..., Result]:
    def handler(state: CommandState, **values: Any) -> Result:
        args = request_args(spec, values)
        if DATA_ROOT.name in args:
            raise GraphCliError(
                "admin.remote_mode",
                f"{DATA_ROOT.name} names a local data root, but this host is a client of a"
                " server; run maintenance on that data root where it lives",
                details={"argument": DATA_ROOT.name},
            )
        session: RemoteSession = state.services.remote(state.warnings)
        out = _local_out(args) if spec.path == RENDER else None
        for param in spec.params:
            value = args.get(param.name)
            if value is None or (param.name == OUT and out is not None):
                continue
            if param.type == "file":
                args[param.name] = _upload_file(session, str(value), param.name)
            elif param.type == "directory":
                args[param.name] = upload_tree(session, str(value), param.name)
        write = None
        if spec.kind == "write" and args.get("--dry-run") is not True:
            write = Write(None, command_line(spec, args))
        if out is not None:
            args[OUT] = {"files": {}, "name": str(values["out"])}
            document, _, _ = session.command(spec.path, args, text=False, write=write)
            return Result(forwarded=Forwarded(_write_rendered(document, out, values)))
        document, stdout, stderr = session.command(
            spec.path, args, text=not state.json_mode, write=write
        )
        return Result(forwarded=Forwarded(document, stdout, stderr))

    return handler


def _upload_file(session: RemoteSession, value: str, argument: str) -> dict[str, JsonValue]:
    path = Path(value)
    try:
        with path.open("rb"):
            pass
    except OSError as exc:  # as the command says locally
        raise _usage(argument, f"cannot read {value}: {exc.strerror or exc}") from exc
    return upload(session, path, value)


def upload_tree(session: RemoteSession, value: str, argument: str) -> dict[str, JsonValue]:
    """``{files: {relative path: upload}, name}`` of a local directory: every regular file
    below it, uploaded; a symlink or any other file kind is refused, never followed."""
    directory = Path(value)
    if not directory.is_dir():
        raise _usage(argument, f"{value} is not a directory")
    found: dict[str, Path] = {}
    for current, dirs, names in os.walk(directory, followlinks=False):
        dirs.sort()
        for name in sorted([*dirs, *names]):
            path = Path(current) / name
            relative = path.relative_to(directory).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise _usage(
                    argument,
                    f"{value}/{relative} is not a regular file; the remote mode uploads"
                    " regular files only",
                )
            found[relative] = path
    if len(found) > MAX_FILES:
        raise _usage(argument, f"{value} holds more than {MAX_FILES} files to upload")
    files: dict[str, JsonValue] = {
        relative: upload(session, path, relative)["upload"]
        for relative, path in sorted(found.items())
    }
    return {"files": files, "name": _name(value, directory)}


def _name(value: str, directory: Path) -> str:
    """The directory as given; its full path when the given form names no directory name
    (``.``), because the server names its copy after the last part."""
    last = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return str(directory.resolve()) if last in ("", ".", "..") else value


def _local_out(args: Mapping[str, JsonValue]) -> Path:
    """``--out`` of ``graph skill render`` on this machine, checked as locally."""
    out = Path(str(args[OUT])).resolve()
    if not out.is_dir():
        raise _usage("out", f"--out {out}: no such directory; create it first")
    return out


def _write_rendered(
    document: Mapping[str, JsonValue], out: Path, values: Mapping[str, Any]
) -> Mapping[str, JsonValue]:
    """Write the files the server rendered into ``OUT/graph-<graph>/`` and answer that path,
    as ``graph skill render`` does locally (``--force`` replaces an existing preview)."""
    data = document.get("data")
    rendered = data.get("rendered") if isinstance(data, Mapping) else None
    if not isinstance(data, Mapping) or not isinstance(rendered, Mapping):
        return document
    skill = data.get("skill")
    if not isinstance(skill, str) or len(skill) > MAX_NAME or not NAME_RE.match(skill):
        raise protocol(0, "the rendered skill has no valid skill name")  # never a path
    dest = out / skill
    exists = dest.is_symlink() or dest.exists()
    if exists and (not dest.is_dir() or any(dest.iterdir())) and not values.get("force"):
        graph = document.get("graph")
        raise GraphCliError(
            "admin.skill_render_exists",
            f"{dest} exists; pass --force to replace it",
            details={
                "graph": graph.get("name") if isinstance(graph, Mapping) else None,
                "dir": str(Path(str(values.get("source"))).resolve()),
                "out": str(out),
                "path": str(dest),
            },
        )
    try:
        files = {str(name): base64.b64decode(str(content)) for name, content in rendered.items()}
    except ValueError:
        raise protocol(0, "the rendered files are not base64") from None
    if not all(_inside(name) for name in files):  # checked before anything is replaced
        raise protocol(0, "a rendered file name is not a relative path")
    try:
        if exists:
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        write_tree(dest, files)
    except (OSError, ValueError) as exc:
        raise _usage("out", f"cannot write {dest}: {exc}") from exc
    shown = {key: value for key, value in data.items() if key != "rendered"}
    return {**document, "data": {**shown, "path": str(dest)}}


def _inside(name: str) -> bool:
    """A relative POSIX path that stays below the preview directory."""
    parts = name.split("/")
    return "\\" not in name and "\x00" not in name and all(p not in ("", ".", "..") for p in parts)


def _usage(argument: str, message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", message, details={"argument": argument})
