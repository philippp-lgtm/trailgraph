"""C: a reconcile document over ``/v1/commands/apply`` (reconcile plan section 9, step 4): the
answer is the one the command prints locally, it fits the OpenAPI document, the refusal of a
guard is a 422 with the code of the plan, and the server's audit names the source of every
event it wrote for the document."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init, parse
from support.openapi import answer_validator, app_document
from support.reconcile_toy import _MODULE, doc, rows, scope
from support.serve import client_env, graph_server
from support.server import call, grant_server, new_token

from graph_cli import __version__
from graph_cli.kernel.canonical import hash_bytes
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

SOURCE = {"name": "inventory", "version": "collector 3", "run": "run-1"}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    ontology = tmp_path / "ontology"
    ontology.mkdir()
    (ontology / "example_module.yaml").write_text(_MODULE)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(ontology), "--language", "en"
    )
    assert created.code == 0, created.envelope
    grant_server(run_cli, tmp_path)
    return {"root": root, "env": isolated_env, "token": new_token(run_cli, "pipeline")}


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> ServerApp:
    return ServerApp(Settings.load(installation["env"], tmp_path), log=io.StringIO())


def apply(
    app: ServerApp, installation: dict[str, Any], document: dict[str, Any], *flags: str
) -> Any:
    data = json.dumps(document).encode()
    token = installation["token"]
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=data)
    assert uploaded.status == 200, uploaded.json
    assert uploaded.json["upload"] == hash_bytes(data)
    args: dict[str, Any] = {
        "file": {"upload": uploaded.json["upload"], "name": "run.json"},
        "--graph": "example_graph",
    }
    args.update({flag: True for flag in flags})
    return call(app, "POST", "/v1/commands/apply", token=token, body={"args": args})


def events(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for segment in sorted(p for p in root.rglob("*.jsonl") if "audit" in p.parts):
        found += [json.loads(line) for line in segment.read_text().splitlines()]
    return found


def test_a_reconcile_document_over_the_api(served: ServerApp, installation: dict[str, Any]) -> None:
    document = doc(scope(complete=True, items=rows("curl", "jq", "zlib")))
    first = apply(served, installation, document)
    assert first.status == 200, first.json
    result = first.json["data"]["results"][0]["result"]
    assert result["seq"] == 1 and result["entity_count"] == 3
    assert result["reconcile"]["source"] == SOURCE
    (counts,) = result["reconcile"]["scopes"]
    assert (counts["created"], counts["unchanged"], counts["retired"]) == (3, 0, 0)
    # the same document again changes nothing: no tx, every item unchanged
    again = apply(served, installation, document)
    (counts,) = again.json["data"]["results"][0]["result"]["reconcile"]["scopes"]
    assert again.json["data"]["results"][0]["result"]["seq"] is None
    assert (counts["created"], counts["unchanged"]) == (0, 3)
    dry = apply(served, installation, doc(scope(complete=True, items=rows("curl"))), "--dry-run")
    (counts,) = dry.json["data"]["results"][0]["result"]["reconcile"]["scopes"]
    assert dry.json["data"]["dry_run"] is True and counts["retired_keys"] == [
        "host_a/jq",
        "host_a/zlib",
    ]
    # the answers fit the document of the API
    spec = app_document(__version__, dist=False, enroll=False)
    validator = answer_validator(spec, "/v1/commands/apply", "post", "200")
    for reply in (first, again, dry):
        assert not [e.message for e in validator.iter_errors(reply.json)], reply.json


def test_a_guard_refusal_is_a_422_with_the_code_of_the_plan(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    names = [f"p{n:02d}" for n in range(20)]
    assert apply(served, installation, doc(scope(items=rows(*names)))).status == 200
    refused = apply(served, installation, doc(scope(complete=True, items=rows(*names[:10]))))
    assert refused.status == 422, refused.json
    assert refused.error["code"] == "reconcile.retire_limit"
    assert (
        refused.error["details"]["count"] == 10
        and refused.error["details"]["field"] == "allow_retire"
    )
    spec = app_document(__version__, dist=False, enroll=False)
    validator = answer_validator(spec, "/v1/commands/apply", "post", "422")
    assert not [e.message for e in validator.iter_errors(refused.json)], refused.json


def test_the_audit_of_the_server_carries_the_source(
    run_cli: CliRunner, served: ServerApp, installation: dict[str, Any]
) -> None:
    names = [f"p{n:02d}" for n in range(20)]
    apply(served, installation, doc(scope(items=rows(*names))))
    apply(served, installation, doc(scope(complete=True, items=rows(*names[:10]))))  # refused
    found = [e for e in events(installation["root"]) if "source" in e.get("request", {})]
    intent, refusal = found
    assert intent["request"]["source"] == SOURCE and intent["result"] == {"status": "ok"}
    assert refusal["request"]["source"] == SOURCE
    assert refusal["result"]["code"] == "reconcile.retire_limit"
    assert admin(run_cli, "audit", "verify").code == 0


def test_the_client_in_remote_mode_hands_the_document_over(
    run_cli: CliRunner, installation: dict[str, Any], isolated_env: dict[str, str], tmp_path: Path
) -> None:
    run = tmp_path / "run.json"
    run.write_text(json.dumps(doc(scope(complete=True, items=rows("curl", "jq")))))
    with graph_server(isolated_env, tmp_path) as server:
        env = client_env(isolated_env, tmp_path / "client", server.port, installation["token"])
        graph = ("--graph", "example_graph")
        first = parse(run_cli("graph", "apply", str(run), *graph, "--json", env=env))
        assert first.code == 0, first.envelope
        result = first.data["results"][0]["result"]
        assert result["seq"] == 1 and result["reconcile"]["source"] == SOURCE
        again = parse(run_cli("graph", "apply", str(run), *graph, "--json", env=env))
        assert again.data["results"][0]["result"]["seq"] is None
        text = run_cli("graph", "apply", str(run), *graph, env=env)
        assert text.returncode == 0 and "reconcile" in text.stdout
    written = [e for e in events(installation["root"]) if "source" in e.get("request", {})]
    assert [e["request"]["source"] for e in written] == [
        SOURCE
    ]  # the run that wrote nothing has none
