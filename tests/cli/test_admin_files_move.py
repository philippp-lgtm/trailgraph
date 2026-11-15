"""S: ``graph-admin files move`` as a process, against the fake object store (object storage O7).

A local graph with files moves to the object store and back: the dry run first, then the
move, ``audit verify`` and ``files verify --rehash`` clean, ``files gc`` removing the old
copies. ``--all`` skips graphs at the target; text output names the outcome; usage errors
exit 2; a target that is not configured is refused before anything is copied.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.fixtures import ONTOLOGIES
from support.move_scene import add_file, write
from support.s3_graphs import blob_key, blob_keys

from graph_cli.storage.files.store_marker import MARKER_KEY

pytestmark = pytest.mark.slow

NOTES = ONTOLOGIES / "notes_and_sources" / "1.1.0"
CONTENTS = [b"%PDF-1.4\nfirst\n", b"%PDF-1.4\nsecond, longer\n"]


@pytest.fixture
def fake() -> Iterator[FakeS3]:
    with FakeS3() as store:
        yield store


@pytest.fixture
def s3_env(isolated_env: dict[str, str], fake: FakeS3) -> dict[str, str]:
    return {
        **isolated_env,
        "GRAPH_CLI_S3_BUCKET": fake.bucket,
        "AWS_REGION": fake.region,
        "AWS_ENDPOINT_URL_S3": fake.endpoint_url,
        "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
    }


@pytest.fixture
def graph(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES))
    assert created.code == 0, created.envelope
    for number, content in enumerate(CONTENTS):
        add_file(root, None, write(tmp_path, f"file{number}.pdf", content))
    return {"root": root, "id": created.data["id"]}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_a_graph_moves_to_the_object_store_and_back(
    run_cli: CliRunner, s3_env: dict[str, str], fake: FakeS3, graph: dict[str, Any]
) -> None:
    total = {"blobs": 2, "bytes": sum(len(c) for c in CONTENTS)}
    planned = admin(
        run_cli, "files", "move", "example_graph", "--to", "s3", "--dry-run", env=s3_env
    )
    assert planned.code == 0, planned.envelope
    assert planned.data == {
        "to": "s3",
        "dry_run": True,
        "step": None,
        "graphs": [
            {
                "graph_id": graph["id"],
                "name": "example_graph",
                "from": "local",
                "status": "planned",
                "store_id": None,
                "stored": total,
                "copied": {"blobs": 0, "bytes": 0},
                "remaining": total,
                "passes": 0,
            }
        ],
    }
    assert fake.keys() == []

    done = admin(
        run_cli, "files", "move", "example_graph", "--to", "s3", "--reason", "cloud", env=s3_env
    )
    assert done.code == 0, done.envelope
    (row,) = done.data["graphs"]
    assert (row["status"], row["copied"], row["passes"]) == ("moved", total, 1)
    assert sorted(blob_keys(fake.settings())) == sorted(
        blob_key(graph["id"], digest(c)) for c in CONTENTS
    )
    assert fake.get(MARKER_KEY) is not None
    verified = admin(run_cli, "audit", "verify", env=s3_env)
    assert verified.code == 0 and verified.data["counts"]["error"] == 0, verified.envelope
    checked = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash", env=s3_env)
    assert checked.code == 0, checked.envelope
    codes = sorted(f["code"] for f in checked.data["findings"])
    assert codes == ["integrity.storage.blob_misplaced"] * 2
    gc = admin(run_cli, "files", "gc", "--graph", "example_graph", env=s3_env)
    assert gc.code == 0 and len(gc.data["misplaced_removed"]) == 2, gc.envelope

    again = admin(run_cli, "files", "move", "--all", "--to", "s3", env=s3_env)
    assert [r["status"] for r in again.data["graphs"]] == ["at_target"]

    text = run_cli("graph-admin", "files", "move", "example_graph", "--to", "local", env=s3_env)
    assert text.returncode == 0, text.stderr
    assert "moved" in text.stdout
    shown = admin(run_cli, "graph", "show", "example_graph", env=s3_env)
    assert shown.data["store"]["blob_location"] == "local"
    gc = admin(run_cli, "files", "gc", "--graph", "example_graph", env=s3_env)
    assert len(gc.data["misplaced_removed"]) == 2, gc.envelope
    assert blob_keys(fake.settings()) == []
    verified = admin(run_cli, "audit", "verify", env=s3_env)
    assert verified.code == 0 and verified.data["counts"]["error"] == 0, verified.envelope


def test_refusals(run_cli: CliRunner, s3_env: dict[str, str], graph: dict[str, Any]) -> None:
    neither = admin(run_cli, "files", "move", "--to", "s3", env=s3_env)
    assert (neither.code, neither.error["code"]) == (2, "usage.invalid_argument")
    both = admin(run_cli, "files", "move", "example_graph", "--all", "--to", "s3", env=s3_env)
    assert (both.code, both.error["code"]) == (2, "usage.invalid_argument")
    bad = run_cli("graph-admin", "files", "move", "example_graph", "--to", "nowhere", env=s3_env)
    assert bad.returncode == 2
    unconfigured = admin(run_cli, "files", "move", "example_graph", "--to", "s3")
    assert unconfigured.error["code"] == "config.invalid", unconfigured.envelope
    assert unconfigured.data["graphs"] == []
    keyless = {
        k: v
        for k, v in s3_env.items()
        if not k.endswith("_ACCESS_KEY_ID") and not k.endswith("_SECRET_ACCESS_KEY")
    }
    missing = admin(run_cli, "files", "move", "example_graph", "--to", "s3", env=keyless)
    assert missing.error["code"] == "backend.unavailable", missing.envelope
    unknown = admin(run_cli, "files", "move", "no_such_graph", "--to", "s3", env=s3_env)
    assert unknown.error["code"] == "graph.not_found"
