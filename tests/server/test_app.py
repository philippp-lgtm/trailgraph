"""C: the WSGI app of graph-server in-process (contracts B14.2-B14.8, B14.14).

The installation is prepared with the real ``graph-admin`` (policy grant,
tokens); requests go through ``ServerApp`` without a socket.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.files import synthetic_pdf
from support.fixtures import FIXTURES
from support.server import actor_header, call, grant_server, new_token

from graph_cli.kernel.canonical import hash_bytes
from graph_cli.kernel.ports.catalog import skill_hash
from graph_cli.skills.tree import read_tree
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

LOCATION = {"type": "inv:Location", "--set": ["name=Shelf", "code=S1"]}
GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "files_graph", "notes_and_sources", version="1.1.0")
    grant_server(run_cli, tmp_path)
    tokens = {
        "ci": new_token(run_cli, "ci"),
        "bound": new_token(run_cli, "bound", "--project", "example_project"),
        "stranger": new_token(run_cli, "stranger"),
    }
    return {"root": root, "env": isolated_env, "tokens": tokens}


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> tuple[ServerApp, io.StringIO]:
    log = io.StringIO()
    settings = Settings.load(installation["env"], tmp_path)
    return ServerApp(settings, log=log), log


def command(app: ServerApp, path: str, token: str, **body: Any) -> Any:
    return call(app, "POST", f"/v1/commands/{path}", token=token, body=body)


def audit_events(root: Path) -> list[dict[str, Any]]:
    """Every event of every audit stream."""
    found: list[dict[str, Any]] = []
    for segment in sorted(p for p in root.rglob("*.jsonl") if "audit" in p.parts):
        found += [json.loads(line) for line in segment.read_text().splitlines()]
    return found


def test_health_needs_no_token_and_the_rest_does(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, log = served
    health = call(app, "GET", "/v1/health")
    assert health.status == 200
    assert health.json["ok"] is True and health.json["api"] == "graph_cli.api/1"
    for token in (None, "gct_unknown", "not-a-token"):
        refused = call(app, "POST", "/v1/commands/whoami", token=token, body={})
        assert refused.status == 401
        assert refused.headers["WWW-Authenticate"] == "Bearer"
        assert refused.error["code"] == "remote.unauthorized"
        assert refused.headers["Graph-Cli-Exit-Code"] == "3"
    lines = log.getvalue().splitlines()
    assert [line.split()[-3:] for line in lines] == [
        ["200", line.split()[-2], "token=-"]
        if "health" in line
        else ["401", line.split()[-2], "token=-"]
        for line in lines
    ]
    assert len(lines) == 4


def test_whoami_is_the_token_with_client_facets(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    actor = {"agent": {"id": "agent-1", "type": "coding_agent"}, "session": {"id": "s-1"}}
    reply = command(app, "whoami", installation["tokens"]["ci"], actor=actor)
    assert reply.status == 200, reply.json
    seen = reply.json["data"]["actor"]
    assert seen["principal"] == {
        "assurance": "verified",
        "id": "token:ci",
        "name": "ci",
        "source": "token",
    }
    assert seen["client"]["entrypoint"] == "server"
    assert seen["agent"]["source"] == "client"
    assert reply.headers["Graph-Cli-Exit-Code"] == "0"


def test_write_read_and_text_rendering(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, log = served
    token = installation["tokens"]["ci"]
    created = command(app, "create", token, args={**LOCATION, "--graph": "example_graph"})
    assert created.status == 200, created.json
    entity = created.json["data"]["entities"][0]["id"]
    found = command(
        app,
        "get",
        token,
        args={"ref": entity},
        pointer={"graphs": ["example_graph"], "default": "example_graph"},
    )
    assert found.status == 200, found.json
    assert found.json["data"]["entity"]["attrs"]["name"] == "Shelf"
    text = call(
        app,
        "POST",
        "/v1/commands/describe/types",
        token=token,
        body={"graph_env": "example_graph"},
        query="render=text",
    )
    assert text.status == 200
    assert set(text.json) == {"envelope", "stdout", "stderr"}
    assert "inv:Location" in text.json["stdout"]
    assert token not in log.getvalue() and "Shelf" not in log.getvalue()
    events = [e for e in audit_events(installation["root"]) if "change" in e]
    assert any(e["actor"]["client"]["entrypoint"] == "server" for e in events)


def test_usage_errors_unknown_routes_and_methods(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    token = installation["tokens"]["ci"]
    unknown = command(app, "find", token, args={"--nope": 1})
    assert unknown.status == 400 and unknown.error["code"] == "usage.invalid_argument"
    assert unknown.json["command"] == "find"
    missing = command(app, "get", token, args={})
    assert missing.status == 400 and missing.error["code"] == "usage.missing_argument"
    not_json = call(app, "POST", "/v1/commands/whoami", token=token, raw=b"{")
    assert not_json.status == 400 and not_json.error["code"] == "usage.invalid_input"
    extra = command(app, "whoami", token, cwd="/")
    assert extra.status == 400 and extra.error["code"] == "usage.invalid_input"
    for path in ("/v1/commands/describe/commands", "/v1/commands/skills/install", "/other"):
        assert call(app, "POST", path, token=token, body={}).status == 404
    wrong = call(app, "GET", "/v1/commands/whoami", token=token)
    assert wrong.status == 400 and wrong.error["code"] == "usage.invalid_argument"
    plain = command(app, "apply", token, args={"file": "/etc/hosts"})
    assert plain.status == 400 and plain.error["details"]["argument"] == "file"


def test_the_server_never_reads_stdin_and_hides_its_paths(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    """Security review: ``query -`` would read the server's stdin; ``file path`` would
    show where the server keeps a file."""
    app, log = served
    token = installation["tokens"]["ci"]
    stdin = command(app, "query", token, args={"text": "-", "--graph": "example_graph"})
    assert stdin.status == 400 and stdin.error["details"] == {"argument": "text"}
    forged = call(app, "GET", "/v1/x\ngraph-server 2026 POST /v1/commands/create 200 1ms token=x")
    assert forged.status == 401
    assert len(log.getvalue().splitlines()) == 2
    assert "\\x0a" in log.getvalue()


def test_denials_are_audited_and_projects_verified(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    tokens = installation["tokens"]
    denied = command(
        app, "create", tokens["stranger"], args={**LOCATION, "--graph": "example_graph"}
    )
    assert denied.status == 404 and denied.error["code"] == "graph.not_found"
    denials = [e for e in audit_events(installation["root"]) if e["result"]["status"] == "denied"]
    assert denials and denials[-1]["actor"]["principal"]["id"] == "token:stranger"

    bound = tokens["bound"]
    seen = command(app, "whoami", bound)
    assert seen.json["data"]["actor"]["project"] == {
        "assurance": "verified",
        "id": "example_project",
        "source": "token",
    }
    other = command(app, "whoami", bound, actor={"project": "another_project"})
    assert other.status == 403 and other.error["code"] == "access.project_mismatch"


def test_inventory_and_openapi(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    token = installation["tokens"]["ci"]
    inventory = call(app, "GET", "/v1/inventory", token=token).json
    assert inventory["schema"] == "graph_cli.inventory/1"
    document = call(app, "GET", "/v1/openapi.json", token=token).json
    assert document["openapi"] == "3.1.0"
    paths = document["paths"]
    assert "/v1/commands/create" in paths and "/v1/commands/file/add" in paths
    assert "/v1/commands/describe/commands" not in paths
    assert not [p for p in paths if p.startswith("/v1/commands/skills")]
    schema = paths["/v1/commands/create"]["post"]["requestBody"]["content"]["application/json"]
    assert schema["schema"]["properties"]["args"]["required"] == ["type"]


def test_upload_then_apply(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    token = installation["tokens"]["ci"]
    document = {
        "ops": [
            {
                "op": "create",
                "type": "inv:Location",
                "local": "s",
                "attrs": {"name": "Bin", "code": "B1"},
            }
        ]
    }
    data = json.dumps(document).encode()
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=data)
    assert uploaded.status == 200, uploaded.json
    assert uploaded.json == {"upload": hash_bytes(data), "size": len(data)}
    reference = {"upload": uploaded.json["upload"], "name": "changes.json"}
    applied = command(app, "apply", token, args={"file": reference, "--graph": "example_graph"})
    assert applied.status == 200, applied.json
    assert applied.json["data"]["applied"] == 1
    again = command(app, "apply", token, args={"file": reference, "--graph": "example_graph"})
    assert again.status == 400 and "upload it again" in again.error["message"]
    other = call(app, "PUT", "/v1/uploads", token=installation["tokens"]["bound"], raw=data)
    stranger = command(
        app,
        "apply",
        installation["tokens"]["ci"],
        args={"file": {**reference, "upload": other.json["upload"]}},
    )
    assert stranger.status == 400  # uploads are kept per token


def test_file_add_with_origin_and_download(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    token = installation["tokens"]["ci"]
    content = synthetic_pdf()
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=content).json
    reference = {
        "upload": uploaded["upload"],
        "name": "scans/report 2024.pdf",
        "origin": {"root": "notes", "path": "scans/report 2024.pdf"},
    }
    added = command(app, "file/add", token, args={"path": reference, "--graph": "files_graph"})
    assert added.status == 200, added.json
    stored = added.json["data"]["file"]
    assert stored["file_name"] == "report 2024.pdf"
    assert stored["media_type"] == "application/pdf"
    assert stored["origin"] == {"root": "notes", "path": "scans/report 2024.pdf"}
    entity = added.json["data"]["entity"]["id"]
    shown = command(app, "file/path", token, args={"ref": entity, "--graph": "files_graph"})
    assert shown.status == 200 and shown.json["data"]["path"] is None
    download = call(
        app,
        "GET",
        f"/v1/graphs/files_graph/files/{entity}",
        token=token,
        headers={"Graph-Cli-Actor": actor_header({"agent": {"id": "agent-1"}})},
    )
    assert download.status == 200
    assert download.body == content
    assert download.headers["Graph-Cli-Content-Hash"] == hash_bytes(content)
    assert download.headers["Content-Type"] == "application/octet-stream"
    missing = call(app, "GET", "/v1/graphs/files_graph/files/deadbeefdeadbeef", token=token)
    assert missing.status == 404 and missing.error["code"] == "entity.not_found"


def test_graph_skill_endpoint(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    app, _ = served
    token = installation["tokens"]["ci"]
    reply = call(app, "POST", "/v1/graphs/example_graph/skill", token=token, body={})
    assert reply.status == 404 and reply.error["code"] == "skills.source_missing"
    hidden = call(
        app,
        "POST",
        "/v1/graphs/example_graph/skill",
        token=installation["tokens"]["stranger"],
        body={},
    )
    assert hidden.status == 404 and hidden.error["code"] == "graph.not_found"


def test_cors(installation: dict[str, Any], tmp_path: Path) -> None:
    config = Path(installation["env"]["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(
        config.read_text() + '\n[server]\ncors_origins = ["https://app.example.org"]\n'
    )
    app = ServerApp(Settings.load(installation["env"], tmp_path), log=io.StringIO())
    allowed = {"Origin": "https://app.example.org"}
    preflight = call(app, "OPTIONS", "/v1/commands/whoami", headers=allowed)
    assert preflight.status == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "https://app.example.org"
    assert "Authorization" in preflight.headers["Access-Control-Allow-Headers"]
    assert "Graph-Cli-Skills" in preflight.headers["Access-Control-Allow-Headers"]
    other = call(app, "OPTIONS", "/v1/commands/whoami", headers={"Origin": "https://evil.example"})
    assert other.status == 403 and "Access-Control-Allow-Origin" not in other.headers
    reply = call(app, "GET", "/v1/health", headers=allowed)
    assert reply.headers["Vary"] == "Origin"
    assert "Access-Control-Allow-Credentials" not in reply.headers


BUILD_A = "sha256:" + "a" * 64
BUILD_B = "sha256:" + "b" * 64


def test_another_build_of_the_client_is_refused(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """Plan origin_lifecycle E2: the server is the origin; its build is the one to run."""
    settings = Settings.load(installation["env"], tmp_path)
    app = ServerApp(settings, log=io.StringIO(), builds={"graph": BUILD_A})
    token = installation["tokens"]["ci"]

    def whoami(build: str | None) -> Any:
        headers = {} if build is None else {"Graph-Cli-Build": build}
        return call(app, "POST", "/v1/commands/whoami", token=token, body={}, headers=headers)

    same = whoami(f"graph {BUILD_A}")
    assert same.status == 200 and same.headers["Graph-Cli-Build"] == f"graph {BUILD_A}"
    refused = whoami(f"graph {BUILD_B}")
    assert (refused.status, refused.error["code"]) == (422, "remote.client_outdated")
    assert refused.error["details"] == {"program": "graph", "client": BUILD_B, "server": BUILD_A}
    assert "build bbbbbbbbbbbb, the server runs build aaaaaaaaaaaa" in refused.error["message"]
    assert refused.headers["Graph-Cli-Exit-Code"] == "1"
    # no build, a malformed one, or one of a program this server does not know: no gate
    for build in (None, "graph unknown", f"other {BUILD_B}"):
        assert whoami(build).status == 200
    # the token is checked first; health needs neither
    anonymous = call(
        app, "POST", "/v1/commands/whoami", body={}, headers={"Graph-Cli-Build": f"graph {BUILD_B}"}
    )
    assert anonymous.error["code"] == "remote.unauthorized"
    health = call(app, "GET", "/v1/health", headers={"Graph-Cli-Build": f"graph {BUILD_B}"})
    assert health.status == 200
    # upload too: every route after the token
    upload = call(
        app,
        "PUT",
        "/v1/uploads",
        token=token,
        raw=b"x",
        headers={"Graph-Cli-Build": f"graph {BUILD_B}"},
    )
    assert upload.error["code"] == "remote.client_outdated"
    # a server of unknown build compares nothing
    unknown = ServerApp(settings, log=io.StringIO(), builds={"graph": None})
    reply = call(
        unknown,
        "POST",
        "/v1/commands/whoami",
        token=token,
        body={},
        headers={"Graph-Cli-Build": f"graph {BUILD_B}"},
    )
    assert reply.status == 200 and "Graph-Cli-Build" not in reply.headers


def test_warn_mode_lets_another_build_through(installation: dict[str, Any], tmp_path: Path) -> None:
    config = Path(installation["env"]["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + '\n[server]\nclient_builds = "warn"\n')
    settings = Settings.load(installation["env"], tmp_path)
    app = ServerApp(settings, log=io.StringIO(), builds={"graph": BUILD_A})
    reply = call(
        app,
        "POST",
        "/v1/commands/whoami",
        token=installation["tokens"]["ci"],
        body={},
        headers={"Graph-Cli-Build": f"graph {BUILD_B}"},
    )
    assert reply.status == 200
    assert reply.headers["Graph-Cli-Build"] == f"graph {BUILD_A}"  # the client warns


def test_installed_graph_skills_are_judged_from_the_header(
    run_cli: CliRunner, served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    """Plan origin_lifecycle E3: the client claims its graph skills, the server judges them
    against the graph's slot; a malformed or foreign claim is no claim."""
    app, _ = served
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(GRAPH_SKILL))
    assert published.code == 0, published.envelope
    token = installation["tokens"]["ci"]
    listed = command(app, "graphs", token)
    (graph,) = [g["id"] for g in listed.json["data"]["items"] if g["name"] == "example_graph"]
    current = skill_hash(read_tree(GRAPH_SKILL)[0])
    pointer = {"graphs": ["example_graph"], "default": "example_graph"}
    codes = iter(range(1, 100))

    def create(claims: str, shown: dict[str, Any] = pointer) -> Any:
        args = {"type": "inv:Location", "--set": ["name=Shelf", f"code=S{next(codes)}"]}
        body = {"args": args, "pointer": shown}
        headers = {"Graph-Cli-Skills": claims}
        return call(app, "POST", "/v1/commands/create", token=token, body=body, headers=headers)

    fresh = create(f"graph={graph};hash={current};scope=user")
    assert fresh.status == 200 and fresh.json["warnings"] == [], fresh.json
    stale = f"graph={graph};hash=sha256:{'0' * 64};scope=project"
    refused = create(stale, {**pointer, "file": "/client/project/.graph_cli.toml"})
    assert (refused.status, refused.error["code"]) == (422, "skills.outdated")
    assert refused.error["hint"] == "graph self update --dir /client/project"
    assert refused.headers["Graph-Cli-Exit-Code"] == "1"
    assert refused.error["details"]["skills"][0]["current"] == {"hash": current, "model": None}
    unnamed = create(stale)  # no pointer file named: the hint cannot name the directory
    assert unnamed.error["hint"] == "graph self update"
    for ignored in ("garbage", f"graph={graph};hash=nope;scope=project", stale.replace(graph, "x")):
        assert create(ignored).status == 200
    read = call(
        app,
        "POST",
        "/v1/commands/find",
        token=token,
        body={"args": {"--type": ["inv:Location"]}, "pointer": pointer},
        headers={"Graph-Cli-Skills": stale},
    )
    assert read.status == 200
    assert [w["code"] for w in read.json["warnings"]] == ["skills.stale"]
