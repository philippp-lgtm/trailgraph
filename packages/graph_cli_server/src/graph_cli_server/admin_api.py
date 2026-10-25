"""The admin API of ``graph-server``: the ``graph-admin`` commands over HTTP (contracts B14.15).

Plugged into ``ServerApp`` by ``main`` when ``[server] admin_api`` is on;
the only server module that imports the maintenance distribution. Routes
are generated from the ``graph-admin`` declarations like the app's: every
command the server runs (``remote`` is ``server`` or ``hooked``) is ``POST
/v1/admin/commands/<path>``, run by the same click program with one admin
``Services`` per request. The request acts through the entrypoint ``maint``
with the token's principal, so the installed policy's maintainers decide
and every change is audited as locally. Before any command runs, the
request must be allowed ``maint.audit_read`` on ``installation`` (a denial
is audited): locally only who can read the data root runs ``graph-admin``
at all, so the installation reads (``token list``, ``policy show``)
authorize nothing themselves; over HTTP an admin token that is no
maintainer reaches none of them. A project admin (an admin token bound to
a project) passes instead when the command is one of ``PROJECT_COMMANDS``
and the project maintains at least one graph; every other command is
refused by the ``audit_read`` decision, which the ceiling denies it. Each
of those commands authorizes its graph itself and shows a project admin
only the graphs it maintains (``AdminContext.resolve``).

``--data-root`` is not part of the API, and every file or directory
parameter takes uploads only: the server never takes a path from a
request. A directory is rebuilt from uploaded files below the request
directory. ``graph skill render`` writes into an empty directory there and
answers the files it wrote (``rendered``, base64) instead of a path. Paths
of the request directory never reach the answer: they are replaced by the
client's arguments.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import click

import graph_cli_admin
from graph_cli import __version__
from graph_cli.catalog.build import read_build
from graph_cli.catalog.identity.request import request_actor
from graph_cli.catalog.tokens import TokenRecord
from graph_cli.cli_kit.argv import inventory_args, to_argv
from graph_cli.cli_kit.envelope import envelope
from graph_cli.cli_kit.runner import Completed, Invocation, run
from graph_cli.cli_kit.spec import SERVED, CommandSpec
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.records import RequestMeta
from graph_cli.skills.tree import read_tree
from graph_cli_admin.maintenance_cli import hints
from graph_cli_admin.maintenance_cli.commands import all_specs
from graph_cli_admin.maintenance_cli.commands.common import DATA_ROOT
from graph_cli_admin.maintenance_cli.commands.meta import inventory
from graph_cli_admin.maintenance_cli.main import program
from graph_cli_admin.maintenance_cli.wiring import PROGRAM, AdminRequest, Services
from graph_cli_admin.services_admin import error_codes
from graph_cli_server.app import (
    SERVER_PROGRAM,
    Outcome,
    Response,
    Staged,
    _json,
    _object,
    _read_json,
    answer,
    status_of,
    with_header_key,
)
from graph_cli_server.openapi import openapi

if TYPE_CHECKING:
    from graph_cli_server.app import ServerApp

CLIENT: Final = ClientInfo(SERVER_PROGRAM, __version__, Entrypoint.MAINT)
COMMANDS: Final = "/v1/admin/commands/"
RETURNED: Final = {("graph", "skill", "render"): "rendered", ("graph", "pull"): "pulled"}
GATE: Final = ("maint.audit_read", "installation")  # every admin request: a maintainer
# What a project admin may run: commands on one graph that authorize it themselves, the
# graph list (filtered) and the pure diff. Anything new stays with the maintainers.
PROJECT_COMMANDS: Final = frozenset(
    {
        "files verify",
        "graph check",
        "graph list",
        "graph pull",
        "graph push",
        "graph reindex",
        "graph show",
        "graph skill publish",
        "graph skill render",
        "ontology diff",
        "ontology release",
    }
)
_BODY_KEYS: Final = frozenset({"args", "actor"})


def served(spec: CommandSpec) -> CommandSpec:
    """A declaration as the API takes it: without ``--data-root``."""
    return replace(spec, params=tuple(p for p in spec.params if p.name != DATA_ROOT.name))


class AdminApi:
    def __init__(self, server: ServerApp) -> None:
        error_codes.register()
        hints.register()
        self.server = server
        server.builds.setdefault(PROGRAM, read_build(graph_cli_admin.__file__))
        self.program: click.Group = program()
        self.declared = tuple(served(spec) for spec in all_specs())
        self.specs: dict[str, CommandSpec] = {
            "/".join(spec.path): spec for spec in self.declared if spec.remote in SERVED
        }

    def dispatch(
        self, environ: Mapping[str, Any], method: str, parts: list[str], token: TokenRecord
    ) -> Response:
        server = self.server
        if parts == ["inventory"]:
            server._method(method, "GET")
            return _json(200, inventory(self.declared))
        if parts == ["openapi.json"]:
            server._method(method, "GET")
            document = openapi(
                inventory(self.declared, output_schemas=True),
                version=__version__,
                commands=COMMANDS,
                admin=True,
                public_url=server.settings.public_url,
            )
            return _json(200, document)
        if len(parts) > 1 and parts[0] == "commands":
            spec = self.specs.get("/".join(parts[1:]))
            if spec is None:
                return server._unknown("/v1/admin/" + "/".join(parts))
            server._method(method, "POST")
            render = "text" if "render=text" in str(environ.get("QUERY_STRING", "")) else "json"
            command = " ".join(spec.path)
            try:
                body = _object(_read_json(environ), "body", _BODY_KEYS)
            except GraphCliError as exc:
                return _failed(command, exc)
            key = environ.get("HTTP_IDEMPOTENCY_KEY")
            outcome = self.run_command(spec, body, token, None if key is None else str(key))
            return answer(outcome, render)
        return server._unknown("/v1/admin/" + "/".join(parts))

    def run_command(
        self,
        spec: CommandSpec,
        body: Mapping[str, JsonValue],
        token: TokenRecord,
        idempotency_key: str | None = None,
    ) -> Outcome:
        """Run one admin command for a request; every error becomes an envelope. No admin
        command takes an idempotency key, so ``idempotency_key`` (the header) is refused."""
        command = " ".join(spec.path)
        args = body.get("args", {})
        staged: list[Staged] = []
        uploads = self.server.uploads
        try:
            if not isinstance(args, Mapping):
                raise GraphCliError(
                    "usage.invalid_input", "args must be an object", details={"argument": "args"}
                )
            asserted = body.get("actor")
            if asserted is not None and not isinstance(asserted, Mapping):
                raise GraphCliError(
                    "usage.invalid_input", "actor must be an object", details={"argument": "actor"}
                )
            if idempotency_key is not None:
                args = with_header_key(spec, inventory_args(spec, args), idempotency_key)
            actor, warnings = request_actor(asserted, token=token, client=CLIENT)
            self._gate(command, AdminRequest(actor, warnings))
            with uploads.request_dir(token.name) as directory:
                stage = self.server.stager(token, directory, staged)
                argv = to_argv(spec, args, stage, directories=True)
                done = self._run(argv, AdminRequest(actor, warnings))
                document = done.document
                if spec.path in RETURNED:
                    document = _returned(document, RETURNED[spec.path])
        except GraphCliError as exc:
            failed = envelope(command, program=PROGRAM, error=exc)
            return Outcome(failed, None, *status_of(failed))
        finally:
            self.server.release(token, staged)
        restored = _restore(document, staged)
        assert isinstance(restored, Mapping)
        text = _restore(done.text, staged) if done.text is not None else None
        return Outcome(restored, text if isinstance(text, str) else None, *status_of(restored))

    def _gate(self, command: str, request: AdminRequest) -> None:
        """Only a maintainer, or a project admin that maintains a graph running a project
        command, reaches an admin command; a denial is audited and raised."""
        settings = self.server.settings
        services = Services(settings.env, settings.cwd, request=request)
        found: list[WarningRecord] = []
        ctx = services.context(data_root=None, warnings=found)
        if (
            ctx.project_admin
            and command in PROJECT_COMMANDS
            and ctx.manifests(include_archived=True)
        ):
            return
        ctx.authorize([GATE], request=RequestMeta.of(command, {}))

    def _run(self, argv: list[str], request: AdminRequest) -> Completed:
        settings = self.server.settings
        services = Services(settings.env, settings.cwd, request=request)
        done: list[Completed] = []
        out, err = io.StringIO(), io.StringIO()
        run(
            self.program,
            argv,
            Invocation(PROGRAM, json_default=True),
            services=services,
            out=out,
            err=err,
            after=done.append,
        )
        if not done:
            raise GraphCliError("internal.error", "the command gave no answer")
        return done[0]


def _failed(command: str, error: GraphCliError) -> Response:
    document = envelope(command, program=PROGRAM, error=error)
    status, exit_code = status_of(document)
    return _json(status, document, exit_code=exit_code)


def _returned(document: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue]:
    """``graph skill render`` and ``graph pull`` over the API: the files they wrote under
    ``key`` instead of their place on the server (``path: null``); the client writes them
    into its own directory."""
    data = document.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("path"), str):
        return document
    files, _ = read_tree(Path(str(data["path"])))
    rendered: dict[str, JsonValue] = {
        name: base64.b64encode(content).decode() for name, content in sorted(files.items())
    }
    return {**document, "data": {**data, "path": None, key: rendered}}


def _restore(value: JsonValue, staged: list[Staged]) -> JsonValue:
    """Every string of an answer names the client's arguments, not the staged paths."""
    if not staged:
        return value
    pairs = sorted(
        {
            (form, item.name)
            for item in staged
            for form in (str(item.link), str(item.link.resolve()))
        },
        key=lambda pair: -len(pair[0]),
    )

    def fix(found: JsonValue) -> JsonValue:
        if isinstance(found, str):
            for path, name in pairs:
                found = found.replace(path, name)
            return found
        if isinstance(found, list):
            return [fix(item) for item in found]
        if isinstance(found, Mapping):
            return {key: fix(item) for key, item in found.items()}
        return found

    return fix(value)
