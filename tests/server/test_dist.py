"""C: graph-server hands out the programs it was built from (contracts B14.18).

The wheels are synthetic: a zip with the distribution's ``_build_id`` is all
the server reads. Requests go through ``ServerApp`` without a socket.
"""

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.openapi import answer_validator, app_document
from support.server import call, grant_server, new_token

from graph_cli import __version__
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

GRAPH_BUILD = "sha256:" + "a" * 64
ADMIN_BUILD = "sha256:" + "b" * 64
OTHER_BUILD = "sha256:" + "c" * 64
CONSTRAINTS = b"click==8.5.0\npyyaml==6.0.3\n"


def wheel(directory: Path, distribution: str, build: str | None) -> Path:
    path = directory / f"{distribution}-0.1.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{distribution}/__init__.py", "")
        if build is not None:
            archive.writestr(f"{distribution}/_build_id", build + "\n")
    return path


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    init(run_cli, isolated_env)
    grant_server(run_cli, tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheels = {
        "graph": wheel(dist, "graph_cli", GRAPH_BUILD),
        "graph-admin": wheel(dist, "graph_cli_admin", ADMIN_BUILD),
    }
    (dist / "constraints.txt").write_bytes(CONSTRAINTS)
    tokens = {
        "app": new_token(run_cli, "app"),
        "admin": new_token(run_cli, "root", "--scope", "admin"),
    }
    env = {**isolated_env, "GRAPH_CLI_DIST_DIR": str(dist)}
    return {"env": env, "dist": dist, "wheels": wheels, "tokens": tokens}


def served(
    installation: dict[str, Any], tmp_path: Path, builds: dict[str, str | None] | None = None
) -> ServerApp:
    settings = Settings.load(installation["env"], tmp_path)
    found = {"graph": GRAPH_BUILD, "graph-admin": ADMIN_BUILD} if builds is None else builds
    return ServerApp(settings, log=io.StringIO(), builds=found)


def test_the_manifest_names_each_wheel_without_a_token(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    reply = call(app, "GET", "/v1/dist/manifest")
    assert reply.status == 200
    manifest = reply.json
    assert manifest["schema"] == "graph_cli.dist/1"
    graph, admin = manifest["programs"]["graph"], manifest["programs"]["graph-admin"]
    path = installation["wheels"]["graph"]
    assert graph == {
        "distribution": "graph_cli",
        "version": "0.1.0.dev0",
        "build": GRAPH_BUILD,
        "requires": [],
        "access": "public",
        "file": path.name,
        "hash": sha(path),
        "size": path.stat().st_size,
    }
    assert admin["requires"] == ["graph"] and admin["access"] == "admin"
    assert admin["build"] == ADMIN_BUILD
    assert manifest["constraints"]["file"] == "constraints.txt"
    assert manifest["constraints"]["size"] == len(CONSTRAINTS)
    assert app.dist_problems == []


def test_the_graph_wheel_is_public_the_admin_wheel_needs_an_admin_token(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    graph, admin = installation["wheels"]["graph"], installation["wheels"]["graph-admin"]
    reply = call(app, "GET", f"/v1/dist/{graph.name}")
    assert reply.status == 200 and reply.body == graph.read_bytes()
    assert reply.headers["Content-Type"] == "application/octet-stream"
    assert call(app, "GET", "/v1/dist/constraints.txt").body == CONSTRAINTS
    assert call(app, "GET", f"/v1/dist/{admin.name}").status == 401
    as_app = call(app, "GET", f"/v1/dist/{admin.name}", token=installation["tokens"]["app"])
    assert as_app.status == 403 and as_app.error["details"]["required"] == "admin"
    as_admin = call(app, "GET", f"/v1/dist/{admin.name}", token=installation["tokens"]["admin"])
    assert as_admin.status == 200 and as_admin.body == admin.read_bytes()


def test_the_openapi_document_describes_the_dist_routes(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """B14.22 G4: a server that hands out programs describes ``/v1/dist/``, without a
    token for the manifest, with an optional one for a file; the answers fit."""
    app = served(installation, tmp_path)
    document = call(app, "GET", "/v1/openapi.json", token=installation["tokens"]["app"]).json
    assert document == app_document(__version__, enroll=False)
    paths = document["paths"]
    assert paths["/v1/dist/manifest"]["get"]["security"] == []
    assert paths["/v1/dist/{file}"]["get"]["security"] == [{}, {"bearer": []}]
    manifest = call(app, "GET", "/v1/dist/manifest")
    validator = answer_validator(document, "/v1/dist/manifest", "get", "200")
    assert not [e.message for e in validator.iter_errors(manifest.json)]
    graph = installation["wheels"]["graph"].name
    reply = call(app, "GET", f"/v1/dist/{graph}")
    assert reply.status == 200
    file = paths["/v1/dist/{file}"]["get"]["responses"]["200"]
    assert reply.headers["Content-Disposition"] == "attachment"
    assert set(file["content"]) == {reply.headers["Content-Type"]}
    refused = call(app, "GET", f"/v1/dist/{installation['wheels']['graph-admin'].name}")
    assert refused.status == 401
    validator = answer_validator(document, "/v1/dist/{file}", "get", "default")
    assert not [e.message for e in validator.iter_errors(refused.json)]


def test_unknown_files_and_other_methods_are_refused(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    for name in ("nothing.whl", "..", "%2e%2e%2fconfig.toml", "manifest.json"):
        assert call(app, "GET", f"/v1/dist/{name}").status == 404, name
    assert call(app, "GET", "/v1/dist/a/b").status == 401  # not a dist route: needs a token
    assert call(app, "POST", "/v1/dist/manifest").error["code"] == "usage.invalid_argument"


def test_dist_is_never_refused_for_a_build(installation: dict[str, Any], tmp_path: Path) -> None:
    """A client of another build fetches what it needs to catch up (B14.16)."""
    app = served(installation, tmp_path)
    header = {"Graph-Cli-Build": f"graph {OTHER_BUILD}"}
    assert call(app, "GET", "/v1/dist/manifest", headers=header).status == 200
    refused = call(
        app, "POST", "/v1/commands/whoami", token=installation["tokens"]["app"], headers=header
    )
    assert refused.error["code"] == "remote.client_outdated"
    assert refused.error["hint"] == "graph self update"


def test_a_wheel_of_another_build_is_not_handed_out(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """A client would install a program the server then refuses; graph-admin needs graph."""
    app = served(installation, tmp_path, {"graph": OTHER_BUILD, "graph-admin": None})
    reply = call(app, "GET", "/v1/dist/manifest")
    assert reply.status == 404
    assert "no programs" in reply.error["message"]
    assert len(app.dist_problems) == 2
    assert (
        "graph: graph_cli-0.1.0.dev0-py3-none-any.whl has build aaaaaaaaaaaa"
        in (app.dist_problems[0])
    )
    assert "graph-admin: needs graph" in app.dist_problems[1]


def test_without_wheels_or_with_dist_off_nothing_is_handed_out(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    env = {k: v for k, v in installation["env"].items() if k != "GRAPH_CLI_DIST_DIR"}
    app = ServerApp(Settings.load(env, tmp_path), log=io.StringIO())
    reply = call(app, "GET", "/v1/dist/manifest")
    assert reply.status == 404 and "GRAPH_CLI_DIST_DIR" in reply.error["message"]
    config = Path(installation["env"]["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "[server]\ndist = false\n")
    app = served(installation, tmp_path)
    reply = call(app, "GET", "/v1/dist/manifest")
    assert reply.status == 404 and "dist = false" in reply.error["message"]
    graph = installation["wheels"]["graph"]
    assert call(app, "GET", f"/v1/dist/{graph.name}").status == 404
