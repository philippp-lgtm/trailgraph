"""C: roles of app tokens end to end through the server in-process (contracts B6.7, B14.11,
B14.21).

One project, a reader token and an importer token; the installation policy declares the
roles and grants them, a graph policy denies the importer one type. The installation is
prepared with the real ``graph-admin``; requests go through ``ServerApp`` without a socket.
A role takes effect with the next request, a membership change is audited and replayed by
``audit verify``, and the roles are recorded in the actors of the audit events and of the
transactions that a token's requests write.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.policies import write_policy
from support.server import call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps: read example_graph"}
  importer: {description: "Importers: write into example_graph"}
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}
  - {entrypoint: server, principal: {role: importer}, graphs: [example_graph], level: write}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
GRAPH = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: importer_only_some_type, role: [importer], action: ["write.*"], type: ["inv:Category"]}
"""
GRAPH_ARGS = {"--graph": "example_graph"}
ALLOWED = {"type": "inv:Location", "--set": ["name=Shelf", "code=S1"], **GRAPH_ARGS}
DENIED = {"type": "inv:Category", "--set": ["name=Books"], **GRAPH_ARGS}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    work = tmp_path / "policies"
    for name, text, args in (
        ("installation.yaml", INSTALLATION, ()),
        ("graph.yaml", GRAPH, ("--graph", "example_graph")),
    ):
        path = write_policy(work, name, text)
        applied = admin(run_cli, "policy", "apply", str(path), *args, "--reason", "roles")
        assert applied.code == 0, applied.envelope
        assert applied.warnings == []
    tokens = {
        "reader": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reader"),
        "importer": new_token(
            run_cli, "app_b", "--project", "example_project", "--role", "importer"
        ),
    }
    return {"root": root, "env": isolated_env, "tokens": tokens}


def served(installation: dict[str, Any], cwd: Path) -> ServerApp:
    settings = Settings.load(installation["env"], cwd)
    return ServerApp(settings, log=io.StringIO(), enroll=Enrollment)


def command(app: ServerApp, path: str, token: str, **body: Any) -> Any:
    return call(app, "POST", f"/v1/commands/{path}", token=token, body=body)


def audit_events(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for segment in sorted(p for p in root.rglob("*.jsonl") if "audit" in p.parts):
        found += [json.loads(line) for line in segment.read_text().splitlines()]
    return found


def tx_records(root: Path) -> list[dict[str, Any]]:
    """Every record of every graph's transaction log."""
    found: list[dict[str, Any]] = []
    for segment in sorted(root.glob("graphs/*/data/txlog/*.jsonl")):
        found += [json.loads(line) for line in segment.read_text().splitlines()]
    return found


def test_roles_decide_what_each_app_of_a_project_may_do(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root = installation["root"]
    reader, importer = installation["tokens"]["reader"], installation["tokens"]["importer"]
    assert admin(run_cli, "audit", "verify").code == 0

    seen = command(app, "whoami", reader)
    assert seen.json["data"]["actor"]["roles"] == ["reader"]
    assert command(app, "whoami", importer).json["data"]["actor"]["roles"] == ["importer"]

    # A reads but cannot write: no rule of the policy lets a reader write
    denied = command(app, "create", reader, args=ALLOWED)
    assert denied.status == 403 and denied.error["code"] == "access.denied", denied.json
    assert denied.error["details"]["rule"] == "default.deny"
    assert command(app, "find", reader, args={"type": ["inv:Location"], **GRAPH_ARGS}).status == 200

    # B writes an allowed type but not the type the graph policy denies it
    created = command(app, "create", importer, args=ALLOWED)
    assert created.status == 200, created.json
    entity = created.json["data"]["entities"][0]["id"]
    assert command(app, "get", reader, args={"ref": entity, **GRAPH_ARGS}).status == 200
    refused = command(app, "create", importer, args=DENIED)
    assert refused.status == 403 and refused.error["code"] == "access.denied", refused.json
    assert refused.error["details"]["rule"] == "deny.importer_only_some_type"

    # policy explain says what the requests above observed
    for role, kind, effect, rule in (
        ("importer", "inv:Category", "deny", "deny.importer_only_some_type"),
        ("importer", "inv:Location", "allow", "grant.2"),
        ("reader", "inv:Location", "deny", "default.deny"),
    ):
        explained = admin(
            run_cli,
            *("policy", "explain", "--graph", "example_graph", "--project", "example_project"),
            *("--role", role, "--action", "write.create", "--type", kind),
        )
        assert explained.code == 0 and explained.warnings == [], explained.envelope
        (decision,) = explained.data["decisions"]
        assert (explained.data["roles"], decision["effect"], decision["rule"]) == (
            [role],
            effect,
            rule,
        )

    # the events and the transaction of B's write carry its roles
    events = [e for e in audit_events(root) if e["actor"]["principal"]["id"] == "token:app_b"]
    assert events and all(e["actor"]["roles"] == ["importer"] for e in events)
    assert {e["result"]["status"] for e in events} >= {"ok", "denied"}
    written = [t for t in tx_records(root) if t["actor"]["principal"]["id"] == "token:app_b"]
    assert len(written) == 1 and written[0]["actor"]["roles"] == ["importer"]
    ok = [e["actor"] for e in events if e["result"]["status"] == "ok"]
    assert written[0]["actor"] in ok  # B2.9: the same actor in the tx record and the event
    assert admin(run_cli, "audit", "verify").code == 0

    # taking the role away changes the very next request
    removed = admin(run_cli, "token", "role", "remove", "app_b", "importer")
    assert removed.code == 0 and removed.data["roles"] == []
    after = command(app, "create", importer, args={**ALLOWED, "--set": ["name=Rack", "code=R1"]})
    assert after.status == 404 and after.error["code"] == "graph.not_found"  # nothing is granted
    assert "roles" not in command(app, "whoami", importer).json["data"]["actor"]
    assert admin(run_cli, "token", "role", "add", "app_b", "importer").data["changed"] is True
    again = command(app, "create", importer, args={**ALLOWED, "--set": ["name=Rack", "code=R1"]})
    assert again.status == 200, again.json
    assert admin(run_cli, "audit", "verify").code == 0


def test_an_enrollment_code_with_a_role_yields_a_token_that_holds_it(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    created = admin(run_cli, "enroll", "create", "--project", "example_project", "--role", "reader")
    assert created.code == 0, created.envelope
    assert created.warnings == []  # the policy grants a reader
    exchanged = call(
        app, "POST", "/v1/enroll", body={"code": created.data["code"], "label": "dashboard"}
    )
    assert exchanged.status == 200, exchanged.json
    assert exchanged.json["data"]["graphs"] == ["example_graph"]
    token = exchanged.json["data"]["token"]
    seen = command(app, "whoami", token)
    assert seen.json["data"]["actor"]["roles"] == ["reader"]
    assert command(app, "create", token, args=ALLOWED).error["code"] == "access.denied"
    listed = admin(run_cli, "token", "list", "--role", "reader").data["tokens"]
    assert sorted(t["name"] for t in listed) == ["app_a", "example_project.dashboard"]
    assert admin(run_cli, "audit", "verify").code == 0
