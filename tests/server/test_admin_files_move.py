"""S: ``graph-admin files move`` through the admin API and in the remote mode (object storage O7).

In-process: the client's driver (``maintenance_cli.remote_move.drive``) against the served
admin API with no time per request beyond the first copy (``PASS_BUDGET_S`` 0): a dry run,
one pass per file with ``remaining`` counts, then the switch; files added before the switch
make its copies under the lock run out of time, so it switches nothing and the client goes
back to passes. As processes: ``graph-admin`` in remote mode against a ``graph-server``,
both directions, with the 30 s budget.
"""

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, CliRunner
from support.admin import admin, data_root, init
from support.admin_api import command, served
from support.equivalence import home_env
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.fixtures import ONTOLOGIES
from support.move_scene import add_file, write
from support.s3_graphs import blob_key, blob_keys
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY, call, grant_server, new_token

from graph_cli.cli_kit.argv import api_args
from graph_cli.kernel.canonical import JsonValue
from graph_cli.storage.files.store_marker import reset_identity_cache
from graph_cli_admin.maintenance_cli import remote_move
from graph_cli_admin.services_admin import files_move
from graph_cli_server.app import ServerApp

pytestmark = pytest.mark.slow

NOTES = ONTOLOGIES / "notes_and_sources" / "1.1.0"


@pytest.fixture
def fake() -> Iterator[FakeS3]:
    reset_identity_cache()
    with FakeS3() as store:
        yield store
    reset_identity_cache()


def s3_env(env: dict[str, str], fake: FakeS3) -> dict[str, str]:
    return {
        **env,
        "GRAPH_CLI_S3_BUCKET": fake.bucket,
        "AWS_REGION": fake.region,
        "AWS_ENDPOINT_URL_S3": fake.endpoint_url,
        "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
    }


def app_add(app: ServerApp, token: str, content: bytes, name: str) -> None:
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=content).json
    added = call(
        app,
        "POST",
        "/v1/commands/file/add",
        token=token,
        body={
            "args": {
                "path": {"upload": uploaded["upload"], "name": name},
                "--graph": "example_graph",
            }
        },
    )
    assert added.status == 200, added.json


@pytest.fixture
def api(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, fake: FakeS3
) -> dict[str, Any]:
    init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES))
    assert created.code == 0, created.envelope
    grant_server(run_cli, tmp_path)
    tokens = {
        "ci": new_token(run_cli, "ci"),
        "root": new_token(run_cli, "root", "--scope", "admin"),
    }
    env = s3_env(isolated_env, fake)
    app = served({"env": env}, tmp_path)
    for number in range(2):
        app_add(app, tokens["ci"], f"%PDF-1.4\nfile {number}\n".encode(), f"file{number}.pdf")
    return {"app": app, "tokens": tokens, "graph": created.data["id"]}


def sender(
    app: ServerApp, token: str, before: Callable[[dict[str, JsonValue]], None] | None = None
) -> tuple[remote_move.Send, list[str]]:
    steps: list[str] = []

    def send(args: dict[str, JsonValue], write: bool) -> Any:
        step = str(args.get("--step") or ("dry run" if args.get("--dry-run") else "?"))
        assert write is (step != "dry run")
        steps.append(step)
        if before is not None:
            before(args)
        return command(app, "files/move", token, args=api_args(args)).json

    return send, steps


