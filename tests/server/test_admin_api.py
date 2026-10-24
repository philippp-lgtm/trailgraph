"""C and S: the admin API of graph-server and graph-admin in remote mode (contracts B14.15).

In-process: the admin routes are off unless plugged in, token scopes keep the
app and admin surfaces apart, a request acts as its token through the
entrypoint ``maint`` (the policy's maintainers decide, the audit names the
token), and no request can name a path on the server. As processes: every
command the admin API runs answers the same from a remote-mode host as
locally, on an installation prepared alike.
"""

import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.equivalence import differences, home_env, normalized
from support.fixtures import FIXTURES, ONTOLOGIES
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY, call, grant_server, new_token

from graph_cli_admin.maintenance_cli.commands import all_specs
from graph_cli_server.admin_api import AdminApi
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

V100 = ONTOLOGIES / "inventory" / "1.0.0"
V101 = ONTOLOGIES / "inventory" / "1.0.1"
GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
SECRET = re.compile(r"gct_[A-Za-z0-9_-]{20,}")
HASH_PREFIX = re.compile(r"sha256:[0-9a-f]{16}(?![0-9a-f])")


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", version="1.0.0")
    grant_server(run_cli, tmp_path)
    tokens = {
        "ci": new_token(run_cli, "ci"),
        "root": new_token(run_cli, "root", "--scope", "admin"),
    }
    return {"root": root, "env": isolated_env, "tokens": tokens}


def served(installation: dict[str, Any], cwd: Path, *, with_admin: bool = True) -> ServerApp:
    settings = Settings.load(installation["env"], cwd)
    return ServerApp(settings, log=io.StringIO(), admin=AdminApi if with_admin else None)


def command(app: ServerApp, path: str, token: str, **body: Any) -> Any:
    return call(app, "POST", f"/v1/admin/commands/{path}", token=token, body=body)


def upload_tree(app: ServerApp, token: str, directory: Path) -> dict[str, Any]:
    files = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        reply = call(app, "PUT", "/v1/uploads", token=token, raw=path.read_bytes())
        assert reply.status == 200, reply.json
        files[path.relative_to(directory).as_posix()] = reply.json["upload"]
    return {"files": files, "name": str(directory)}


