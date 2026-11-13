"""C: enrollment codes and ``POST /v1/enroll`` in-process (contracts B14.21).

A maintainer's code is exchanged once for a token bound to its project; a
check says what the code is for without using it; a wrong, used, revoked or
expired code gets one answer; a taken name leaves the code unused; the
exchange is audited as the enrollment and ``audit verify`` replays it.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
from support.openapi import answer_validator, app_document
from support.server import call, grant_server

from graph_cli import __version__
from graph_cli.catalog.enrollments import code_hash
from graph_cli.storage.files.token_files import enrollments_path, tokens_path
from graph_cli_server.admin_api import AdminApi
from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps"}
  importer: {description: "Importers"}
grants:
  - {entrypoint: app, graphs: "*", level: write}
  - {entrypoint: server, principal: {project: p1}, graphs: "*", level: write}
  - {entrypoint: server, principal: {id: "token:p1.reserved"}, graphs: "*", level: write}
scopes:
  p1: {example_graph: maintain}
  p2: {other_graph: read}
no_project: {"*": write}
"""


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    create_graph(run_cli, "other_graph")
    grant_server(run_cli, tmp_path, POLICY)
    return {"root": root, "env": isolated_env}


def served(installation: dict[str, Any], cwd: Path) -> tuple[ServerApp, io.StringIO]:
    log = io.StringIO()
    settings = Settings.load(installation["env"], cwd)
    return ServerApp(settings, log=log, admin=AdminApi, enroll=Enrollment), log


def code(run_cli: CliRunner, *args: str) -> dict[str, Any]:
    created = admin(run_cli, "enroll", "create", *args)
    assert created.code == 0, created.envelope
    data: dict[str, Any] = created.data
    return data


