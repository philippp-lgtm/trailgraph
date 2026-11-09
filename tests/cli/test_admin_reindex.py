"""S: ``graph-admin graph reindex`` and the disposable index through both programs.

A seeded graph has an index after its writes; reads through ``graph`` use
it. Reindex rebuilds every derived file as a ``maint.reindex`` event pair
and ``audit verify`` is clean afterwards; an edited index is a verify
finding that reindex repairs.
"""

import json
import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph
from support.fixtures import FIXTURES
from support.trees import tree_hash

from graph_cli.kernel.ids import GraphId
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow

SEED = FIXTURES / "seeds" / "notes_and_sources" / "changes.jsonl"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    applied = app(run_cli, "apply", str(SEED), env=ENV)
    assert applied.code == 0, applied.envelope
    return root


def store_of(root: Path) -> FileStore:
    (graph,) = list((root / "graphs").iterdir())
    return FileStore(graph, GraphId(UUID(graph.name)))


def found(run_cli: CliRunner) -> list[str]:
    outcome = app(run_cli, "find", "--include-inactive", "--limit", "500", env=ENV)
    assert outcome.code == 0, outcome.envelope
    assert outcome.warnings == []
    return [item["id"] for item in outcome.data["items"]]


def test_reindex_rebuilds_every_derived_file(run_cli: CliRunner, root: Path) -> None:
    store = store_of(root)
    assert store.layout.index.exists()
    before = found(run_cli)
    revisions = tree_hash(store.layout.entities)

    dry = admin(run_cli, "graph", "reindex", "example_graph", "--dry-run")
    assert dry.code == 0, dry.envelope
    assert (dry.data["dry_run"], dry.data["rebuilt"]) == (True, False)
    assert dry.data["seq"] == store.head().seq

    shutil.rmtree(store.layout.index.parent)
    done = admin(run_cli, "graph", "reindex", "example_graph", "--reason", "rebuild")
    assert done.code == 0, done.envelope
    assert done.data["rebuilt"] is True
    assert store.layout.index.exists()
    assert tree_hash(store.layout.entities) == revisions
    assert json.loads(store.layout.head.read_text())["seq"] == dry.data["seq"]
    assert found(run_cli) == before

    stream = store.layout.audit.glob("events-*.jsonl")
    events = [json.loads(line) for path in stream for line in path.read_text().splitlines()]
    reindexed = [e for e in events if e["actions"] == ["maint.reindex"]]
    assert [e["change"] for e in reindexed] == [{"reindex": {"seq": dry.data["seq"]}}]
    audit = admin(run_cli, "audit", "verify")
    assert audit.code == 0, audit.envelope
    assert audit.data["counts"]["error"] == 0


def test_an_edited_index_is_found_and_repaired(run_cli: CliRunner, root: Path) -> None:
    store = store_of(root)
    with sqlite3.connect(store.layout.index) as db:
        db.execute("DELETE FROM entity WHERE id = (SELECT min(id) FROM entity)")
    audit = admin(run_cli, "audit", "verify")
    assert audit.code == 1
    assert "integrity.storage.index_inconsistent" in [
        f["code"] for f in audit.error["details"]["findings"]
    ]
    done = admin(run_cli, "graph", "reindex", "example_graph", "--reason", "repair")
    assert done.code == 0, done.envelope
    assert admin(run_cli, "audit", "verify").code == 0


def test_an_unusable_index_warns_and_reads_replay(run_cli: CliRunner, root: Path) -> None:
    store = store_of(root)
    before = found(run_cli)
    store.layout.index.write_bytes(b"not a database")
    outcome = app(run_cli, "find", "--include-inactive", "--limit", "500", env=ENV)
    assert outcome.code == 0, outcome.envelope
    assert [w["code"] for w in outcome.envelope["warnings"]] == ["storage.index_stale"]
    assert [item["id"] for item in outcome.data["items"]] == before
