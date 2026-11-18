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
    added = app(run_cli, "file", "add", str(home / "report.pdf"), "--no-extract", env=env)
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


def test_verify_gc_and_audit_verify_of_a_graph_in_the_object_store(
    run_cli: CliRunner, root: Path, s3_env: dict[str, str], fake: FakeS3
) -> None:
    """O8 through graph-admin: ``files verify`` lists the graph's prefix once, a local copy
    is misplaced (info) and goes with ``files gc`` once the store holds it, a store that is
    another store is an error finding in both verifies (exit 1), never a secret."""
    graph_id = create(run_cli, s3_env, "example_graph").data["id"]
    home = Path(s3_env["HOME"])
    write_pdf(home / "report.pdf")
    env = {**s3_env, "GRAPH_CLI_GRAPH": "example_graph"}
    added = app(run_cli, "file", "add", str(home / "report.pdf"), "--no-extract", env=env)
    digest = added.data["file"]["hash"]
    copy = root / "graphs" / graph_id / "blobs" / "sha256" / digest[7:9] / digest[7:]
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(synthetic_pdf())
    start = len(fake.requests)
    verify = ("files", "verify", "--graph", "example_graph", "--rehash")
    checked = admin(run_cli, *verify, env=s3_env)
    assert checked.code == 0, checked.envelope
    listings = [r for r in fake.requests[start:] if r.method == "GET" and "list-type" in r.query]
    assert len(listings) == 1
    (finding,) = checked.data["findings"]
    assert finding["code"] == "integrity.storage.blob_misplaced"
    assert finding["target"] == {"path": str(copy.relative_to(root / "graphs" / graph_id))}
    assert checked.data["stored"] == {"files": 1, "bytes": len(synthetic_pdf())}
    gc = admin(run_cli, "files", "gc", "--graph", "example_graph", env=s3_env)
    assert gc.code == 0, gc.envelope
    assert gc.data["misplaced_removed"] == [{"hash": digest, "size": len(synthetic_pdf())}]
    assert gc.data["removed"] is True and not copy.exists()
    assert fake.get(blob_key(graph_id, digest)) == synthetic_pdf()
    verified = admin(run_cli, "audit", "verify", env=s3_env)
    assert verified.code == 0, verified.envelope

    fake.put(
        MARKER_KEY,
        json.dumps(
            {
                "schema": "graph_cli.object_store/1",
                "store_id": "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9",
                "created_at": "x",
            }
        ).encode(),
    )
    for args in (verify, ("audit", "verify")):
        failed = admin(run_cli, *args, env=s3_env)
        assert (failed.code, failed.error["code"]) == (1, "admin.integrity_errors"), args
        codes = [f["code"] for f in failed.error["details"]["findings"]]
        assert codes == ["integrity.storage.blob_store_unavailable"], failed.envelope
        assert "not in that bucket" in failed.error["details"]["findings"][0]["message"]
        assert SECRET_ACCESS_KEY not in str(failed.envelope)
        assert ACCESS_KEY_ID not in str(failed.envelope)


def test_files_store_check(
    run_cli: CliRunner, root: Path, s3_env: dict[str, str], fake: FakeS3
) -> None:
    """``files store check`` answers each check in words and fails on an error; without a
    store it says so and succeeds; no audit event, no probe left, no secret."""
    plain = admin(run_cli, "files", "store", "check")  # no [files.s3], no variables
    assert plain.code == 5 and plain.error["code"] == "config.invalid"  # store = "s3" needs it
    config = Path(s3_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    written = config.read_text()
    config.write_text(written.replace('store = "s3"', 'store = "local"'))
    nothing = admin(run_cli, "files", "store", "check")
    config.write_text(written)
    assert nothing.code == 0, nothing.envelope
    assert nothing.data["configured"] is False
    assert "no object store is configured" in nothing.data["checks"][0]["message"]

    checked = admin(run_cli, "files", "store", "check", env=s3_env)
    assert checked.code == 0, checked.envelope
    statuses = {c["name"]: c["status"] for c in checked.data["checks"]}
    assert statuses["marker"] == "warning"  # no graph yet, so no marker yet
    assert {s for n, s in statuses.items() if n != "marker"} == {"ok"}
    assert [k for k in fake.objects if k.startswith("graph_cli_probe/")] == []
    created = create(run_cli, s3_env, "example_graph")
    checked = admin(run_cli, "files", "store", "check", env=s3_env)
    assert checked.data["graphs"] == [{"id": created.data["id"], "name": "example_graph"}]
    assert checked.data["store_id"] == created.data["store"]["store_id"]
    text = run_cli("graph-admin", "files", "store", "check", env=s3_env)
    assert text.returncode == 0, text.stderr
    assert "missing_key" in text.stdout and "ok" in text.stdout

    fake.without_list_bucket = True
    failed = admin(run_cli, "files", "store", "check", env=s3_env)
    assert (failed.code, failed.error["code"]) == (1, "admin.store_check_failed")
    assert "ListBucket" in failed.error["message"]
    checks = {c["name"]: c for c in failed.error["details"]["checks"]}
    assert checks["missing_key"]["status"] == "error"
    for outcome in (checked, failed):
        assert SECRET_ACCESS_KEY not in str(outcome.envelope)
        assert ACCESS_KEY_ID not in str(outcome.envelope)
    stream = events(root, INSTALLATION_STREAM)
    assert not [e for e in stream if e["request"]["command"] == "files store check"]
