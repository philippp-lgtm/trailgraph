"""C: writes of tokens with read limits through the server in-process (type read limits plan V6).

The role ``reporting`` reads items of ``example_graph`` and may update them; ``curator`` may write
everything. Token A holds ``reporting``, token B both roles, so B sees every type. A create of a
location (a type A does not see) is refused to A as a create of a type that does not exist; B
creates it. A's update of an item keeps the item's edge to its location, which A cannot see, and
the refusal of A is audited with the decision that hides the type.
"""

import io
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, data_root, init
from support.app import create_graph
from support.policies import write_policy
from support.server import call, new_token

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: items only"}
  curator: {description: "Curators: write everything"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [inv:Item]
    writes:
      inv:Item: {update: true}
  - {entrypoint: server, principal: {role: curator}, graphs: [example_graph], level: write}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
GRAPH = {"--graph": "example_graph"}
GHOST = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"


@pytest.fixture
def served(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> dict[str, Any]:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    path = write_policy(tmp_path / "policies", "installation.yaml", INSTALLATION)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "read limits")
    assert applied.code == 0, applied.envelope
    tokens = {
        "a": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reporting"),
        "b": new_token(
            *(run_cli, "app_b", "--project", "example_project"),
            *("--role", "reporting", "--role", "curator"),
        ),
    }
    app = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)
    return {"app": app, "tokens": tokens, "root": data_root(isolated_env)}


def command(world: dict[str, Any], path: str, who: str, **args: Any) -> Any:
    token = world["tokens"][who]
    return call(world["app"], "POST", f"/v1/commands/{path}", token=token, body={"args": args})


def create(world: dict[str, Any], who: str, type_: str, *sets: str, **extra: Any) -> Any:
    return command(world, "create", who, type=type_, **{"--set": list(sets), **extra, **GRAPH})


def test_a_create_of_a_type_a_token_does_not_see_is_a_create_of_an_unknown_type(
    served: dict[str, Any],
) -> None:
    hidden = create(served, "a", "inv:Location", "name=Shelf", "code=S1")
    unknown = create(served, "a", "inv:Gone", "name=Shelf", "code=S1")
    assert hidden.status == unknown.status == 403
    assert hidden.error["code"] == unknown.error["code"] == "access.denied"
    assert hidden.error["details"]["rule"] == unknown.error["details"]["rule"] == "default.deny"
    assert str(hidden.json).replace("inv:Location", "inv:Gone") == str(unknown.json)


def test_a_token_that_sees_everything_creates_the_type_and_the_other_reads_its_item(
    served: dict[str, Any],
) -> None:
    where = create(served, "b", "inv:Location", "name=Shelf", "code=S1")
    assert where.status == 200, where.json
    location = where.json["data"]["entities"][0]["id"]
    item = create(
        served,
        "b",
        "inv:Item",
        "name=Chair",
        "quantity=1",
        **{"--edge": [f"inv:located_in={location}"]},
    )
    assert item.status == 200, item.json
    chair = item.json["data"]["entities"][0]["id"]
    # A reads the item without its edge to the location, and updates it
    seen = command(served, "get", "a", ref=chair, **GRAPH)
    assert seen.json["data"]["entity"]["edges"] == []
    renamed = command(served, "update", "a", ref=chair, **{"--set": ["name=Armchair"], **GRAPH})
    assert renamed.status == 200, renamed.json
    assert location not in str(renamed.json)
    # the edge is still there for the token that sees it
    whole = command(served, "get", "b", ref=chair, **GRAPH)
    assert [e["target"] for e in whole.json["data"]["entity"]["edges"]] == [location]
    assert whole.json["data"]["entity"]["attrs"]["name"] == "Armchair"
    # a link to the location, by A, is a link to a missing entity
    linked = command(served, "link", "a", ref=chair, rel="inv:located_in", target=location, **GRAPH)
    ghost = command(served, "link", "a", ref=chair, rel="inv:located_in", target=GHOST, **GRAPH)
    assert linked.status == ghost.status
    assert str(linked.json).replace(location, GHOST) == str(ghost.json)


def test_the_refusal_is_audited_with_the_decision_that_hides_the_type(
    run_cli: CliRunner, served: dict[str, Any]
) -> None:
    assert create(served, "a", "inv:Location", "name=Shelf", "code=S1").status == 403
    assert admin(run_cli, "audit", "verify").code == 0
    lines = FileAuditLog(served["root"]).read_lines(INSTALLATION_STREAM)
    report = verify_stream(lines, stream=INSTALLATION_STREAM)
    assert report.findings == ()
    (event,) = [dict(e) for e in report.events if e["result"]["status"] == "denied"]
    assert event["result"] == {"status": "denied", "code": "access.denied"}
    hidden = [
        d
        for d in event["authz"]["decisions"]
        if d["action"] == "read.get" and d["resource"].endswith("/type:inv:Location")
    ]
    assert [d["effect"] for d in hidden] == ["deny"]
