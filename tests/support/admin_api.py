"""The admin API of graph-server in-process: a served app, a command call, an upload (S tests)."""

import io
from pathlib import Path
from typing import Any

from graph_cli_server.admin_api import AdminApi
from graph_cli_server.app import ServerApp, Settings
from support.server import call


def served(installation: dict[str, Any], cwd: Path, *, with_admin: bool = True) -> ServerApp:
    settings = Settings.load(installation["env"], cwd)
    return ServerApp(settings, log=io.StringIO(), admin=AdminApi if with_admin else None)


def command(app: ServerApp, path: str, token: str, **body: Any) -> Any:
    """``POST /v1/admin/commands/<path>`` with ``body`` (``args``, ``actor``)."""
    return call(app, "POST", f"/v1/admin/commands/{path}", token=token, body=body)


def upload_tree(app: ServerApp, token: str, directory: Path) -> dict[str, Any]:
    """The directory argument of a command: every regular file below it, uploaded."""
    files = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        reply = call(app, "PUT", "/v1/uploads", token=token, raw=path.read_bytes())
        assert reply.status == 200, reply.json
        files[path.relative_to(directory).as_posix()] = reply.json["upload"]
    return {"files": files, "name": str(directory)}
