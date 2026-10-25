"""C: the WSGI app of graph-server in-process (contracts B14.2-B14.8, B14.14).

The installation is prepared with the real ``graph-admin`` (policy grant,
tokens); requests go through ``ServerApp`` without a socket.
"""

import base64
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
from support.openapi import answer_validator, app_document
from support.server import actor_header, call, grant_server, new_token

from graph_cli import __version__
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
    assert wrong.status == 405 and wrong.error["code"] == "usage.invalid_argument"
    assert wrong.headers["Allow"] == "POST"  # B14.22 G10, RFC 9110 15.5.6
    health = call(app, "POST", "/v1/health")
    assert health.status == 405 and health.headers["Allow"] == "GET"
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


def test_answers_match_the_openapi_document(
    run_cli: CliRunner, served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    """B14.22 G4: the served document is the one the goldens hold, and real answers fit
    the response of their status, closed (the routes that are no command included)."""
    app, _ = served
    token = installation["tokens"]["ci"]
    served_document = call(app, "GET", "/v1/openapi.json", token=token)
    document = served_document.json
    # this server hands out no programs and exchanges no codes: no such routes described
    assert document == app_document(__version__, dist=False, enroll=False)
    assert not {"/v1/dist/manifest", "/v1/enroll"} & set(document["paths"])

    def check(path: str, method: str, reply: Any) -> None:
        responses = document["paths"][path][method]["responses"]
        status = str(reply.status) if str(reply.status) in responses else "default"
        validator = answer_validator(document, path, method, status)
        assert not [e.message for e in validator.iter_errors(reply.json)], (path, reply.json)

    pointer = {"graphs": ["example_graph"], "default": "example_graph"}
    check("/v1/health", "get", call(app, "GET", "/v1/health"))
    check("/v1/openapi.json", "get", served_document)
    check("/v1/uploads", "put", call(app, "PUT", "/v1/uploads", token=token, raw=b"x"))
    created = command(app, "create", token, args=LOCATION, pointer=pointer)
    assert created.status == 200, created.json
    check("/v1/commands/create", "post", created)
    missing = command(app, "get", token, args={"ref": "deadbeefdeadbeef"}, pointer=pointer)
    assert missing.status == 404, missing.json
    check("/v1/commands/get", "post", missing)
    malformed = command(app, "get", token, args={})
    assert malformed.error["code"] == "usage.missing_argument"  # undeclared: under 400
    check("/v1/commands/get", "post", malformed)
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(GRAPH_SKILL))
    assert published.code == 0, published.envelope
    skill = call(app, "POST", "/v1/graphs/example_graph/skill", token=token, body={})
    assert skill.status == 200, skill.json
    check("/v1/graphs/{graph}/skill", "post", skill)


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
    twice = base64.urlsafe_b64encode(b'{"agent": {"id": "a1"}, "agent": {"id": "a2"}}').decode()
    duplicate = call(
        app,
        "GET",
        f"/v1/graphs/files_graph/files/{entity}",
        token=token,
        headers={"Graph-Cli-Actor": twice.rstrip("=")},
    )
    assert (duplicate.status, duplicate.error["code"]) == (400, "usage.invalid_input")


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
    # a cache must not hand an answer without CORS headers to an allowed origin
    for headers in ({}, {"Origin": "https://evil.example"}):
        plain = call(app, "GET", "/v1/health", headers=headers)
        assert plain.headers["Vary"] == "Origin", headers
        assert "Access-Control-Allow-Origin" not in plain.headers


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


# ---- HTTP conventions (B14.22 G7-G10) -----------------------------------------------

POINTER = {"graphs": ["example_graph"], "default": "example_graph"}
PROBLEM = {"Accept": "application/problem+json"}


def keyed(app: ServerApp, path: str, token: str, key: str, **body: Any) -> Any:
    return call(
        app,
        "POST",
        f"/v1/commands/{path}",
        token=token,
        body={"pointer": POINTER, **body},
        headers={"Idempotency-Key": key},
    )