def audit_events(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for segment in sorted(p for p in root.rglob("*.jsonl") if "audit" in p.parts):
        found += [json.loads(line) for line in segment.read_text().splitlines()]
    return found


def test_the_admin_api_is_off_unless_plugged_in(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path, with_admin=False)
    reply = command(app, "token/list", installation["tokens"]["root"])
    assert reply.status == 404
    assert "admin_api" in reply.error["message"]


def test_token_scopes_keep_the_surfaces_apart(installation: dict[str, Any], tmp_path: Path) -> None:
    app = served(installation, tmp_path)
    tokens = installation["tokens"]
    refused = command(app, "token/list", tokens["ci"])
    assert refused.status == 403
    assert refused.error["code"] == "remote.unauthorized"
    assert refused.error["details"] == {"scope": "app", "required": "admin"}
    assert refused.headers["Graph-Cli-Exit-Code"] == "3"
    for path in ("/v1/commands/whoami", "/v1/inventory"):
        method = "POST" if "commands" in path else "GET"
        reply = call(app, method, path, token=tokens["root"], body={})
        assert reply.status == 403 and reply.error["details"]["scope"] == "admin"
    upload = call(app, "PUT", "/v1/uploads", token=tokens["root"], raw=b"x")
    assert upload.status == 200
    assert command(app, "token/list", tokens["root"]).status == 200
    assert command(app, "init", tokens["root"]).status == 404
    assert command(app, "view", tokens["root"]).status == 404


def test_a_request_acts_as_its_token_through_maint(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    actor = {"agent": {"id": "agent-1", "type": "coding_agent"}}
    created = command(app, "token/create", root, args={"name": "web"}, actor=actor)
    assert created.status == 200, created.json
    assert created.json["data"]["token"].startswith("gct_")
    listed = command(app, "token/list", root)
    scopes = {t["name"]: t["scope"] for t in listed.json["data"]["tokens"]}
    assert scopes == {"ci": "app", "root": "admin", "web": "app"}
    event = next(
        e
        for e in audit_events(installation["root"])
        if e.get("actions") == ["maint.token_create"] and e["change"]["token"]["name"] == "web"
    )
    assert event["actor"]["principal"]["id"] == "token:root"
    assert event["actor"]["client"] == {
        "name": "graph-server",
        "version": event["actor"]["client"]["version"],
        "entrypoint": "maint",
    }
    assert event["actor"]["agent"]["id"] == "agent-1"
    verified = command(app, "audit/verify", root)
    assert verified.status == 200, verified.json


def test_the_maintainers_decide(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    policy = SERVER_POLICY + f'maintainers:\n  - {{id: "os:{os.getuid()}"}}\n'
    grant_server(run_cli, tmp_path, policy)
    other = admin(run_cli, "token", "create", "other", "--scope", "admin")
    assert other.warning_codes == ["admin.token_not_maintainer"]
    app = served(installation, tmp_path)
    denied = command(app, "token/revoke", installation["tokens"]["root"], args={"name": "ci"})
    assert denied.status == 403, denied.json
    assert denied.error["code"] == "access.denied"
    refusals = [
        e
        for e in audit_events(installation["root"])
        if e.get("result", {}).get("status") == "denied"
    ]
    assert refusals[-1]["actor"]["principal"]["id"] == "token:root"


def test_an_admin_token_has_no_project(run_cli: CliRunner, installation: dict[str, Any]) -> None:
    outcome = admin(run_cli, "token", "create", "x", "--scope", "admin", "--project", "p1")
    assert outcome.code == 2
    assert outcome.error["details"]["argument"] == "--project"


def test_no_request_names_a_path_on_the_server(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    inventory = call(app, "GET", "/v1/admin/inventory", token=root).json
    options = {o["name"] for c in inventory["commands"] for o in c["options"]}
    assert "--data-root" not in options
    paths = call(app, "GET", "/v1/admin/openapi.json", token=root).json["paths"]
    assert "/v1/admin/commands/ontology/release" in paths
    assert not {"/v1/admin/commands/init", "/v1/admin/commands/view"} & set(paths)
    assert "/v1/admin/commands/ontology/validate" not in paths
    upload = call(app, "PUT", "/v1/uploads", token=root, raw=b"schema: x\n").json["upload"]
    refused = [
        {"name": "g", "--ontology": str(V100)},
        {"name": "g", "--ontology": {"files": {"../x.yaml": upload}, "name": "o"}},
        {"name": "g", "--ontology": {"files": {"/etc/x.yaml": upload}, "name": "o"}},
        {"name": "g", "--ontology": {"files": {"a/./b.yaml": upload}, "name": "o"}},
        {"name": "g", "--ontology": {"files": {"x.yaml": "sha256:" + "0" * 64}, "name": "o"}},
        {"name": "g", "--ontology": {"files": {"a": upload, "a/b": upload}, "name": "o"}},
        {"name": "g", "--ontology": {"files": {}, "name": "o"}, "--data-root": "/tmp"},
    ]
    for args in refused:
        reply = command(app, "graph/create", root, args=args)
        assert reply.status == 400, (args, reply.json)
    policy = command(app, "policy/check", root, args={"file": "/etc/passwd"})
    assert policy.status == 400
    module = command(
        app,
        "graph/create",
        root,
        args={
            "name": "g",
            "--ontology": upload_tree(app, root, V100),
            "--module": "../../../../etc/os-release",
        },
    )
    assert module.status == 400 and module.error["details"]["argument"] == "module"
    left = list((Path(installation["env"]["XDG_STATE_HOME"]) / "graph_cli").rglob("request-*"))
    assert left == []


def test_an_ontology_is_created_and_released_through_uploads(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    created = command(
        app,
        "graph/create",
        root,
        args={"name": "second", "--ontology": upload_tree(app, root, V100)},
    )
    assert created.status == 200, created.json
    released = command(
        app,
        "ontology/release",
        root,
        args={"graph": "second", "source": upload_tree(app, root, V101)},
    )
    assert released.status == 200, released.json
    shown = command(app, "graph/show", root, args={"ref": "second"})
    assert shown.json["data"]["ontology"]["version"] == "1.0.1", shown.json["data"]
    broken = command(
        app,
        "ontology/release",
        root,
        args={"graph": "second", "source": {"files": {}, "name": "empty"}},
    )
    assert broken.status == 400, broken.json
    assert "request-" not in json.dumps(broken.json)
    assert command(app, "audit/verify", root).status == 200


def test_a_hand_edited_scope_is_found(run_cli: CliRunner, installation: dict[str, Any]) -> None:
    file = installation["root"] / "tokens" / "tokens.json"
    document = json.loads(file.read_text())
    assert [t.get("scope") for t in document["tokens"]] == [None, "admin"]
    for entry in document["tokens"]:
        entry.pop("scope", None)
    file.write_text(json.dumps(document))
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 1
    codes = {f["code"] for f in verified.error["details"]["findings"]}
    assert "integrity.audit.token_mismatch" in codes


# ---- graph-admin in remote mode ---------------------------------------------------------

POLICY = SERVER_POLICY + "scopes:\n  example_project: {example_graph: read}\n"


def prepare(env: dict[str, str], cwd: Path) -> str:
    """An installation with a graph, the server policy and an admin token."""
    policy = cwd / "server_policy.yaml"
    policy.write_text(SERVER_POLICY)
    token = ""
    for args in (
        ("init",),
        ("graph", "create", "example_graph", "--ontology", str(V100)),
        ("policy", "apply", str(policy), "--reason", "equivalence"),
        ("token", "create", "root", "--scope", "admin"),
    ):
        result = subprocess.run(
            [str(BIN_DIR / "graph-admin"), *args, "--json"],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        token = json.loads(result.stdout)["data"].get("token", token)
    return token


def scenario(env: dict[str, str], cwd: Path) -> list[Any]:
    """Every command the admin API runs, in order, with their outcomes."""
    seen: list[Any] = []
    preview = cwd / "preview"
    shutil.rmtree(preview, ignore_errors=True)
    preview.mkdir()
    (cwd / "policy.yaml").write_text(POLICY)

    def run(*args: str, text: bool = False) -> None:
        argv = [str(BIN_DIR / "graph-admin"), *args, *([] if text else ["--json"])]
        result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=60)
        if text:
            seen.append([list(args), result.returncode, result.stdout, result.stderr])
        else:
            seen.append([list(args), result.returncode, json.loads(result.stdout)])

    for args in (
        ("graph", "list"),
        ("graph", "show", "example_graph"),
        ("ontology", "validate", str(V101)),
        ("ontology", "diff", str(V101), "--graph", "example_graph"),
        ("ontology", "diff", str(V101), "--against", str(V100)),
        ("graph", "check", "example_graph"),
        ("graph", "check", "example_graph", "--ontology", str(V101)),
        ("ontology", "release", "example_graph", str(V101)),
        ("ontology", "release", "example_graph", str(V101)),
        ("ontology", "release", "example_graph", "missing_dir"),
        ("graph", "create", "second", "--ontology", str(V100), "--title", "Second"),
        ("graph", "create", "second", "--ontology", str(V100)),
        ("graph", "reindex", "second", "--dry-run"),
        ("graph", "reindex", "second", "--reason", "equivalence"),
        ("graph", "skill", "publish", "example_graph", str(GRAPH_SKILL), "--dry-run"),
        ("graph", "skill", "publish", "example_graph", str(GRAPH_SKILL)),
        ("graph", "skill", "render", "example_graph", str(GRAPH_SKILL), "--out", "preview"),
        ("graph", "skill", "render", "example_graph", str(GRAPH_SKILL), "--out", "preview"),
        (
            "graph",
            "skill",
            "render",
            "example_graph",
            str(GRAPH_SKILL),
            "--out",
            "preview",
            "--force",
        ),
        ("policy", "check", "policy.yaml"),
        ("policy", "check", "missing.yaml"),
        ("policy", "diff", "policy.yaml"),
        ("policy", "apply", "policy.yaml", "--reason", "scope a project"),
        ("policy", "show"),
        ("policy", "explain", "--graph", "example_graph", "--project", "example_project"),
        ("policy", "explain", "--graph", "example_graph", "--file", "policy.yaml"),
        ("token", "create", "agent", "--project", "example_project"),
        ("token", "create", "agent"),
        ("token", "list"),
        ("token", "revoke", "agent", "--reason", "done"),
        ("files", "verify", "--graph", "example_graph"),
        ("files", "gc", "--graph", "example_graph", "--dry-run"),
        ("audit", "anchor"),
        ("audit", "verify"),
    ):
        run(*args)
    seen.append(sorted(str(p.relative_to(preview)) for p in preview.rglob("*")))
    run("graph", "list", text=True)
    run("token", "list", text=True)
    return seen


def comparable(outcomes: list[Any], *homes: Path) -> Any:
    text = json.dumps(outcomes)
    for home in homes:
        text = text.replace(str(home), "<home>")
    text = HASH_PREFIX.sub("<hash prefix>", SECRET.sub("<token>", text))
    return normalized(json.loads(text))


def covered(outcomes: list[Any]) -> set[tuple[str, ...]]:
    paths = {s.path for s in all_specs()}
    found = set()
    for outcome in outcomes:
        if outcome and isinstance(outcome[0], list):
            found |= {p for p in paths if tuple(outcome[0][: len(p)]) == p}
    return found


def test_remote_graph_admin_answers_as_locally(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    local_home = tmp_path / "local"
    local = home_env(isolated_env, local_home)
    prepare(local, tmp_path)
    expected = scenario(local, workdir)

    owner_home = tmp_path / "server"
    owner = home_env(isolated_env, owner_home)
    token = prepare(owner, tmp_path)
    config = Path(owner["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "[server]\nadmin_api = true\n")
    client_home = tmp_path / "client"
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            isolated_env,
            client_home,
            server.port,
            None,
            extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n',
        )
        client["GRAPH_CLI_ADMIN_TOKEN"] = token
        actual = scenario(client, workdir)
        refused = [
            subprocess.run(
                [str(BIN_DIR / "graph-admin"), *args, "--json"],
                env=client,
                cwd=workdir,
                capture_output=True,
                text=True,
            )
            for args in (
                ("init",),
                ("graph", "list", "--data-root", str(tmp_path / "elsewhere")),
            )
        ]
        view = subprocess.run(
            [str(BIN_DIR / "graph-admin"), "view", "--graph", "example_graph", "--no-open"],
            env=client,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        app_token = json.loads(
            subprocess.run(
                [str(BIN_DIR / "graph-admin"), "token", "create", "app", "--json"],
                env=client,
                cwd=workdir,
                capture_output=True,
                text=True,
            ).stdout
        )["data"]["token"]
        scoped = subprocess.run(  # text mode: the refusal comes before any command ran
            [str(BIN_DIR / "graph-admin"), "token", "list"],
            env={**client, "GRAPH_CLI_ADMIN_TOKEN": app_token},
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        without = {k: v for k, v in client.items() if k != "GRAPH_CLI_ADMIN_TOKEN"}
        unauthorized = subprocess.run(
            [str(BIN_DIR / "graph-admin"), "graph", "list", "--json"],
            env={**without, "GRAPH_CLI_TOKEN": token},
            cwd=workdir,
            capture_output=True,
            text=True,
        )
    assert "POST /v1/admin/commands/ontology/release 200" in "".join(server.log)
    for result in refused:
        assert json.loads(result.stdout)["error"]["code"] == "admin.remote_mode", result.stdout
    assert view.returncode == 1 and "admin.remote_mode" in view.stderr, view.stderr
    error = json.loads(unauthorized.stdout)["error"]
    assert error["code"] == "remote.unauthorized" and "GRAPH_CLI_ADMIN_TOKEN" in error["message"]
    assert scoped.returncode == 3, scoped.stdout + scoped.stderr
    assert "remote.unauthorized" in scoped.stderr and "scope app" in scoped.stderr
    assert covered(expected) == {s.path for s in all_specs() if s.remote != "local"} - {
        ("describe", "commands"),  # the same code on both sides: the client's inventory
    }
    mine = comparable(actual, client_home, owner_home, local_home)
    theirs = comparable(expected, local_home, owner_home, client_home)
    assert len(mine) == len(theirs)
    for a, b in zip(mine, theirs, strict=True):
        assert a == b, differences(a, b)
