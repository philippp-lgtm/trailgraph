"""``graph connect``: set up a machine and a folder from an enrollment code (contracts B14.21).

One command takes a client from a one-time code to a working folder: it
asks the server what the code is for (``POST /v1/enroll`` with ``check``,
which uses nothing), checks everything here that could stand in the way
(the config, the folder's pointer, a token stored before), and only then
spends the code for a token bound to its project. It stores the token under
the config directory per project (never in the folder, whose pointer may be
in git), writes the remote mode into ``config.toml`` and a pointer into the
folder, installs the program from the server (also when run by ``uvx``) and
lets the installed program fetch the project's graph skills and run ``self
update``, which compares its build with the server's. An admin code sets up
``graph-admin`` with a project admin's token instead; it gets no skills.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import graph_cli
from graph_cli import __version__
from graph_cli.app_cli.commands.common import PROGRAM, services
from graph_cli.app_cli.commands.self_update import program_of, refresh
from graph_cli.catalog.build import read_build
from graph_cli.catalog.config import load_config, remote_config
from graph_cli.catalog.dist import Installer, bootstrap, run_program, tool_env
from graph_cli.catalog.endpoint import Endpoint, parse_endpoint
from graph_cli.catalog.enrollments import host_label, is_code
from graph_cli.catalog.pointer import POINTER_NAME, find_pointer, format_pointer
from graph_cli.catalog.remote import (
    ADMIN_TOKEN_FILE,
    APP_TOKEN_FILE,
    RemoteClient,
    envelope_of,
    error_of,
    project_token_path,
    protocol,
    write_token_file,
)
from graph_cli.catalog.resolve import config_path
from graph_cli.catalog.tokens import ADMIN_SCOPE, APP_SCOPE
from graph_cli.catalog.wiring import AppServices
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GRAPH_NAME_RE, Uuid7Provider, format_id
from graph_cli.storage.files.durable import OsSyncer, ensure_dir, write_temp

ENROLL_ROUTE: Final = "/v1/enroll"
TIMEOUT_S: Final = 60.0
ADMIN_PROGRAM: Final = "graph-admin"


def connect(
    state: CommandState,
    endpoint: str,
    code: str,
    dir: str | None,
    label: str | None,
    force: bool,
) -> Result:
    app = services(state)
    directory = app.path(dir) if dir is not None else app.cwd.resolve()
    target = _endpoint(endpoint)
    code = code.strip().lower()
    if not is_code(code):
        raise _usage("--code", "not an enrollment code (gce_ and 26 letters and digits)")
    client = RemoteClient(
        target,
        None,
        timeout_s=TIMEOUT_S,
        version=__version__,
        program=PROGRAM,
        build=read_build(graph_cli.__file__),
        warnings=state.warnings,
    )
    enrollment = _enroll(client, {"code": code, "check": True}).get("enrollment")
    if not isinstance(enrollment, Mapping):
        raise protocol(200, "the answer names no enrollment")
    project, scope = enrollment.get("project"), enrollment.get("scope")
    if not isinstance(project, str) or scope not in (APP_SCOPE, ADMIN_SCOPE):
        raise protocol(200, "the answer names no project and scope")
    offered = _graphs(enrollment.get("graphs"))

    # Everything that could stand in the way is checked before the code is spent.
    config_file = config_path(app.env)
    config_data, config_action = _config(config_file, endpoint, target, force)
    pointer, _ = find_pointer(directory)
    if pointer is not None and pointer.project != project:
        held = f"names project {pointer.project}" if pointer.project else "names no project"
        raise _refused(
            "pointer",
            f"{pointer.path} {held}, the code is for project {project}; set project ="
            f' "{project}" in it, or connect another folder',
            pointer.path,
        )
    if pointer is None and not offered:
        raise _refused(
            "no_graphs",
            f"a token of project {project} would reach no graph yet, so no pointer can be"
            " written; ask the maintainer to grant the project a graph (the code is unused)",
            None,
        )
    token_file = project_token_path(
        config_file,
        target,
        project,
        ADMIN_TOKEN_FILE if scope == ADMIN_SCOPE else APP_TOKEN_FILE,
    )
    if token_file is None:
        raise protocol(200, "the enrollment names no usable project")
    if token_file.exists() and not force:
        raise _refused(
            "token",
            f"a token of project {project} is stored at {token_file} already; --force replaces"
            " it (revoke the old one with the maintainer)",
            token_file,
        )

    # an admin code's token gets its own default, so it never clashes with the app token
    default = host_label() + ("-admin" if scope == ADMIN_SCOPE else "")
    chosen = label if label is not None else default
    try:
        answer = _enroll(client, {"code": code, "label": chosen})
    except GraphCliError as exc:
        if exc.code != "usage.invalid_argument" or exc.details.get("argument") != "label":
            raise
        raise _usage(
            "--label",
            f"the server refused the token name {exc.details.get('actual')} (taken, or named"
            " by its policy); pass another --label (the code is still unused)",
        ) from None
    secret, token_name = answer.get("token"), answer.get("name")
    if (
        not isinstance(secret, str)
        or not secret.isascii()
        or not isinstance(token_name, str)
        or (answer.get("project"), answer.get("scope")) != (project, scope)
    ):
        raise protocol(200, "the answer carries no token of the checked project")
    graphs = _graphs(answer.get("graphs")) or offered
    write_token_file(token_file, secret)
    if config_data is not None:
        _write(config_file, config_data)
    pointer_file, pointer_action = (pointer.path, "kept") if pointer is not None else (None, None)
    if pointer is None:
        pointer_file, pointer_action = directory / POINTER_NAME, "created"
        _write(pointer_file, format_pointer(project, graphs))

    data: dict[str, JsonValue] = {
        "endpoint": target.display,
        "project": project,
        "scope": scope,
        "token": {"name": token_name, "file": str(token_file)},
        "config": {"file": str(config_file), "action": config_action},
        "pointer": {"file": str(pointer_file), "action": pointer_action, "graphs": list(graphs)},
    }
    error = _install(app, state, target, secret, scope, directory, data)
    return Result(data=data, error=error, text=_text(data))


def _install(
    app: AppServices,
    state: CommandState,
    target: Endpoint,
    secret: str,
    scope: str,
    directory: Path,
    data: dict[str, JsonValue],
) -> GraphCliError | None:
    """The program from the server (unless this one is a checkout or the uv tool itself),
    the project's skills (app scope) and the installed program's ``self update``."""
    program = ADMIN_PROGRAM if scope == ADMIN_SCOPE else PROGRAM
    command = program_of(app) if program == PROGRAM else _admin_module()
    step: dict[str, JsonValue] = {"name": program, "action": "skipped", "reason": "no_build"}
    if read_build(graph_cli.__file__) is not None:
        step = {"name": program, "action": "self_update"}
        if program != PROGRAM or tool_env(Path(sys.prefix)) is None:
            _, roots, _ = app.remote_roots()
            client = RemoteClient(
                target,
                secret,
                timeout_s=TIMEOUT_S,
                version=__version__,
                program=PROGRAM,
                build=read_build(graph_cli.__file__),
                warnings=state.warnings,
            )
            step, launcher = bootstrap(
                client, program, state_root=roots.state_root, installer=Installer(app.env)
            )
            command = [str(launcher)]
    data["program"] = step
    errors: list[GraphCliError] = []
    data["skills"] = []
    if program == PROGRAM:
        (directory / ".claude").mkdir(exist_ok=True)  # the folder is set up for an agent
        item, error = refresh(app, "project", directory, check=False, force=False, command=command)
        data["skills"] = [item]
        errors += [error] if error is not None else []
    words = ["self", "update"] + (["--dir", str(directory)] if program == PROGRAM else [])
    updated, _ = run_program([*command, *words], app.env, directory)
    found = updated.get("data")
    if isinstance(found, Mapping):
        data["self_update"] = {"program": found.get("program"), "changed": found.get("changed")}
        if step["action"] == "self_update":  # this program, a uv tool: self update replaced it
            data["program"] = found.get("program")
    if updated.get("ok") is not True:
        errors.append(error_of(updated))
    return errors[0] if errors else None


def _admin_module() -> list[str]:
    return [sys.executable, "-m", "graph_cli_admin"]


def _endpoint(text: str) -> Endpoint:
    found = parse_endpoint(text)
    if isinstance(found, str):
        raise _usage("endpoint", found)
    if found.plain_http and not found.loopback:
        raise _usage(
            "endpoint",
            "is plain http:// to a host that is not loopback: the code and the token would"
            " travel in clear; use https://",
        )
    return found


def _enroll(client: RemoteClient, body: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """The data of a ``POST /v1/enroll`` answer; its error raised as if it happened here."""
    answer = client.call("POST", ENROLL_ROUTE, body=json.dumps(dict(body)).encode())
    document = envelope_of(answer)
    if document.get("ok") is not True:
        raise error_of(document)
    data = document.get("data")
    if not isinstance(data, Mapping):
        raise protocol(answer.status, "the answer carries no data")
    return data


def _config(file: Path, given: str, target: Endpoint, force: bool) -> tuple[bytes | None, str]:
    """The config file to write (``None``: as it is) and what happens to it. Another mode
    or endpoint is replaced only with ``--force``: it changes where every graph command of
    this user goes."""
    try:
        existing = file.read_bytes()
    except FileNotFoundError:
        device = format_id(Uuid7Provider(SystemClock()).new_id())
        return remote_config(None, path=file, endpoint=given, device_id=device), "created"
    except OSError as exc:
        raise GraphCliError(
            "config.invalid",
            f"{file} cannot be read: {exc.strerror or exc}",
            details={"file": str(file), "key": ""},
        ) from exc
    config, _ = load_config(file)
    current = parse_endpoint(config.endpoint or "")
    if config.mode == "remote" and isinstance(current, Endpoint) and current == target:
        return None, "unchanged"
    if not force:
        now = f"the remote mode with {config.endpoint}" if config.mode == "remote" else "local"
        raise _refused(
            "config",
            f"{file} runs graph {now}; --force switches every graph command of this user to"
            f" {target.display}",
            file,
        )
    return remote_config(existing, path=file, endpoint=given, device_id=""), "updated"


def _write(path: Path, data: bytes) -> None:
    syncer = OsSyncer("full")
    try:
        ensure_dir(path.parent, syncer)
        temp = write_temp(path, data, syncer)
        os.replace(temp, path)
        syncer.sync_dir(path.parent)
    except OSError as exc:
        raise GraphCliError(
            "config.invalid",
            f"cannot write {path}: {exc.strerror or exc}",
            details={"file": str(path), "key": ""},
        ) from exc


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _graphs(value: JsonValue) -> list[str]:
    """Graph names a server answered; anything else is no answer of the API."""
    found = _list(value)
    if not all(isinstance(g, str) and GRAPH_NAME_RE.fullmatch(g) for g in found):
        raise protocol(200, "the answer names graphs that are no graph names")
    return [str(g) for g in found]


def _text(data: Mapping[str, JsonValue]) -> str:
    token, config, pointer = data["token"], data["config"], data["pointer"]
    assert isinstance(token, Mapping) and isinstance(config, Mapping)
    assert isinstance(pointer, Mapping)
    lines = [
        f"Connected to {data['endpoint']} for project {data['project']} (scope {data['scope']}).",
        f"Token {token['name']} stored at {token['file']}.",
        f"Config {config['file']}: {config['action']}.",
        f"Pointer {pointer['file']}: {pointer['action']}; graphs:"
        f" {', '.join(str(g) for g in _list(pointer['graphs'])) or 'none'}.",
    ]
    program = data.get("program")
    if isinstance(program, Mapping):
        lines.append(f"Program {program.get('name')}: {program.get('action')}.")
    return "\n".join(lines) + "\n"


def _usage(argument: str, message: str) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument},
    )


def _refused(reason: str, message: str, file: Path | None) -> GraphCliError:
    details: dict[str, JsonValue] = {"reason": reason, "file": str(file) if file else None}
    return GraphCliError("connect.refused", message, details=details)


SPECS: Final = (
    CommandSpec(
        path=("connect",),
        kind="meta",
        surface="meta",
        action=None,
        summary="Set up this machine and a folder for a server, from a one-time enrollment code.",
        params=(
            ParamSpec(
                "endpoint",
                "string",
                "The server's URL: https://, or http:// to this machine.",
                required=True,
                positional=True,
            ),
            ParamSpec(
                "--code",
                "string",
                "The one-time code the maintainer gave you.",
                required=True,
            ),
            ParamSpec(
                "--dir",
                "directory",
                "The folder to set up; default: the working one.",
                must_exist=True,
            ),
            ParamSpec(
                "--label",
                "string",
                "The new token is named after the project and this label; default: the host name.",
            ),
            ParamSpec(
                "--force",
                "flag",
                "Replace another endpoint in config.toml, and a stored token.",
            ),
        ),
        examples=(
            Example("graph connect <value> --code <value>", "Set up the working folder."),
            Example("graph connect <value> --code <value> --dir <value>", "Another folder."),
        ),
        errors=(
            "config.invalid",
            "config.forbidden_key",
            "connect.refused",
            "remote.unreachable",
            "remote.unauthorized",
            "remote.protocol",
            "update.unavailable",
            "update.unsupported_install",
            "update.install_failed",
            "skills.modified",
            "skills.not_owned",
            "skills.target_invalid",
            "skills.check_failed",
        ),
        handler=connect,
        remote="client",
        description="The code works once, for its project. connect checks the config, the"
        " folder's pointer and the token store before it uses the code, then installs the"
        " program and the project's graph skills. Never run it for the user: the code is"
        " theirs.",
    ),
)