def test_idempotency_key_header(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    """G7: the header is the key of ``--idempotency-key``; a repeat replays."""
    app, _ = served
    token = installation["tokens"]["ci"]
    first = keyed(app, "create", token, '"k-1"', args=LOCATION)
    assert first.status == 200, first.json
    again = keyed(app, "create", token, '"k-1"', args=LOCATION)
    assert again.status == 200 and again.json["data"]["idempotent_replay"] is True
    assert again.json["data"]["id"] == first.json["data"]["id"]
    both = keyed(app, "create", token, ' "k-1"\t', args={**LOCATION, "idempotency_key": "k-1"})
    assert both.status == 200 and both.json["data"]["idempotent_replay"] is True
    other = {"type": "inv:Location", "set": {"name": "Other", "code": "S2"}}
    reused = keyed(app, "create", token, '"k-1"', args=other)
    assert (reused.status, reused.error["code"]) == (409, "conflict.idempotency_key_reuse")
    refused = {
        "unquoted": keyed(app, "create", token, "k-2", args=LOCATION),
        "malformed": keyed(app, "create", token, '"k 2"', args=LOCATION),
        "empty": keyed(app, "create", token, '""', args=LOCATION),
        "with parameters": keyed(app, "create", token, '"k-2";a=1', args=LOCATION),
        "two keys": keyed(app, "create", token, '"k-2"', args={**LOCATION, "idempotency_key": "x"}),
        "a read": keyed(app, "find", token, '"k-2"', args={"type": ["inv:Location"]}),
    }
    for why, reply in refused.items():
        assert reply.status == 400, why
        assert reply.error["code"] == "usage.invalid_argument", why
        assert reply.error["details"] == {"argument": "Idempotency-Key"}, why
    found = command(app, "find", token, args={"type": ["inv:Location"]}, pointer=POINTER)
    assert found.status == 200, found.json
    assert len(found.json["data"]["items"]) == 1  # created once, the reuse refused


def test_problem_details_on_request(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any], tmp_path: Path
) -> None:
    """G8, G9: every error answer is problem details when ``Accept`` asks for them, the
    errors before a command included; otherwise the envelope."""
    app, _ = served
    token = installation["tokens"]["ci"]
    document = app_document(__version__)
    missing = {"args": {"ref": "deadbeefdeadbeef"}, "pointer": POINTER}
    plain = command(app, "get", token, **missing)
    assert plain.headers["Content-Type"] == "application/json" and plain.error
    assert plain.headers["Vary"] == "Accept"
    reply = call(app, "POST", "/v1/commands/get", token=token, body=missing, headers=PROBLEM)
    assert reply.status == 404
    assert reply.headers["Content-Type"] == "application/problem+json"
    assert reply.headers["Graph-Cli-Exit-Code"] == plain.headers["Graph-Cli-Exit-Code"]
    found = reply.json
    error = plain.error
    assert found == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": error["message"],
        "code": "entity.not_found",
        "category": "not_found",
        "retryable": False,
        "hint": error["hint"],
        "details": error["details"],
        "exit_code": int(plain.headers["Graph-Cli-Exit-Code"]),
        "warnings": [],
    }
    validator = answer_validator(
        document, "/v1/commands/get", "post", "404", "application/problem+json"
    )
    assert not [e.message for e in validator.iter_errors(found)]
    before = {
        401: call(app, "POST", "/v1/commands/whoami", body={}, headers=PROBLEM),
        404: call(app, "GET", "/v1/nothing", token=token, headers=PROBLEM),
        405: call(app, "GET", "/v1/commands/whoami", token=token, headers=PROBLEM),
        400: call(app, "POST", "/v1/commands/whoami", token=token, raw=b"{", headers=PROBLEM),
    }
    for status, answer in before.items():
        assert answer.status == status and answer.json["status"] == status
        assert answer.headers["Content-Type"] == "application/problem+json"
    assert before[401].headers["WWW-Authenticate"] == "Bearer"
    assert before[405].headers["Allow"] == "POST"
    text = call(
        app,
        "POST",
        "/v1/commands/get",
        token=token,
        body=missing,
        query="render=text",
        headers=PROBLEM,
    )
    assert text.json["code"] == "entity.not_found"  # the problem, not the rendered text
    for accept in ("application/problem+json;q=0", "*/*", "application/json"):
        kept = call(app, "POST", "/v1/commands/whoami", body={}, headers={"Accept": accept})
        assert kept.headers["Content-Type"] == "application/json", accept
    ok = call(app, "POST", "/v1/commands/whoami", token=token, body={}, headers=PROBLEM)
    assert ok.status == 200 and ok.json["ok"] is True and "Vary" not in ok.headers
    # with [server] public_url the type names the registry entry
    config = Path(installation["env"]["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(
        config.read_text() + '\n[server]\npublic_url = "https://graph.example.org/"\n'
    )
    public = ServerApp(Settings.load(installation["env"], tmp_path), log=io.StringIO())
    typed = call(public, "POST", "/v1/commands/get", token=token, body=missing, headers=PROBLEM)
    assert typed.json["type"] == "https://graph.example.org/v1/errors/entity.not_found"
    assert typed.json["title"] == "The entity does not exist."
    served_document = call(public, "GET", "/v1/openapi.json", token=token).json
    assert served_document["servers"] == [{"url": "https://graph.example.org"}]


def test_error_registry(served: tuple[ServerApp, io.StringIO]) -> None:
    """G9: ``GET /v1/errors`` needs no token, like health."""
    app, _ = served
    document = app_document(__version__)
    listed = call(app, "GET", "/v1/errors")
    assert listed.status == 200
    codes = {entry["code"]: entry for entry in listed.json["errors"]}
    assert codes["backend.lock_timeout"] == {
        "code": "backend.lock_timeout",
        "category": "unavailable",
        "retryable": True,
        "status": 503,
        "exit_code": codes["backend.lock_timeout"]["exit_code"],
        "summary": "The graph lock was not acquired.",
    }
    assert codes["remote.unauthorized"]["status"] == 401
    assert "entity.superseded" not in codes  # a warning is no problem type
    one = call(app, "GET", "/v1/errors/entity.not_found")
    assert one.json == {**codes["entity.not_found"]}
    for path, reply in (("/v1/errors", listed), ("/v1/errors/{code}", one)):
        validator = answer_validator(document, path, "get", "200")
        assert not [e.message for e in validator.iter_errors(reply.json)]
    assert call(app, "GET", "/v1/errors/entity.superseded").status == 404
    assert call(app, "GET", "/v1/errors/nothing").error["code"] == "usage.unknown_command"
    assert call(app, "POST", "/v1/errors").status == 405


def test_a_retryable_503_says_when_to_retry(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any], monkeypatch: Any
) -> None:
    """G10: ``Retry-After`` on a retryable 503 only."""
    from graph_cli.kernel.errors import GraphCliError

    app, _ = served
    raised: list[GraphCliError] = []

    def dispatch(*_: Any) -> Any:
        raise raised[-1]

    monkeypatch.setattr(app, "_dispatch", dispatch)
    raised.append(GraphCliError("backend.lock_timeout", "the graph lock was not acquired"))
    busy = call(app, "GET", "/v1/health", headers=PROBLEM)
    assert busy.status == 503 and busy.headers["Retry-After"] == "2"
    assert busy.json["retryable"] is True
    raised.append(GraphCliError("backend.corrupt", "damaged"))
    corrupt = call(app, "GET", "/v1/health")
    assert corrupt.status == 503 and "Retry-After" not in corrupt.headers


