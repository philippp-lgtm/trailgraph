"""``POST /v1/enroll``: an enrollment code exchanged for a token (contracts B14.21).

Plugged into ``ServerApp`` by ``main``; besides the admin API the only
server module that imports the maintenance distribution, because an
exchange writes the token files under the registry lock, audited. The
request carries no bearer token and is never refused for a build: it is
how a client gets its token. The body ``{code, label?, check?}`` is at most
4 KiB. A wrong, used, revoked or expired code is ``remote.unauthorized``
(401) with one message for all; a code that is not listed is refused before
any context is built, and no wrong code takes a lock. ``check: true`` answers
what the code is for without using it, so the client can check its own
folder before the code is spent. The code is never logged or echoed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from graph_cli import __version__
from graph_cli.catalog.enrollments import find, parse_enrollments
from graph_cli.catalog.wiring import PROGRAM
from graph_cli.cli_kit.envelope import Result, envelope
from graph_cli.kernel.actor import ABSENT, ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.storage.files.token_files import enrollments_path, read_tokens_file
from graph_cli_admin.maintenance_cli.wiring import AdminRequest, Services
from graph_cli_admin.services_admin import enroll, error_codes
from graph_cli_server.app import SERVER_PROGRAM, Response, _json, _object, _read_json

if TYPE_CHECKING:
    from graph_cli_server.app import ServerApp

COMMAND: Final = "enroll"
MAX_BODY_BYTES: Final = 4096
CLIENT: Final = ClientInfo(SERVER_PROGRAM, __version__, Entrypoint.SERVER)
_KEYS: Final = frozenset({"code", "label", "check"})
# The services need an actor before the code names one; the exchange acts as the enrollment.
_NOBODY: Final = ActorContext(
    principal=ABSENT,
    agent=ABSENT,
    session=ABSENT,
    device=ABSENT,
    project=ABSENT,
    client=CLIENT,
    pid=None,
)


class Enrollment:
    def __init__(self, server: ServerApp) -> None:
        # graph-server always enrolls, so the public registry (GET /v1/errors) names the
        # admin codes whether or not the admin API is on, and does not tell which
        error_codes.register()
        self.server = server

    def dispatch(self, environ: Mapping[str, Any]) -> Response:
        try:
            code, label, check = _request(environ)
            settings = self.server.settings
            listed = parse_enrollments(read_tokens_file(enrollments_path(settings.data_root)))
            if find(listed, code) is None:  # before any context: a wrong code costs nothing
                raise enroll.refused()
            services = Services(settings.env, settings.cwd, request=AdminRequest(_NOBODY, ()))
            warnings: list[WarningRecord] = []
            ctx = services.context(data_root=None, warnings=warnings)
            data = enroll.exchange(ctx, code=code, label=label, check=check)
        except GraphCliError as exc:
            return self.server._error(COMMAND, exc)
        document = envelope(COMMAND, program=PROGRAM, result=Result(data=data))
        return _json(200, dict(document))


def _request(environ: Mapping[str, Any]) -> tuple[str, str | None, bool]:
    """``(code, label, check)`` of the body."""
    length = str(environ.get("CONTENT_LENGTH") or "0")
    if not length.isdigit() or int(length) > MAX_BODY_BYTES:
        raise _invalid(f"the body must have a Content-Length of at most {MAX_BODY_BYTES} bytes")
    body = _object(_read_json(environ), "body", _KEYS)
    code, label, check = body.get("code"), body.get("label"), body.get("check", False)
    if not isinstance(code, str):
        raise _invalid("code must be the enrollment code")
    if label is not None and not isinstance(label, str):
        raise _invalid("label must be a string")
    if not isinstance(check, bool):
        raise _invalid("check must be a boolean")
    return code, label, check


def _invalid(message: str) -> GraphCliError:
    details: dict[str, JsonValue] = {"argument": "body"}
    return GraphCliError("usage.invalid_input", message, details=details)
