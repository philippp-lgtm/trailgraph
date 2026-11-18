"""C: the download route ``GET /v1/graphs/{graph}/files/{ref}`` for a graph whose stored
files are in the fake object store (object storage O8).

The bytes stream from ``FileService.open``, verified while they stream; the server writes
no copy. ``file path`` over the API answers ``path: null`` without reading the bytes. A
missing object, or a single-chunk file whose bytes differ from their hash, is an error
answer before the first byte; a larger file whose bytes turn out wrong at the end is cut
off: fewer bytes than ``Content-Length``, and a line in the server log.
"""

import hashlib
import io
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.files import synthetic_pdf
from support.fixtures import ONTOLOGIES
from support.s3_graphs import blob_key
from support.server import call, grant_server, new_token

from graph_cli.kernel.canonical import hash_bytes
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow


@pytest.fixture
def fake() -> Iterator[FakeS3]:
    with FakeS3() as store:
        yield store


@pytest.fixture
def served(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, fake: FakeS3
) -> Iterator[dict[str, Any]]:
    root = init(run_cli, isolated_env)
    grant_server(run_cli, tmp_path)
    token = new_token(run_cli, "ci")
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + '\n[files]\nstore = "s3"\n')
    env = {
        **isolated_env,
        "GRAPH_CLI_S3_BUCKET": fake.bucket,
        "AWS_REGION": fake.region,
        "AWS_ENDPOINT_URL_S3": fake.endpoint_url,
        "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
    }
    source = str(ONTOLOGIES / "notes_and_sources" / "1.1.0")
    created = admin(run_cli, "graph", "create", "files_graph", "--ontology", source, env=env)
    assert created.code == 0, created.envelope
    assert created.data["store"]["blob_location"] == "s3"
    log = io.StringIO()
    app = ServerApp(Settings.load(env, tmp_path), log=log)
    yield {"app": app, "log": log, "token": token, "root": root, "graph": created.data["id"]}


def add_file(served: dict[str, Any], content: bytes, name: str) -> str:
    app, token = served["app"], served["token"]
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=content).json
    reference = {"upload": uploaded["upload"], "name": name}
    added = call(
        app,
        "POST",
        "/v1/commands/file/add",
        token=token,
        body={"args": {"path": reference, "--graph": "files_graph", "--no-extract": True}},
    )
    assert added.status == 200, added.json
    entity: str = added.json["data"]["entity"]["id"]
    return entity


def blob_gets(fake: FakeS3) -> int:
    return len([c for c in fake.calls("GET") if "/blobs/" in (c.key or "")])


def test_the_download_streams_from_the_object_store(served: dict[str, Any], fake: FakeS3) -> None:
    app, token = served["app"], served["token"]
    content = synthetic_pdf()
    entity = add_file(served, content, "report.pdf")
    shown = call(
        app,
        "POST",
        "/v1/commands/file/path",
        token=token,
        body={"args": {"ref": entity, "--graph": "files_graph"}},
    )
    assert shown.status == 200 and shown.json["data"]["path"] is None
    assert shown.json["data"]["hash"] == hash_bytes(content)
    assert blob_gets(fake) == 0  # file path reads no bytes
    route = f"/v1/graphs/files_graph/files/{entity}"
    download = call(app, "GET", route, token=token)
    assert download.status == 200, download.body[:200]
    assert download.body == content
    assert download.headers["Content-Length"] == str(len(content))
    assert download.headers["Graph-Cli-Content-Hash"] == hash_bytes(content)
    assert download.headers["Graph-Cli-Media-Type"] == "application/pdf"
    assert download.headers["Content-Type"] == "application/octet-stream"
    tag = f'"{hash_bytes(content)}"'
    assert download.headers["ETag"] == tag
    gets = blob_gets(fake)
    same = call(app, "GET", route, token=token, headers={"If-None-Match": tag})
    assert same.status == 304 and same.body == b""
    assert blob_gets(fake) == gets  # a 304 starts no read
    state = Path(app.settings.env["XDG_STATE_HOME"])
    assert not (state / "graph_cli" / "files").exists()  # the server keeps no copy
    graph_dir = served["root"] / "graphs" / served["graph"]
    assert not any(p.is_file() for p in (graph_dir / "blobs").rglob("*"))


def test_failures_before_the_first_byte_are_error_answers(
    served: dict[str, Any], fake: FakeS3
) -> None:
    app, token = served["app"], served["token"]
    content = synthetic_pdf()
    entity = add_file(served, content, "report.pdf")
    key = blob_key(served["graph"], hash_bytes(content))
    route = f"/v1/graphs/files_graph/files/{entity}"
    fake.corrupt_get(key, b"x" * len(content))  # one chunk: the mismatch shows at once
    corrupt = call(app, "GET", route, token=token)
    assert corrupt.error["code"] == "backend.corrupt", corrupt.body[:300]
    assert corrupt.status >= 500 and "Graph-Cli-Content-Hash" not in corrupt.headers
    del fake.objects[key]  # the fake is idle between requests
    missing = call(app, "GET", route, token=token)
    assert missing.error["code"] == "backend.corrupt", missing.body[:300]
    assert "files verify" in missing.error["message"]
    fake.fail(503, "SlowDown", times=10, method="HEAD")
    unavailable = call(app, "GET", route, token=token)
    assert unavailable.error["code"] == "backend.unavailable", unavailable.body[:300]


def test_a_failure_after_the_first_byte_cuts_the_body_off(
    served: dict[str, Any], fake: FakeS3
) -> None:
    app, token, log = served["app"], served["token"], served["log"]
    content = os.urandom(2 * 1024 * 1024 + 4096)  # more than one chunk of the client
    entity = add_file(served, content, "data.bin")
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    # flip the last byte: a fixed replacement equals it in 1 of 256 random contents
    fake.corrupt_get(blob_key(served["graph"], digest), content[:-1] + bytes([content[-1] ^ 0xFF]))
    route = f"/v1/graphs/files_graph/files/{entity}"
    cut = call(app, "GET", route, token=token)
    assert cut.status == 200  # the headers were sent before the bytes turned out wrong
    assert int(cut.headers["Content-Length"]) == len(content)
    assert 0 < len(cut.body) < len(content)
    assert content.startswith(cut.body)
    assert f"GET /v1/graphs/files_graph/files/{entity} cut off: backend.corrupt" in log.getvalue()
    assert "Traceback" not in log.getvalue()