def test_the_client_repeats_passes_until_nothing_remains_then_switches(
    api: dict[str, Any], fake: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root, ci = api["app"], api["tokens"]["root"], api["tokens"]["ci"]
    monkeypatch.setattr(files_move, "PASS_BUDGET_S", 0.0)
    send, steps = sender(app, root)
    result = remote_move.drive(send, {"ref": ["example_graph"], "--to": "s3", "--reason": "x"})
    assert result.forwarded is not None and result.error is None
    document = result.forwarded.document
    assert steps == ["dry run", "pass", "pass", "switch"]
    assert document["error"] is None
    (row,) = document["data"]["graphs"]
    assert (row["status"], row["from"], row["passes"]) == ("moved", "local", 2)
    assert row["copied"]["blobs"] == 2 and row["remaining"]["blobs"] == 0
    assert document["data"]["step"] is None and document["data"]["dry_run"] is False
    assert len(blob_keys(fake.settings())) == 2

    # Two files arrive right before the switch: its copies under the lock run out of time
    # after the first, so it switches nothing; the client runs passes and switches again.
    arrived: list[int] = []

    def arrive(args: dict[str, JsonValue]) -> None:
        if args.get("--step") == "switch" and not arrived:
            arrived.append(1)
            for number in (2, 3):
                app_add(app, ci, f"%PDF-1.4\nfile {number}\n".encode(), f"file{number}.pdf")

    send, steps = sender(app, root, arrive)
    back = remote_move.drive(send, {"ref": ["example_graph"], "--to": "local"})
    assert back.forwarded is not None
    # the first two files are still in the data root (no gc ran): the first pass copies none
    assert steps == ["dry run", "pass", "switch", "pass", "switch"]
    (row,) = back.forwarded.document["data"]["graphs"]
    assert (row["status"], row["from"], row["copied"]["blobs"]) == ("moved", "s3", 2)
    assert row["stored"]["blobs"] == 4
    verified = command(app, "audit/verify", root)
    assert verified.status == 200 and verified.json["data"]["counts"]["error"] == 0
    checked = command(app, "files/verify", root, args={"--graph": "example_graph"})
    assert checked.status == 200, checked.json
    again = remote_move.drive(sender(app, root)[0], {"--all": True, "--to": "local"})
    assert again.forwarded is not None
    assert [r["status"] for r in again.forwarded.document["data"]["graphs"]] == ["at_target"]


def test_a_refused_step_ends_the_move_with_its_error(
    api: dict[str, Any], fake: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = api["app"], api["tokens"]["root"]
    monkeypatch.setattr(files_move, "PASS_BUDGET_S", 0.0)
    refused = b"%PDF-1.4\nfile 1\n"
    key = blob_key(api["graph"], "sha256:" + hashlib.sha256(refused).hexdigest())
    fake.fail(403, "AccessDenied", method="PUT", key=key, times=10)
    send, steps = sender(app, root)
    result = remote_move.drive(send, {"ref": ["example_graph"], "--to": "s3"})
    assert result.forwarded is not None
    document = result.forwarded.document
    assert document["error"]["code"] == "files.store_refused"
    assert document["data"]["graphs"] == []
    assert steps[0] == "dry run" and "switch" not in steps
    assert key not in sorted(fake.keys())
    shown = command(app, "graph/show", root, args={"ref": "example_graph"})
    assert shown.json["data"]["store"]["blob_location"] == "local"


def test_unconfigured_target_is_refused_by_the_dry_run(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    assert admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES)).code == 0
    grant_server(run_cli, tmp_path)
    root = new_token(run_cli, "root", "--scope", "admin")
    app = served({"env": isolated_env}, tmp_path)
    send, steps = sender(app, root)
    result = remote_move.drive(send, {"ref": ["example_graph"], "--to": "s3"})
    assert result.forwarded is not None
    assert result.forwarded.document["error"]["code"] == "config.invalid"
    assert steps == ["dry run"]


# ---- graph-admin in remote mode -------------------------------------------------------------


def admin_cli(env: dict[str, str], cwd: Path, *args: str, json_mode: bool = True) -> Any:
    result = subprocess.run(
        [str(BIN_DIR / "graph-admin"), *args, *(["--json"] if json_mode else [])],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if not json_mode:
        return result.returncode, result.stdout + result.stderr
    return result.returncode, json.loads(result.stdout)


def test_files_move_in_remote_mode(
    isolated_env: dict[str, str], tmp_path: Path, fake: FakeS3
) -> None:
    owner = home_env(isolated_env, tmp_path / "server")
    policy = tmp_path / "server_policy.yaml"
    policy.write_text(SERVER_POLICY)
    root = data_root(owner)
    token = ""
    for args in (
        ("init",),
        ("graph", "create", "example_graph", "--ontology", str(NOTES)),
        None,  # files, before the server policy (decisions are verified against it)
        ("policy", "apply", str(policy), "--reason", "serve"),
        ("token", "create", "root", "--scope", "admin"),
    ):
        if args is None:
            for number in range(3):
                path = write(tmp_path, f"file{number}.pdf", f"%PDF-1.4\nfile {number}\n".encode())
                add_file(root, None, path)
            continue
        code, document = admin_cli(owner, tmp_path, *args)
        assert code == 0, document
        token = document["data"].get("token", token)
    config = Path(owner["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "[server]\nadmin_api = true\n")
    with graph_server(s3_env(owner, fake), tmp_path) as server:
        env = client_env(
            isolated_env,
            tmp_path / "client",
            server.port,
            None,
            extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n',
        )
        env["GRAPH_CLI_ADMIN_TOKEN"] = token
        code, planned = admin_cli(
            env, tmp_path, "files", "move", "example_graph", "--to", "s3", "--dry-run"
        )
        assert code == 0, planned
        assert planned["data"]["graphs"][0]["status"] == "planned"
        assert blob_keys(fake.settings()) == []
        code, done = admin_cli(env, tmp_path, "files", "move", "--all", "--to", "s3")
        assert code == 0, done
        (row,) = done["data"]["graphs"]
        assert (row["status"], row["copied"]["blobs"], row["passes"]) == ("moved", 3, 1)
        assert len(blob_keys(fake.settings())) == 3
        code, verified = admin_cli(env, tmp_path, "audit", "verify")
        assert code == 0 and verified["data"]["counts"]["error"] == 0, verified
        code, text = admin_cli(
            env, tmp_path, "files", "move", "example_graph", "--to", "local", json_mode=False
        )
        assert code == 0, text
        assert "moved" in text
        code, shown = admin_cli(env, tmp_path, "graph", "show", "example_graph")
        assert shown["data"]["store"]["blob_location"] == "local"
        code, usage = admin_cli(env, tmp_path, "files", "move", "--to", "s3")
        assert code == 2 and usage["error"]["code"] == "usage.invalid_argument"
