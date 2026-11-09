"""S: ``graph-admin files verify|gc`` (files slice 1, step 7).

An orphan as KB2 leaves it (stored, no tx) is removed by gc, a young staging
file stays and an old one goes; the removal is a ``maint.repair`` event pair
and ``audit verify`` is clean afterwards. Verify findings and exit codes for
missing and damaged files; ``audit verify`` sees a missing file too.
"""

import json
import os
import time
from pathlib import Path
from uuid import UUID

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph
from support.files import synthetic_pdf, write_pdf
from support.trees import tree_hash

from graph_cli.kernel.ids import GraphId
from graph_cli.storage.common.crypto import BLOB_HEADER_BYTES, TAG_BYTES
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow

ORPHAN = b"bytes that no tx names\n"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    path = write_pdf(Path(isolated_env["HOME"]) / "a.pdf")
    added = app(run_cli, "file", "add", str(path), env=ENV)
    assert added.code == 0, added.envelope
    return root


def store_of(root: Path) -> FileStore:
    (graph,) = list((root / "graphs").iterdir())
    return FileStore(graph, GraphId(UUID(graph.name)))


def stored(store: FileStore) -> list[Path]:
    return sorted(store.layout.blobs.glob("sha256/*/*"))


def add_orphan(store: FileStore) -> None:
    staged = store.stage_blob([ORPHAN], max_bytes=100)
    with store.session(timeout_s=5) as session:
        session.publish_blob(staged)


def test_verify_a_clean_graph(run_cli: CliRunner, root: Path) -> None:
    outcome = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash", "--decrypt")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert outcome.data["decrypt"] is True
    assert outcome.data["stored"]["files"] == 1
    # the graph is sealed: the stored copy is the header, the content and one tag
    sealed = BLOB_HEADER_BYTES + len(synthetic_pdf()) + TAG_BYTES
    assert outcome.data["stored"]["bytes"] == sealed


def test_gc_removes_orphans_and_old_staging(run_cli: CliRunner, root: Path) -> None:
    store = store_of(root)
    add_orphan(store)
    (store.layout.blobs / ".tmp-young").write_bytes(ORPHAN)
    old = store.layout.blobs / ".tmp-old"
    old.write_bytes(ORPHAN)
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    verified = admin(run_cli, "files", "verify", "--graph", "example_graph")
    assert verified.code == 0
    assert sorted(f["code"] for f in verified.data["findings"]) == [
        "integrity.storage.blob_orphan",
        "integrity.storage.blob_staging_leftover",
        "integrity.storage.blob_staging_leftover",
    ]

    before = tree_hash(root)
    dry = admin(run_cli, "files", "gc", "--graph", "example_graph", "--dry-run")
    assert dry.code == 0, dry.envelope
    assert [b["size"] for b in dry.data["blobs_removed"]] == [len(ORPHAN)]
    assert dry.data["staging_removed"] == [".tmp-old"]
    assert dry.data["removed"] is False
    assert tree_hash(root) == before

    done = admin(run_cli, "files", "gc", "--graph", "example_graph", "--reason", "cleanup")
    assert done.code == 0, done.envelope
    assert done.data["removed"] is True
    assert len(stored(store)) == 1  # the referenced file stays
    assert stored(store)[0].read_bytes().startswith(b"GCBLOB")  # sealed: the encrypted copy
    assert sorted(p.name for p in store.layout.blobs.iterdir()) == [".tmp-young", "sha256"]

    stream = store.layout.audit.glob("events-*.jsonl")
    events = [json.loads(line) for path in stream for line in path.read_text().splitlines()]
    repair = [e for e in events if e["actions"] == ["maint.repair"]]
    assert [e["change"] for e in repair] == [
        {"blobs_removed": dry.data["blobs_removed"], "staging_removed": 1}
    ]
    audit = admin(run_cli, "audit", "verify")
    assert audit.code == 0, audit.envelope
    assert audit.data["counts"]["error"] == 0

    again = admin(run_cli, "files", "gc", "--graph", "example_graph")
    assert (again.code, again.data["removed"]) == (0, False)  # nothing to remove, no event


def test_missing_and_damaged_files_fail_verify(run_cli: CliRunner, root: Path) -> None:
    store = store_of(root)
    (blob,) = stored(store)
    blob.chmod(0o644)
    blob.write_bytes(b"%PDF-damaged")
    plain = admin(run_cli, "files", "verify", "--graph", "example_graph")
    assert plain.code == 0
    rehashed = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash")
    assert (rehashed.code, rehashed.error["code"]) == (1, "admin.integrity_errors")
    codes = [f["code"] for f in rehashed.error["details"]["findings"]]
    assert codes == ["integrity.storage.blob_hash_mismatch"]

    blob.unlink()
    missing = admin(run_cli, "files", "verify", "--graph", "example_graph")
    assert (missing.code, missing.error["code"]) == (1, "admin.integrity_errors")
    assert [f["code"] for f in missing.error["details"]["findings"]] == [
        "integrity.storage.blob_missing"
    ]
    audit = admin(run_cli, "audit", "verify")
    assert audit.code == 1
    assert "integrity.storage.blob_missing" in [
        f["code"] for f in audit.error["details"]["findings"]
    ]
    gone = app(run_cli, "file", "path", "FILE-0001", env=ENV)
    assert (gone.code, gone.error["code"]) == (5, "backend.corrupt")
