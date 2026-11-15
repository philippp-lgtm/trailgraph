"""S: graphs whose stored files are in an object store, through both programs (object
storage O7, O8), against the fake object store.

``graph-admin graph create`` with ``[files] store = "s3"`` creates the graph local and
switches it in the same command: manifest format 2 naming the store marker's id (created
once per bucket), a ``maint.graph_configure`` intent with ``change.blob_store`` anchored
in the installation stream, ``audit verify`` clean. Then ``graph file add`` stores the
bytes in the bucket and ``graph file path`` answers a read-only copy in this machine's
file cache. A store that refuses the marker stops the command before a graph exists.
"""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.files import synthetic_pdf, write_pdf
from support.fixtures import ONTOLOGIES
from support.s3_graphs import blob_key

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.store_marker import MARKER_KEY

pytestmark = pytest.mark.slow

SOURCE = ONTOLOGIES / "notes_and_sources" / "1.1.0"


@pytest.fixture
def fake() -> Any:
    with FakeS3() as store:
        yield store


@pytest.fixture
def s3_env(isolated_env: dict[str, str], fake: FakeS3) -> dict[str, str]:
    """The isolated env with ``[files] store = "s3"`` and the fake's settings."""
    env = {
        **isolated_env,
        "GRAPH_CLI_S3_BUCKET": fake.bucket,
        "AWS_REGION": fake.region,
        "AWS_ENDPOINT_URL_S3": fake.endpoint_url,
        "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
    }
    return env


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + '\n[files]\nstore = "s3"\n')
    return root


def create(run_cli: CliRunner, env: dict[str, str], name: str) -> Any:
    return admin(run_cli, "graph", "create", name, "--ontology", str(SOURCE), env=env)


def events(root: Path, stream: str) -> list[dict[str, Any]]:
    report = verify_stream(FileAuditLog(root).read_lines(stream), stream=stream)
    assert report.findings == ()
    return [dict(event) for event in report.events]


def blob_gets(fake: FakeS3) -> int:
    return len([c for c in fake.calls("GET") if "/blobs/" in (c.key or "")])


def marker_puts(fake: FakeS3) -> int:
    return len([c for c in fake.calls("PUT") if c.key == MARKER_KEY and c.status == 200])


def test_graph_create_puts_the_graph_into_the_object_store(
    run_cli: CliRunner, root: Path, s3_env: dict[str, str], fake: FakeS3
) -> None:
    created = create(run_cli, s3_env, "example_graph")
    assert created.code == 0, created.envelope
    graph_id = created.data["id"]
    marker = json.loads(fake.get(MARKER_KEY) or b"{}")
    store_id = marker["store_id"]
    assert created.data["store"] == {
        "backend": "files",
        "format_version": 1,
        "blob_location": "s3",
        "store_id": store_id,
    }
    manifest = (root / "graphs" / graph_id / "manifest.toml").read_text()
    assert manifest.startswith('format_version = 2\nrequires = ["blobs.s3"]\n')
    assert f'store_id = "{store_id}"' in manifest
    shown = admin(run_cli, "graph", "show", "example_graph", env=s3_env)
    assert shown.data["store"] == created.data["store"]

    stream = events(root, graph_stream(graph_id))
    assert [e["actions"] for e in stream] == [["maint.graph_create"], ["maint.graph_configure"]]
    switch = stream[1]
    assert switch["change"] == {
        "blob_store": {"from": "local", "to": "s3", "store_id": store_id, "blobs": 0, "bytes": 0}
    }
    assert switch["request"]["command"] == "graph create"
    companion = events(root, INSTALLATION_STREAM)[-1]
    assert companion["actions"] == ["maint.graph_configure"]
    assert companion["anchors"] == [
        {"graph_id": graph_id, "seq": switch["seq"], "hash": switch["hash"]}
    ]

    second = create(run_cli, s3_env, "second_graph")  # the bucket's marker is reused
    assert second.code == 0, second.envelope
    assert second.data["store"]["store_id"] == store_id
    assert marker_puts(fake) == 1

    verified = admin(run_cli, "audit", "verify", env=s3_env)
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_files_of_a_graph_in_the_object_store(
    run_cli: CliRunner, root: Path, s3_env: dict[str, str], fake: FakeS3
) -> None:
    """``file add`` stores the bytes in the bucket; ``file path`` answers a 0400 copy in
    the file cache under the state directory, nothing in the graph's ``blobs/``."""
    graph_id = create(run_cli, s3_env, "example_graph").data["id"]
    home = Path(s3_env["HOME"])
    write_pdf(home / "report.pdf")
    env = {**s3_env, "GRAPH_CLI_GRAPH": "example_graph"}
    added = app(run_cli, "file", "add", str(home / "report.pdf"), env=env)
    assert added.code == 0, added.envelope
    digest = added.data["file"]["hash"]
    assert fake.get(blob_key(graph_id, digest)) == synthetic_pdf()
    blobs = root / "graphs" / graph_id / "blobs"
    assert not blobs.exists() or not any(p.is_file() for p in blobs.rglob("*"))

    shown = app(run_cli, "file", "path", "FILE-0001", env=env)
    assert shown.code == 0, shown.envelope
    copy = Path(shown.data["path"])
    state = Path(s3_env["XDG_STATE_HOME"]) / "graph_cli"
    assert copy == state / "files" / graph_id / digest.removeprefix("sha256:")
    assert copy.read_bytes() == synthetic_pdf()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o400
    gets = blob_gets(fake)
    again = app(run_cli, "file", "path", "FILE-0001", env=env)
    assert again.data["path"] == str(copy)
    assert blob_gets(fake) == gets == 1  # the copy is used again, not fetched
    verified = admin(run_cli, "audit", "verify", env=s3_env)
    assert verified.code == 0, verified.envelope
    checked = admin(run_cli, "files", "verify", "--graph", "example_graph", env=s3_env)
    assert checked.code == 0, checked.envelope


def test_a_store_that_refuses_the_marker_creates_no_graph(
    run_cli: CliRunner, root: Path, s3_env: dict[str, str], fake: FakeS3
) -> None:
    fake.fail(403, "AccessDenied", method="PUT", key=MARKER_KEY)
    refused = create(run_cli, s3_env, "example_graph")
    assert (refused.code, refused.error["code"]) == (1, "files.store_refused"), refused.envelope
    assert admin(run_cli, "graph", "list", env=s3_env).data["items"] == []
    assert SECRET_ACCESS_KEY not in str(refused.envelope)
    unset = {k: v for k, v in s3_env.items() if not k.startswith("AWS_") or "KEY" not in k}
    missing = create(run_cli, unset, "example_graph")
    assert missing.error["code"] == "backend.unavailable", missing.envelope
    assert "AWS_SECRET_ACCESS_KEY" in missing.error["message"]
    assert admin(run_cli, "graph", "list", env=s3_env).data["items"] == []
    created = create(run_cli, s3_env, "example_graph")  # the name is still free
    assert created.code == 0, created.envelope
