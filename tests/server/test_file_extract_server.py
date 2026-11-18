"""C: derived file metadata over the API (file metadata plan M12, step 6).

``file add`` through ``/v1/commands/file/add`` extracts on the server, which holds the bytes, as
the token that added the file: the token ``intake`` may store files and read and write their
metadata, and sees nothing else (not the ``nas:Scan`` files, their types or counts); it gets the
metadata of its own file and no trace of the hidden one. ``files extract`` is an admin API route:
an app token cannot reach it, an admin token runs it and the answer fits the OpenAPI document.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.admin_api import command as admin_command
from support.app import ENV, app, create_graph
from support.files import synthetic_pdf
from support.openapi import admin_document, answer_validator
from support.policies import write_policy
from support.server import call, grant_server, new_token

from graph_cli_server.admin_api import AdminApi
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

HIDE_SCANS = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: hide_scans, action: [read.get], type: [nas:Scan]}
"""
GRAPH = "example_graph"


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, GRAPH, "notes_and_sources", version="1.3.0")
    grant_server(run_cli, tmp_path)
    scan = tmp_path / "hidden.pdf"
    scan.write_bytes(synthetic_pdf("hidden"))
    hidden = app(run_cli, *("file", "add", "--no-origin", "--type", "nas:Scan", str(scan)), env=ENV)
    assert hidden.code == 0, hidden.envelope
    policy = write_policy(tmp_path / "policies", "graph.yaml", HIDE_SCANS)
    applied = admin(run_cli, "policy", "apply", str(policy), "--graph", GRAPH, "--reason", "hide")
    assert applied.code == 0, applied.envelope
    tokens = {
        "intake": new_token(run_cli, "intake"),
        "root": new_token(run_cli, "root", "--scope", "admin"),
    }
    served = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), admin=AdminApi)
    return {"app": served, "tokens": tokens, "root": root, "hidden": hidden, "tmp": tmp_path}


def add(world: dict[str, Any], token: str, content: bytes, name: str, *flags: str) -> Any:
    uploaded = call(world["app"], "PUT", "/v1/uploads", token=token, raw=content)
    assert uploaded.status == 200, uploaded.json
    reference = {"upload": uploaded.json["upload"], "name": name}
    args: dict[str, Any] = {"path": reference, "--graph": GRAPH}
    args.update({flag: True for flag in flags})
    return call(world["app"], "POST", "/v1/commands/file/add", token=token, body={"args": args})


def test_file_add_extracts_on_the_server_as_the_token(world: dict[str, Any]) -> None:
    hidden = world["hidden"]
    added = add(world, world["tokens"]["intake"], synthetic_pdf("visible"), "report.pdf")
    assert added.status == 200, added.json
    metadata = added.json["data"]["metadata"]
    assert [(m["extractor"], m["status"]) for m in metadata] == [("format", "ok"), ("pdf", "ok")]
    shown = call(
        world["app"],
        "POST",
        "/v1/commands/get",
        token=world["tokens"]["intake"],
        body={"args": {"ref": metadata[1]["id"], "--graph": GRAPH}},
    )
    assert shown.status == 200 and shown.json["data"]["entity"]["attrs"]["page_count"] == 1
    # the records of the hidden file exist (the local app extracted them) and the answer names none
    hidden_records = {m["id"] for m in hidden.data["metadata"]}
    assert hidden_records and hidden_records.isdisjoint({m["id"] for m in metadata})
    assert "Scan" not in json.dumps(added.json)
    # the history names the extractor as source (no run: an add is no named run)
    history = call(
        world["app"],
        "POST",
        "/v1/commands/history",
        token=world["tokens"]["intake"],
        body={"args": {"ref": metadata[1]["id"], "--graph": GRAPH}},
    )
    (item,) = history.json["data"]["items"]
    assert item["source"] == {"name": "pdf", "version": "1", "kind": "extractor"}
    assert item["command"] == "file add" and "run" not in item["source"]


def test_no_extract_over_the_api_stores_the_file_alone(world: dict[str, Any]) -> None:
    added = add(world, world["tokens"]["intake"], synthetic_pdf("alone"), "a.pdf", "--no-extract")
    assert added.status == 200 and added.json["data"]["metadata"] == []


def test_files_extract_is_an_admin_route_an_app_token_cannot_reach(world: dict[str, Any]) -> None:
    refused = admin_command(
        world["app"],
        "files/extract",
        world["tokens"]["intake"],
        args={"--graph": GRAPH, "--dry-run": True},
    )
    assert refused.status == 403 and refused.error["code"] == "remote.unauthorized"


def test_an_admin_token_runs_it_and_the_answer_fits_the_document(world: dict[str, Any]) -> None:
    add(world, world["tokens"]["intake"], synthetic_pdf("one"), "one.pdf", "--no-extract")
    root = world["tokens"]["root"]
    dry = admin_command(
        world["app"], "files/extract", root, args={"--graph": GRAPH, "--dry-run": True}
    )
    assert dry.status == 200, dry.json
    assert dry.json["data"]["dry_run"] is True and dry.json["data"]["files"] == 1
    done = admin_command(world["app"], "files/extract", root, args={"--graph": GRAPH})
    assert done.status == 200, done.json
    pdf = next(t for t in done.json["data"]["extractors"] if t["name"] == "pdf")
    assert pdf["created"] == 1
    again = admin_command(world["app"], "files/extract", root, args={"--graph": GRAPH})
    assert again.json["graph"]["seq"] is None
    listed = admin_command(world["app"], "files/extractors", root, args={})
    assert [e["name"] for e in listed.json["data"]["extractors"]][:2] == ["format", "pdf"]
    spec = admin_document()
    for path, reply in (
        ("/v1/admin/commands/files/extract", done),
        ("/v1/admin/commands/files/extractors", listed),
    ):
        validator = answer_validator(spec, path, "post", "200")
        assert not [e.message for e in validator.iter_errors(reply.json)], reply.json