def installation_events(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for segment in sorted((root / "audit").rglob("*.jsonl")):
        found += [json.loads(line) for line in segment.read_text().splitlines()]
    return found


def test_a_code_is_checked_then_exchanged_once(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app, log = served(installation, tmp_path)
    root = installation["root"]
    created = code(run_cli, "--project", "p1")
    secret = created["code"]
    assert created["scope"] == "app" and created["expired"] is False

    checked = call(app, "POST", "/v1/enroll", body={"code": secret, "check": True})
    assert checked.status == 200, checked.json
    assert checked.json["data"]["enrollment"] == {
        "id": created["id"],
        "project": "p1",
        "scope": "app",
        "expires_at": created["expires_at"],
        "graphs": ["example_graph"],
    }
    assert code_hash(secret) in enrollments_path(root).read_text()  # still unused

    exchanged = call(app, "POST", "/v1/enroll", body={"code": secret, "label": "laptop"})
    assert exchanged.status == 200, exchanged.json
    data = exchanged.json["data"]
    assert data["name"] == "p1.laptop" and data["project"] == "p1" and data["scope"] == "app"
    assert data["graphs"] == ["example_graph"]
    token = data["token"]
    assert token.startswith("gct_")
    assert admin(run_cli, "enroll", "list").data["enrollments"] == []

    whoami = call(app, "POST", "/v1/commands/whoami", token=token, body={})
    assert whoami.status == 200, whoami.json
    assert whoami.json["data"]["actor"]["project"]["assurance"] == "verified"

    again = call(app, "POST", "/v1/enroll", body={"code": secret, "check": True})
    assert again.status == 401 and again.error["code"] == "remote.unauthorized"

    (event,) = [
        e for e in installation_events(root) if e["request"]["command"] == "enroll exchange"
    ]
    assert event["actions"] == ["maint.token_create"]
    assert event["actor"]["principal"] == {
        "assurance": "verified",
        "id": f"enrollment:{created['id']}",
        "source": "enrollment",
    }
    assert event["authz"]["decisions"][0]["rule"] == f"enrollment:{created['id']}"
    assert event["change"]["enrollment"] == {
        "id": created["id"],
        "hash_prefix": created["hash_prefix"],
    }
    assert event["change"]["token"]["name"] == "p1.laptop"
    for path in root.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
            assert token.encode() not in path.read_bytes(), path
    assert secret not in log.getvalue() and token not in log.getvalue()
    enrolls = [line for line in log.getvalue().splitlines() if " /v1/enroll " in line]
    assert len(enrolls) == 3 and all(line.endswith("token=-") for line in enrolls)
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope


def test_the_openapi_document_describes_enrollment(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    """B14.22 G4: ``POST /v1/enroll`` is in the app document, without a token; the check,
    the exchange and a refusal fit their responses, closed."""
    app, _ = served(installation, tmp_path)
    secret = code(run_cli, "--project", "p1")["code"]
    checked = call(app, "POST", "/v1/enroll", body={"code": secret, "check": True})
    exchanged = call(app, "POST", "/v1/enroll", body={"code": secret, "label": "laptop"})
    refused = call(app, "POST", "/v1/enroll", body={"code": secret})
    token = exchanged.json["data"]["token"]
    document = call(app, "GET", "/v1/openapi.json", token=token).json
    assert document == app_document(__version__, dist=False)
    operation = document["paths"]["/v1/enroll"]["post"]
    assert operation["security"] == []
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["additionalProperties"] is False and body["required"] == ["code"]
    for reply, status in ((checked, "200"), (exchanged, "200"), (refused, "401")):
        assert str(reply.status) == status, reply.json
        validator = answer_validator(document, "/v1/enroll", "post", status)
        assert not [e.message for e in validator.iter_errors(reply.json)], reply.json


def test_wrong_used_revoked_and_expired_codes_get_one_answer(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app, _ = served(installation, tmp_path)
    root = installation["root"]
    revoked = code(run_cli, "--project", "p1")
    assert admin(run_cli, "enroll", "revoke", revoked["id"]).code == 0
    expired = code(run_cli, "--project", "p1")
    document = json.loads(enrollments_path(root).read_text())
    document["enrollments"][0]["expires_at"] = "2020-01-01T00:00:00.000Z"
    enrollments_path(root).write_text(json.dumps(document))
    answers = set()
    for given in ("gce_" + "a" * 26, revoked["code"], expired["code"], "nonsense", ""):
        reply = call(app, "POST", "/v1/enroll", body={"code": given, "check": True})
        assert reply.status == 401, reply.json
        answers.add(json.dumps(reply.json["error"], sort_keys=True))
    assert len(answers) == 1
    assert not tokens_path(root).exists()


def test_the_client_picks_only_a_label_and_a_taken_name_leaves_the_code_unused(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    """The token is ``<project>.<label>``: a code holder can neither take a name a policy
    rule grants something to nor pose as another token."""
    app, _ = served(installation, tmp_path)
    assert admin(run_cli, "token", "create", "p1.laptop").code == 0
    secret = code(run_cli, "--project", "p1")["code"]
    for label in ("laptop", "reserved"):  # a token has it; the policy names token:p1.reserved
        taken = call(app, "POST", "/v1/enroll", body={"code": secret, "label": label})
        assert taken.status == 400 and taken.error["details"]["argument"] == "label"
        assert taken.error["details"]["actual"] == f"p1.{label}"
    done = call(app, "POST", "/v1/enroll", body={"code": secret, "label": "CI\n"})
    assert done.status == 200, done.json
    assert done.json["data"]["name"] == "p1.ci"
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_code_used_before_but_still_listed_is_refused(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    """An exchange cut short between its event and the enrollment file leaves the code
    listed; the installation stream says it was used, so it never works twice."""
    app, _ = served(installation, tmp_path)
    root = installation["root"]
    secret = code(run_cli, "--project", "p1")["code"]
    listed = enrollments_path(root).read_bytes()
    assert call(app, "POST", "/v1/enroll", body={"code": secret, "label": "a"}).status == 200
    enrollments_path(root).write_bytes(listed)  # what the cut-short exchange leaves
    again = call(app, "POST", "/v1/enroll", body={"code": secret, "label": "b"})
    assert again.status == 401
    assert [t["name"] for t in admin(run_cli, "token", "list").data["tokens"]] == ["p1.a"]


def test_an_admin_code_makes_a_project_admin(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app, _ = served(installation, tmp_path)
    created = code(run_cli, "--project", "p1", "--scope", "admin", "--expires-in", "2h")
    checked = call(app, "POST", "/v1/enroll", body={"code": created["code"], "check": True})
    assert checked.json["data"]["enrollment"]["graphs"] == ["example_graph"]
    reply = call(app, "POST", "/v1/enroll", body={"code": created["code"], "label": "admin"})
    assert reply.status == 200, reply.json
    token = reply.json["data"]["token"]
    assert reply.json["data"]["scope"] == "admin"
    listed = call(app, "POST", "/v1/admin/commands/graph/list", token=token, body={})
    assert [g["name"] for g in listed.json["data"]["items"]] == ["example_graph"]
    refused = call(app, "POST", "/v1/admin/commands/token/list", token=token, body={})
    assert refused.status == 403
    app_route = call(app, "POST", "/v1/commands/whoami", token=token, body={})
    assert app_route.status == 403  # an admin token reaches the admin API only


def test_the_route_is_strict_about_its_body(installation: dict[str, Any], tmp_path: Path) -> None:
    app, _ = served(installation, tmp_path)
    for body in ({"code": 1}, {"code": "x", "extra": True}, {"code": "x", "check": "yes"}, []):
        reply = call(app, "POST", "/v1/enroll", body=body)
        assert reply.status == 400, reply.json
    large = call(app, "POST", "/v1/enroll", raw=b" " * 5000)
    assert large.status == 400
    wrong = call(app, "GET", "/v1/enroll")
    assert wrong.status == 405 and wrong.headers["Allow"] == "POST"
    off = ServerApp(Settings.load(installation["env"], tmp_path), log=io.StringIO())
    assert call(off, "POST", "/v1/enroll", body={"code": "x"}).status == 404
    assert call(off, "GET", "/v1/enroll").status == 404  # no route, whatever the method


def test_the_error_registry_does_not_tell_whether_the_admin_api_is_on(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """graph-server always enrolls, so the public registry always names the admin codes."""
    settings = Settings.load(installation["env"], tmp_path)
    app = ServerApp(settings, log=io.StringIO(), enroll=Enrollment)
    assert app.admin is None
    assert call(app, "GET", "/v1/errors/admin.model_invalid").status == 200


def test_enroll_create_refuses_and_warns(run_cli: CliRunner, installation: dict[str, Any]) -> None:
    for args, argument in (
        (("--project", "-x"), "--project"),
        (("--project", "p1", "--expires-in", "25h"), "--expires-in"),
        (("--project", "p1", "--expires-in", "0m"), "--expires-in"),
        (("--project", "p1", "--expires-in", "1d"), "--expires-in"),
    ):
        refused = admin(run_cli, "enroll", "create", *args)
        assert refused.code == 2 and refused.error["details"]["argument"] == argument
    idle = admin(run_cli, "enroll", "create", "--project", "p3")
    assert idle.warning_codes == ["admin.token_not_granted"]
    idle_admin = admin(run_cli, "enroll", "create", "--project", "p2", "--scope", "admin")
    assert idle_admin.warning_codes == ["admin.token_not_maintainer"]
    gone = admin(run_cli, "enroll", "revoke", "0123456789ab")
    assert gone.code == 1 and gone.error["code"] == "admin.enrollment_not_found"
    assert gone.error["hint"] == "graph-admin enroll list"
    pasted = admin(run_cli, "enroll", "revoke", idle.data["code"])
    assert pasted.code == 2 and idle.data["code"] not in json.dumps(pasted.envelope)


def test_the_exchange_gives_the_new_token_the_roles_of_its_code(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    """B14.21, B6.7: ``enroll create --role`` puts the roles on the code, listed and
    audited; the token holds them, the exchange event names them, a request with the token
    has them as ``actor.roles`` and ``audit verify`` replays code and exchange."""
    app, _ = served(installation, tmp_path)
    root = installation["root"]
    created = code(run_cli, "--project", "p1", "--role", "reader", "--role", "importer")
    assert created["roles"] == ["importer", "reader"]
    (listed,) = admin(run_cli, "enroll", "list").data["enrollments"]
    assert listed["roles"] == ["importer", "reader"]
    (made,) = [e for e in installation_events(root) if e["request"]["command"] == "enroll create"]
    assert made["change"]["enrollment"]["roles"] == ["importer", "reader"]
    exchanged = call(app, "POST", "/v1/enroll", body={"code": created["code"], "label": "laptop"})
    assert exchanged.status == 200, exchanged.json
    token = exchanged.json["data"]["token"]
    (record,) = json.loads(tokens_path(root).read_text())["tokens"]
    assert record["roles"] == ["importer", "reader"]
    (event,) = [
        e for e in installation_events(root) if e["request"]["command"] == "enroll exchange"
    ]
    assert event["change"]["token"]["roles"] == ["importer", "reader"]
    whoami = call(app, "POST", "/v1/commands/whoami", token=token, body={})
    assert whoami.status == 200, whoami.json
    assert whoami.json["data"]["actor"]["roles"] == ["importer", "reader"]
    assert admin(run_cli, "audit", "verify").code == 0


def test_enroll_create_refuses_unusable_roles(
    run_cli: CliRunner, installation: dict[str, Any]
) -> None:
    admin_scope = admin(
        run_cli, "enroll", "create", "--project", "p1", "--scope", "admin", "--role", "reader"
    )
    assert admin_scope.code == 2 and admin_scope.error["details"]["argument"] == "--role"
    bad = admin(run_cli, "enroll", "create", "--project", "p1", "--role", "Not A Role")
    assert bad.code == 2 and bad.error["details"]["argument"] == "--role"
    unknown = admin(run_cli, "enroll", "create", "--project", "p1", "--role", "reder")
    assert unknown.code == 1 and unknown.error["code"] == "admin.role_unknown"
    assert unknown.error["details"]["did_you_mean"] == ["reader"]
    assert admin(run_cli, "enroll", "list").data["enrollments"] == []
    idle = admin(run_cli, "enroll", "create", "--project", "p3", "--role", "importer")
    assert idle.warning_codes == ["admin.token_not_granted"]  # no grant for an importer, no p3


def test_a_code_without_roles_gives_a_token_without_roles(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app, _ = served(installation, tmp_path)
    root = installation["root"]
    secret = code(run_cli, "--project", "p1")["code"]
    exchanged = call(app, "POST", "/v1/enroll", body={"code": secret, "label": "laptop"})
    assert exchanged.status == 200, exchanged.json
    (record,) = json.loads(tokens_path(root).read_text())["tokens"]
    assert "roles" not in record
    whoami = call(
        app, "POST", "/v1/commands/whoami", token=exchanged.json["data"]["token"], body={}
    )
    assert "roles" not in whoami.json["data"]["actor"]
