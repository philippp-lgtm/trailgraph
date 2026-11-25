"""C: a stored file of a ``files:File`` subclass with a marked attribute, read over the server by
a token that lacks the marking (field visibility plan leak item 10, section 1a): the download
route ``GET /v1/graphs/{graph}/files/{ref}`` and ``file path`` answer it exactly as for a token
that holds the marking (they send the bundled file attributes only), while ``get`` shows the
marked ``remark`` to the holder only. The full end-to-end test of markings is step 8's.
"""

import io
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app
from support.files import write_pdf
from support.markings import STAFF
from support.policies import write_policy
from support.server import call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal data of colleagues"}
  reporting: {description: "Reporting: colleagues without personal data"}
grants:
  - {entrypoint: server, principal: {role: hr}, graphs: [example_graph], level: read,
     markings: [stf:personal]}
  - {entrypoint: server, principal: {role: reporting}, graphs: [example_graph], level: read}
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [stf:personal]}
scopes:
  example_project: {example_graph: read}
no_project: {"*": write}  # the local app that adds the file
"""
REMARK = "Confidential remark of the personnel office"
GRAPH = {"--graph": "example_graph"}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    policy = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "markings")
    assert applied.code == 0, applied.envelope
    content = write_pdf(tmp_path / "contract.pdf", "contract")
    added = app(
        run_cli,
        *("file", "add", str(content), "--type", "stf:PersonnelFile"),
        *("--set", f"remark={REMARK}", "--no-origin"),
        env={"GRAPH_CLI_GRAPH": "example_graph"},
    )
    assert added.code == 0, added.envelope
    tokens = {
        "reporting": new_token(
            run_cli, "app_a", "--project", "example_project", "--role", "reporting"
        ),
        "hr": new_token(run_cli, "app_b", "--project", "example_project", "--role", "hr"),
    }
    return {
        "env": isolated_env,
        "tokens": tokens,
        "file": added.data["entity"]["id"],
        "bytes": content.read_bytes(),
    }


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> ServerApp:
    return ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )


def command(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


@pytest.mark.doc_claim("security_model.file_attributes_visible")
def test_the_download_and_file_path_answer_a_reader_without_the_marking_as_a_holder(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    tokens, entity = installation["tokens"], installation["file"]
    route = f"/v1/graphs/example_graph/files/{entity}"
    lacking = call(served, "GET", route, token=tokens["reporting"])
    holding = call(served, "GET", route, token=tokens["hr"])
    assert lacking.status == holding.status == 200
    assert lacking.body == holding.body == installation["bytes"]
    assert lacking.headers == holding.headers
    assert REMARK not in str(lacking.headers)
    paths = [
        command(served, "file/path", tokens[n], ref=entity, **GRAPH) for n in ("reporting", "hr")
    ]
    assert paths[0].status == paths[1].status == 200
    assert paths[0].json["data"] == paths[1].json["data"]
    assert REMARK.encode() not in paths[0].body
    # the entity itself: the marked remark for the holder only, the hash of what is shown
    shown = command(served, "get", tokens["reporting"], ref=entity, **GRAPH).json["data"]["entity"]
    held = command(served, "get", tokens["hr"], ref=entity, **GRAPH).json["data"]["entity"]
    assert "remark" not in shown["attrs"] and held["attrs"]["remark"] == REMARK
    assert {k: v for k, v in held["attrs"].items() if k != "remark"} == shown["attrs"]
