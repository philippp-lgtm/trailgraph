"""C: ``whoami`` of a token whose role carries a write allow list, through the server
in-process (write allow lists plan W2, W10).

The installation policy grants the role ``importer`` a read level plus ``writes`` and the role
``reader`` a read level. A request of each goes through ``ServerApp`` without a socket: the
importer's ``whoami`` lists what it may write and the writes it may and may not make observe
that list; the reader's carries no ``writes`` key. ``policy explain --role`` says the same.
"""

import io
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
  importer: {description: "Importers: items and their locations in example_graph"}
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    writes:
      inv:Location: {create: true, update: [name]}
      inv:Item:
        create: [name, quantity, condition]
        update: [condition]
        link: [inv:located_in]
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
WRITES = {
    "inv:Item": {
        "create": ["condition", "name", "quantity"],
        "update": ["condition"],
        "link": ["inv:located_in"],
    },
    "inv:Location": {"create": True, "update": ["name"]},
}
GRAPH = {"--graph": "example_graph"}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    path = write_policy(tmp_path / "policies", "installation.yaml", INSTALLATION)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "write allow list")
    assert applied.code == 0, applied.envelope
    tokens = {
        "reader": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reader"),
        "importer": new_token(
            run_cli, "app_b", "--project", "example_project", "--role", "importer"
        ),
    }
    return {"env": isolated_env, "tokens": tokens}


def command(app: ServerApp, path: str, token: str, **body: Any) -> Any:
    return call(app, "POST", f"/v1/commands/{path}", token=token, body=body)


def graph_entry(app: ServerApp, token: str) -> dict[str, Any]:
    seen = command(app, "whoami", token)
    assert seen.status == 200, seen.json
    (graph,) = seen.json["data"]["permissions"]["graphs"]
    assert isinstance(graph, dict)
    return graph


def test_whoami_over_the_server_lists_the_writes_of_a_role(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    app = ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )
    reader, importer = installation["tokens"]["reader"], installation["tokens"]["importer"]

    graph = graph_entry(app, importer)
    assert graph["level"] == "read" and graph["writes"] == WRITES
    reading = graph_entry(app, reader)
    assert sorted(reading) == ["graph", "level", "obligations", "types", "withheld"]

    # what the list holds goes through, what it leaves out is refused with the term it names
    location = command(
        app,
        "create",
        importer,
        args={"type": "inv:Location", "--set": ["name=Shelf", "code=S1"], **GRAPH},
    )
    assert location.status == 200, location.json
    where = location.json["data"]["entities"][0]["id"]
    item = command(
        app,
        "create",
        importer,
        args={
            "type": "inv:Item",
            "--set": ["name=Chair", "quantity=1"],
            "--edge": [f"inv:located_in={where}"],
            **GRAPH,
        },
    )
    assert item.status == 200, item.json
    renamed = command(app, "update", importer, args={"ref": where, "--set": ["name=Rack"], **GRAPH})
    assert renamed.status == 200, renamed.json
    refused = {
        "location code": command(
            app, "update", importer, args={"ref": where, "--set": ["code=S2"], **GRAPH}
        ),
        "item attribute": command(
            app,
            "create",
            importer,
            args={
                "type": "inv:Item",
                "--set": ["name=Desk", "acquired_on=2026-01-01"],
                "--edge": [f"inv:located_in={where}"],
                **GRAPH,
            },
        ),
    }
    for name, reply in refused.items():
        assert reply.status == 403 and reply.error["code"] == "access.denied", (name, reply.json)
    assert refused["location code"].error["details"]["resource"].endswith("/attr:code")
    assert refused["item attribute"].error["details"]["resource"].endswith("/attr:acquired_on")

    # policy explain says the same, from the roles alone
    explained = admin(
        run_cli,
        *("policy", "explain", "--graph", "example_graph", "--project", "example_project"),
        *("--role", "importer"),
    )
    assert explained.code == 0, explained.envelope
    assert explained.data["writes"] == WRITES
    assert admin(run_cli, "audit", "verify").code == 0