def test_file_download_validators(
    served: tuple[ServerApp, io.StringIO], installation: dict[str, Any]
) -> None:
    """G10: the download names its content hash as ETag and answers If-None-Match with 304,
    after the same authorization."""
    app, _ = served
    token = installation["tokens"]["ci"]
    content = synthetic_pdf()
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=content).json
    reference = {"upload": uploaded["upload"], "name": "report.pdf"}
    added = command(app, "file/add", token, args={"path": reference, "graph": "files_graph"})
    entity = added.json["data"]["entity"]["id"]
    route = f"/v1/graphs/files_graph/files/{entity}"
    first = call(app, "GET", route, token=token)
    tag = f'"{hash_bytes(content)}"'
    assert first.status == 200 and first.headers["ETag"] == tag
    assert first.headers["Cache-Control"] == "private, no-cache"
    for header in (tag, f"W/{tag}", f'"sha256:{"0" * 64}", {tag}', "*"):
        same = call(app, "GET", route, token=token, headers={"If-None-Match": header})
        assert same.status == 304 and same.body == b"", header
        assert same.headers["ETag"] == tag
    changed = call(app, "GET", route, token=token, headers={"If-None-Match": '"other"'})
    assert changed.status == 200 and changed.body == content
    stranger = installation["tokens"]["stranger"]
    hidden = call(app, "GET", route, token=stranger, headers={"If-None-Match": tag})
    assert hidden.status in (403, 404) and "ETag" not in hidden.headers
    assert hidden.headers["Cache-Control"] == "no-store"


def test_cors_names_the_http_headers(installation: dict[str, Any], tmp_path: Path) -> None:
    """G10: a browser may send the key and conditional requests and read the headers."""
    config = Path(installation["env"]["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(
        config.read_text() + '\n[server]\ncors_origins = ["https://app.example.org"]\n'
    )
    app = ServerApp(Settings.load(installation["env"], tmp_path), log=io.StringIO())
    allowed = {"Origin": "https://app.example.org"}
    preflight = call(app, "OPTIONS", "/v1/commands/create", headers=allowed)
    for name in ("Idempotency-Key", "If-None-Match"):
        assert name in preflight.headers["Access-Control-Allow-Headers"]
    refused = call(app, "POST", "/v1/commands/whoami", body={}, headers=allowed)
    exposed = refused.headers["Access-Control-Expose-Headers"]
    for name in ("ETag", "Retry-After", "Allow", "Graph-Cli-Content-Hash", "Graph-Cli-Media-Type"):
        assert name in exposed
    assert refused.headers["Vary"] == "Accept, Origin"
