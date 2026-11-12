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
from support.openapi import admin_document
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY, call, grant_server, new_token

from graph_cli import __version__
from graph_cli_admin.maintenance_cli.admin_skills import admin_compat
from graph_cli_admin.maintenance_cli.commands import all_specs
from graph_cli_server.admin_api import AdminApi
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

V100 = ONTOLOGIES / "inventory" / "1.0.0"
V101 = ONTOLOGIES / "inventory" / "1.0.1"
V110 = ONTOLOGIES / "inventory" / "1.1.0"
GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
BROKEN_FUNCTIONS = FIXTURES / "functions" / "inventory" / "broken"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
BROKEN_ACTIONS = FIXTURES / "actions" / "inventory" / "broken"
SECRET = re.compile(r"gct_[A-Za-z0-9_-]{20,}")
HASH_PREFIX = re.compile(r"sha256:[0-9a-f]{16}(?![0-9a-f])")
FOLDER = re.compile(r"\b(example_graph|second)_[0-9a-f]{6}\b")  # workspace sync: id ends
CODE = re.compile(r"gce_[a-z2-7]{26}")
ENROLLMENT_ID = re.compile(r"(?<![0-9a-f-])[0-9a-f]{12}(?![0-9a-f-])")


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


def test_graph_admin_is_compared_with_the_servers_graph_admin(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """Plan origin_lifecycle E2: each program against the server's build of it."""
    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    settings = Settings.load(installation["env"], tmp_path)
    app = ServerApp(
        settings, log=io.StringIO(), admin=AdminApi, builds={"graph": a, "graph-admin": b}
    )
    root = installation["tokens"]["root"]

    def token_list(build: str) -> Any:
        return call(
            app,
            "POST",
            "/v1/admin/commands/token/list",
            token=root,
            body={},
            headers={"Graph-Cli-Build": build},
        )

    same = token_list(f"graph-admin {b}")
    assert same.status == 200 and same.headers["Graph-Cli-Build"] == f"graph-admin {b}"
    refused = token_list(f"graph-admin {a}")
    assert refused.error["code"] == "remote.client_outdated"
    assert refused.error["details"]["program"] == "graph-admin"
    # graph-admin's compatibility id is the admin API's own (C3), named in every answer
    compat = app.compats["graph-admin"]
    assert compat == admin_compat() and compat != app.compats["graph"]
    assert same.headers["Graph-Cli-Compat"] == f"graph-admin {compat}"
    compatible = call(
        app,
        "POST",
        "/v1/admin/commands/token/list",
        token=root,
        body={},
        headers={
            "Graph-Cli-Build": f"graph-admin {a}",
            "Graph-Cli-Compat": f"graph-admin {compat}",
        },
    )
    assert compatible.status == 200


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


def test_usage_report_is_served_from_the_servers_access_log_and_gated(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """The report runs on the server and reads the state root there; it is no project command."""
    from graph_cli_server.admin_api import PROJECT_COMMANDS

    assert "usage report" not in PROJECT_COMMANDS
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    empty = command(app, "usage/report", root)
    assert empty.status == 200, empty.json
    assert empty.json["data"]["access_log"] is False
    graph = command(app, "graph/show", root, args={"ref": "example_graph"}).json["data"]
    log = Path(installation["env"]["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema": "graph_cli.access/1",
        "ts": "2026-11-02T10:00:00.000Z",
        "command": "query",
        "actor": {"session": {"assurance": "asserted", "id": "s1"}},
        "graph": {"id": graph["id"], "name": "example_graph", "seq": 1},
        "result": {"status": "ok"},
        "query": {"shape_hash": "sha256:a", "shape": "MATCH (v1:Item) RETURN v1", "covered_by": []},
    }
    log.write_text(json.dumps(entry) + "\nnot json\n")
    served_report = command(app, "usage/report", root, args={"graph": "example_graph"})
    assert served_report.status == 200, served_report.json
    data = served_report.json["data"]
    assert data["access_log"] is True and data["skipped_lines"] == 1
    assert data["graphs"][0]["queries"] == 1
    assert str(log.parent) not in json.dumps(served_report.json)  # no server path in the answer
    assert command(app, "usage/report", installation["tokens"]["ci"]).status == 403  # app scope
    unknown = command(app, "usage/report", root, args={"graph": "nowhere"})
    assert (unknown.status, unknown.error["code"]) == (404, "graph.not_found")


def test_admin_commands_take_no_idempotency_key_and_answer_problems(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """B14.22 G7, G8 on the admin API: no admin command takes a key, so the header is
    refused; an error is problem details on request."""
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    keyed = call(
        app,
        "POST",
        "/v1/admin/commands/token/list",
        token=root,
        body={},
        headers={"Idempotency-Key": '"k-1"'},
    )
    assert (keyed.status, keyed.error["code"]) == (400, "usage.invalid_argument")
    assert keyed.error["details"] == {"argument": "Idempotency-Key"}
    problem = call(
        app,
        "GET",
        "/v1/admin/commands/token/list",
        token=root,
        headers={"Accept": "application/problem+json"},
    )
    assert problem.status == 405 and problem.headers["Allow"] == "POST"
    assert problem.headers["Content-Type"] == "application/problem+json"
    assert problem.json["code"] == "usage.invalid_argument"


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
    for path in ("token/list", "policy/show", "graph/list", "audit/verify", "usage/report"):
        read = command(app, path, installation["tokens"]["root"])
        assert (read.status, read.error["code"]) == (403, "access.denied"), (path, read.json)
    # the gate comes first: a header or an unknown argument tells a denied caller nothing
    keyed = call(
        app,
        "POST",
        "/v1/admin/commands/token/list",
        token=installation["tokens"]["root"],
        body={"args": {"nothing": True}},
        headers={"Idempotency-Key": '"k-1"'},
    )
    assert (keyed.status, keyed.error["code"]) == (403, "access.denied"), keyed.json
    refusals = [
        e
        for e in audit_events(installation["root"])
        if e.get("result", {}).get("status") == "denied"
    ]
    assert refusals[-1]["actor"]["principal"]["id"] == "token:root"


PROJECT_POLICY = (
    SERVER_POLICY
    + f'maintainers:\n  - {{id: "os:{os.getuid()}"}}\n  - {{id: "token:root"}}\n'
    + "scopes:\n  p1: {example_graph: maintain}\n  p2: {other_graph: write}\n"
)


@pytest.fixture
def project_admins(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> dict[str, Any]:
    """Plan origin_lifecycle E8-E10: ``p1`` maintains example_graph, ``p2`` nothing."""
    create_graph(run_cli, "other_graph", version="1.0.0")
    grant_server(run_cli, tmp_path, PROJECT_POLICY)
    created = admin(run_cli, "token", "create", "pa", "--scope", "admin", "--project", "p1")
    assert created.code == 0 and created.warning_codes == [], created.envelope
    idle = admin(run_cli, "token", "create", "pb", "--scope", "admin", "--project", "p2")
    assert idle.warning_codes == ["admin.token_not_maintainer"], idle.envelope
    assert "scopes.p2" in idle.envelope["warnings"][0]["message"]
    return {**installation, "pa": created.data["token"], "pb": idle.data["token"]}


def project_args(app: ServerApp, token: str, spec_path: tuple[str, ...], graph: str) -> Any:
    """Arguments that get every project command as far as resolving ``graph``."""
    ontology = upload_tree(app, token, V101)
    skill = upload_tree(app, token, GRAPH_SKILL)
    functions = upload_tree(app, token, FUNCTIONS)
    actions = upload_tree(app, token, ACTIONS)
    empty = {"files": {}, "name": "out"}
    return {
        ("files", "verify"): {"--graph": graph},
        ("graph", "actions", "publish"): {"ref": graph, "dir": actions, "--reason": "test"},
        ("graph", "actions", "show"): {"ref": graph},
        ("graph", "check"): {"ref": graph},
        ("graph", "functions", "publish"): {"ref": graph, "dir": functions, "--reason": "test"},
        ("graph", "functions", "show"): {"ref": graph},
        ("graph", "list"): {},
        ("graph", "pull"): {"ref": graph, "dir": empty},
        ("graph", "push"): {"ref": graph, "dir": ontology, "--dry-run": True},
        ("graph", "reindex"): {"ref": graph, "--reason": "test"},
        ("graph", "show"): {"ref": graph},
        ("graph", "skill", "publish"): {"ref": graph, "dir": skill, "--reason": "test"},
        ("graph", "skill", "render"): {"ref": graph, "dir": skill, "--out": empty},
        ("ontology", "diff"): {"source": ontology, "--graph": graph},
        ("ontology", "release"): {"graph": graph, "source": ontology},
    }[spec_path]


def test_a_project_admin_reaches_its_graphs_and_nothing_else(
    project_admins: dict[str, Any], tmp_path: Path
) -> None:
    """E10: every served admin command, run by a project admin, is refused or shows only
    the project's graphs; another graph is not found, as one that does not exist."""
    from graph_cli_server.admin_api import PROJECT_COMMANDS

    app = served(project_admins, tmp_path)
    pa = project_admins["pa"]
    commands = [s for s in all_specs() if s.remote in ("server", "hooked")]
    assert {" ".join(s.path) for s in commands} >= PROJECT_COMMANDS
    for spec in commands:
        path = "/".join(spec.path)
        if " ".join(spec.path) not in PROJECT_COMMANDS:
            reply = command(app, path, pa, args={})
            assert (reply.status, reply.error["code"]) == (403, "access.ceiling"), path
            continue
        args = project_args(app, pa, spec.path, "other_graph")
        reply = command(app, path, pa, args=args)
        if spec.path == ("graph", "list"):
            names = [g["name"] for g in reply.json["data"]["items"]]
            assert names == ["example_graph"], reply.json
            continue
        missing = command(app, path, pa, args=project_args(app, pa, spec.path, "no_such_graph"))
        assert (reply.status, reply.error["code"]) == (404, "graph.not_found"), (path, reply.json)
        assert reply.error["message"].replace("other_graph", "X") == missing.error[
            "message"
        ].replace("no_such_graph", "X"), path
    shown = command(app, "graph/show", pa, args={"ref": "example_graph"})
    assert shown.status == 200, shown.json
    reindexed = command(app, "graph/reindex", pa, args={"ref": "example_graph", "--reason": "x"})
    assert reindexed.status == 200, reindexed.json
    released = command(
        app,
        "ontology/release",
        pa,
        args={"graph": "example_graph", "source": upload_tree(app, pa, V101)},
    )
    assert released.status == 200, released.json
    refusals = [
        e
        for e in audit_events(project_admins["root"])
        if e.get("result", {}).get("status") == "denied"
        and e["actor"]["principal"]["id"] == "token:pa"
    ]
    assert {e["actor"]["project"]["assurance"] for e in refusals} == {"verified"}
    assert {d["rule"] for e in refusals for d in e["authz"]["decisions"]} >= {
        "ceiling.project_maint",
        "default.deny",
    }
    assert command(app, "audit/verify", project_admins["tokens"]["root"]).status == 200


def test_a_project_admin_without_graphs_reaches_nothing(
    project_admins: dict[str, Any], tmp_path: Path
) -> None:
    app = served(project_admins, tmp_path)
    for path in ("graph/list", "graph/show", "token/list"):
        reply = command(app, path, project_admins["pb"], args={})
        assert (reply.status, reply.error["code"]) == (403, "access.ceiling"), path


def test_a_project_admin_reads_data_only_where_reads_are_not_denied(
    run_cli: CliRunner, project_admins: dict[str, Any], tmp_path: Path
) -> None:
    """Security review of step 5: check findings quote values, so they need read.get."""
    app = served(project_admins, tmp_path)
    pa = project_admins["pa"]
    assert command(app, "graph/check", pa, args={"ref": "example_graph"}).status == 200
    denied = PROJECT_POLICY.replace(
        "deny:\n",
        "deny:\n  - {id: no_p1_reads, project: p1, action: [read.get], graph: [example_graph]}\n",
    )
    grant_server(run_cli, tmp_path, denied)
    refused = command(app, "graph/check", pa, args={"ref": "example_graph"})
    assert (refused.status, refused.error["code"]) == (403, "access.denied"), refused.json
    release = command(
        app,
        "ontology/release",
        pa,
        args={"graph": "example_graph", "source": upload_tree(app, pa, V101)},
    )
    assert release.error["code"] == "access.denied", release.json
    shown = command(app, "graph/show", pa, args={"ref": "example_graph"})
    assert shown.status == 200, shown.json


def test_another_graphs_broken_manifest_neither_shows_nor_stops_a_project_admin(
    project_admins: dict[str, Any], tmp_path: Path
) -> None:
    app = served(project_admins, tmp_path)
    root = project_admins["tokens"]["root"]
    other = command(app, "graph/show", root, args={"ref": "other_graph"}).json["data"]["id"]
    manifest = project_admins["root"] / "graphs" / other / "manifest.toml"
    manifest.write_text(manifest.read_text() + "\nsurprise = 1\n")
    pa = project_admins["pa"]
    listed = command(app, "graph/list", pa)
    assert listed.status == 200 and other not in json.dumps(listed.json), listed.json
    assert other in json.dumps(command(app, "graph/list", root).json)  # a maintainer sees it
    manifest.write_text("not toml [")
    for path, args in (("graph/list", {}), ("graph/show", {"ref": "example_graph"})):
        reply = command(app, path, pa, args=args)
        assert reply.status == 200 and other not in json.dumps(reply.json), reply.json
    missing = command(app, "graph/show", pa, args={"ref": other})
    assert missing.error["code"] == "graph.not_found"


def test_policy_explain_names_what_a_token_gets(
    run_cli: CliRunner, project_admins: dict[str, Any]
) -> None:
    explained = admin(run_cli, "policy", "explain", "--graph", "example_graph", "--token", "pa")
    assert explained.code == 0, explained.envelope
    data = explained.data
    assert (data["token"], data["project"], data["maintains"]) == ("pa", "p1", True)
    assert {d["rule"] for d in data["decisions"]} == {"scope.p1:maintain"}
    other = admin(run_cli, "policy", "explain", "--graph", "other_graph", "--token", "pa")
    assert other.data["maintains"] is False
    app = admin(run_cli, "policy", "explain", "--graph", "other_graph", "--token", "ci")
    assert (app.data["level"], app.data["maintains"]) == ("write", False)
    both = admin(
        run_cli, "policy", "explain", "--graph", "other_graph", "--token", "pa", "--project", "p1"
    )
    assert both.code == 2
    unknown = admin(run_cli, "policy", "explain", "--graph", "other_graph", "--token", "nobody")
    assert unknown.error["code"] == "admin.token_not_found"
    secret = project_admins["pa"]
    mistaken = admin(run_cli, "policy", "explain", "--graph", "other_graph", "--token", secret)
    assert mistaken.code == 2 and secret not in json.dumps(mistaken.envelope)


def test_roles_are_assigned_through_the_admin_api(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    """``token role add|remove`` run from their command specs like every admin command: for
    an admin token only, audited as ``maint.token_update`` with the actor of the token."""
    declared = SERVER_POLICY + 'roles:\n  reader: {description: "Readers"}\n'
    grant_server(run_cli, tmp_path, declared)
    assert admin(run_cli, "token", "create", "svc").code == 0
    app = served(installation, tmp_path)
    tokens = installation["tokens"]
    refused = command(
        app, "token/role/add", tokens["ci"], args={"name": "svc", "roles": ["reader"]}
    )
    assert refused.status == 403
    added = command(
        app, "token/role/add", tokens["root"], args={"name": "svc", "roles": ["reader"]}
    )
    assert added.status == 200, added.json
    assert added.json["data"]["roles"] == ["reader"] and added.json["data"]["changed"] is True
    again = command(
        app, "token/role/add", tokens["root"], args={"name": "svc", "roles": ["reader"]}
    )
    assert again.json["data"]["changed"] is False
    unknown = command(
        app, "token/role/add", tokens["root"], args={"name": "svc", "roles": ["nobody"]}
    )
    assert unknown.error["code"] == "admin.role_unknown"
    assert unknown.error["details"]["known"] == ["reader"]
    listed = command(app, "token/list", tokens["root"], args={"role": "reader"})
    assert [t["name"] for t in listed.json["data"]["tokens"]] == ["svc"]
    removed = command(
        app, "token/role/remove", tokens["root"], args={"name": "svc", "roles": ["reader"]}
    )
    assert removed.json["data"]["roles"] == [] and removed.json["data"]["changed"] is True
    updates = [
        e for e in audit_events(installation["root"]) if e["actions"] == ["maint.token_update"]
    ]
    assert [e["change"]["token"]["roles"] for e in updates] == [["reader"], []]
    assert all(e["actor"]["principal"]["id"] == "token:root" for e in updates)
    assert admin(run_cli, "audit", "verify").code == 0


def test_no_request_names_a_path_on_the_server(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    inventory = call(app, "GET", "/v1/admin/inventory", token=root).json
    options = {o["name"] for c in inventory["commands"] for o in c["options"]}
    assert "--data-root" not in options
    document = call(app, "GET", "/v1/admin/openapi.json", token=root).json
    assert document == admin_document(__version__, dist=False)  # the golden, without dist
    paths = document["paths"]
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


def test_render_and_pull_answer_their_files_instead_of_a_server_path(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """B14.15: the answer as sent (``call`` checks it against the declared schema) names no
    place on the server; the client writes the returned files itself."""
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    empty = {"files": {}, "name": "out"}
    rendered = command(
        app,
        "graph/skill/render",
        root,
        args={"ref": "example_graph", "dir": upload_tree(app, root, GRAPH_SKILL), "--out": empty},
    )
    assert rendered.status == 200, rendered.json
    data = rendered.json["data"]
    assert data["path"] is None and "SKILL.md" in data["rendered"], data
    assert sorted(data["rendered"]) == data["files"]
    pulled = command(app, "graph/pull", root, args={"ref": "example_graph", "dir": empty})
    assert pulled.status == 200, pulled.json
    data = pulled.json["data"]
    assert data["path"] is None and sorted(data["pulled"]) == data["files"], data


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

POLICY = (
    SERVER_POLICY
    + "scopes:\n  example_project: {example_graph: read}\n"
    + 'roles:\n  reader: {description: "Reporting apps"}\n  importer: {description: "Importers"}\n'
)


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
    workspace = cwd / "workspace"
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(cwd / "admin_ws", ignore_errors=True)
    (cwd / "policy.yaml").write_text(POLICY)
    shutil.rmtree(cwd / "no_functions", ignore_errors=True)
    (cwd / "no_functions").mkdir()

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
        ("graph", "functions", "publish", "example_graph", str(BROKEN_FUNCTIONS)),
        ("graph", "functions", "publish", "example_graph", str(FUNCTIONS), "--dry-run"),
        ("graph", "functions", "publish", "example_graph", str(FUNCTIONS)),
        ("graph", "functions", "publish", "example_graph", str(FUNCTIONS)),
        ("graph", "functions", "show", "example_graph"),
        ("graph", "show", "example_graph"),
        ("graph", "functions", "publish", "example_graph", "no_functions"),
        ("graph", "functions", "show", "example_graph"),
        ("graph", "actions", "publish", "example_graph", str(BROKEN_ACTIONS)),
        ("graph", "actions", "publish", "example_graph", str(ACTIONS), "--dry-run"),
        ("graph", "actions", "publish", "example_graph", str(ACTIONS)),
        ("graph", "actions", "publish", "example_graph", str(ACTIONS)),
        ("graph", "actions", "show", "example_graph"),
        ("graph", "show", "example_graph"),
        ("graph", "actions", "publish", "example_graph", "no_functions"),
        ("graph", "actions", "show", "example_graph"),
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
        ("graph", "pull", "example_graph", "workspace"),
        ("graph", "pull", "example_graph", "workspace"),
        ("graph", "pull", "example_graph", "workspace", "--force"),
        ("graph", "pull", "example_graph", "elsewhere", "--version", "9.9.9"),
        ("graph", "push", "example_graph", "workspace", "--dry-run"),
        ("graph", "push", "example_graph", "workspace"),
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
        ("token", "create", "worker", "--role", "reader", "--role", "importer"),
        ("token", "role", "remove", "worker", "importer", "--reason", "no more imports"),
        ("token", "role", "remove", "worker", "importer"),
        ("token", "role", "add", "worker", "importer", "reader"),
        ("token", "role", "add", "worker", "importr"),
        ("token", "role", "add", "root", "reader"),
        ("token", "role", "add", "nobody", "reader"),
        ("token", "list", "--role", "importer"),
        ("token", "revoke", "worker"),
        ("enroll", "create", "--project", "example_project", "--role", "reader"),
        (
            "enroll",
            "create",
            "--project",
            "example_project",
            "--scope",
            "admin",
            "--role",
            "reader",
        ),
        ("enroll", "create", "--project", "example_project", "--reason", "a laptop"),
        ("enroll", "create", "--project", "example_project", "--scope", "admin"),
        ("enroll", "list"),
        ("enroll", "revoke", "0123456789ab"),
        ("files", "verify", "--graph", "example_graph"),
        ("files", "gc", "--graph", "example_graph", "--dry-run"),
        ("audit", "anchor"),
        ("audit", "verify"),
        ("usage", "report"),
        ("usage", "report", "example_graph", "--since", "2026-01-01"),
        ("usage", "report", "missing_graph"),
    ):
        run(*args)
    seen.append(sorted(str(p.relative_to(preview)) for p in preview.rglob("*")))
    model = workspace / "skill" / "graph-example-graph" / "references" / "model.md"
    model.write_text(model.read_text() + "\nOne more line.\n")
    shutil.copytree(V110, workspace / "ontology" / "1.1.0")
    shutil.copytree(FUNCTIONS, workspace / "functions")  # published by the push below
    shutil.copytree(ACTIONS, workspace / "actions")  # published by the push below
    run("graph", "push", "example_graph", "workspace", "--reason", "equivalence")
    run("graph", "push", "example_graph", "workspace")
    run("graph", "push", "second", "workspace")
    seen.append(sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*")))
    seen.append([json.loads((workspace / ".graph_cli_source.json").read_text())])
    run("workspace", "sync", "admin_ws", "--dry-run")
    run("workspace", "sync", "admin_ws")
    run("workspace", "sync", "admin_ws")
    run("skills", "install", "--scope", "project", "--dir", "admin_ws", "--dry-run")
    run("skills", "check", "--scope", "project", "--dir", "admin_ws")
    seen.append(sorted(str(p.relative_to(cwd)) for p in (cwd / "admin_ws").rglob("*")))
    run("audit", "verify")
    run("graph", "list", text=True)
    run("token", "list", text=True)
    return seen


def comparable(outcomes: list[Any], *homes: Path) -> Any:
    text = json.dumps(outcomes)
    for home in homes:
        text = text.replace(str(home), "<home>")
    text = HASH_PREFIX.sub("<hash prefix>", SECRET.sub("<token>", text))
    text = FOLDER.sub(r"\1_<suffix>", text)
    text = ENROLLMENT_ID.sub("<enrollment>", CODE.sub("<code>", text))
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
        ("self", "update"),  # installs this program: tests/unit/catalog/test_dist.py
    }
    mine = comparable(actual, client_home, owner_home, local_home)
    theirs = comparable(expected, local_home, owner_home, client_home)
    assert len(mine) == len(theirs)
    for a, b in zip(mine, theirs, strict=True):
        assert a == b, differences(a, b)


@pytest.mark.parametrize(
    ("skill", "files"),
    [("..", {"SKILL.md": "eA=="}), ("graph-x", {"../SKILL.md": "eA=="}), ("a/b", {})],
)
def test_a_rendered_answer_never_writes_outside_out(
    tmp_path: Path, skill: str, files: dict[str, str]
) -> None:
    from graph_cli.kernel.errors import GraphCliError
    from graph_cli_admin.maintenance_cli.remote import _write_rendered

    out = tmp_path / "out"
    (out / "graph-x").mkdir(parents=True)
    (out / "graph-x" / "keep.md").write_text("kept")
    document = {"data": {"skill": skill, "rendered": files, "path": None}}
    with pytest.raises(GraphCliError) as caught:
        _write_rendered(document, out, {"force": True, "source": "dir"})
    assert caught.value.code == "remote.protocol"
    assert (out / "graph-x" / "keep.md").read_text() == "kept"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


@pytest.mark.parametrize(
    "files",
    [
        {"../x.yaml": "eA=="},
        {"ontology/1.0.0/../../x": "eA=="},
        {"skill/graph-x/../../x": "eA=="},
        {"ontology/1.0.0/inventory.yaml": "eA=="},  # no base file
        {".graph_cli_source.json": "eA==", "README.md": "eA=="},
    ],
)
def test_a_pulled_answer_never_writes_outside_the_workspace(
    tmp_path: Path, files: dict[str, str]
) -> None:
    from graph_cli.kernel.errors import GraphCliError
    from graph_cli_admin.maintenance_cli.remote import _write_pulled

    target = tmp_path / "work"
    document = {"data": {"pulled": files, "path": None}}
    with pytest.raises(GraphCliError) as caught:
        _write_pulled(document, target, {"ref": "example_graph", "force": True})
    assert caught.value.code == "remote.protocol"
    assert sorted(p.name for p in tmp_path.iterdir()) == []
