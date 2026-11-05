"""S: ``graph-admin graph purge`` through both programs (plan T16, step 6).

A sealed graph gets an entity through ``graph``; the purge erases it, ``graph
get`` and ``graph history`` show it shredded, and ``audit verify`` stays clean
and counts what was erased.
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import ENV, app, create_graph

pytestmark = pytest.mark.slow

LABEL = "LABELMARK0d6f"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    return root


def test_a_purged_entity_reads_shredded(run_cli: CliRunner, root: Path) -> None:
    created = app(
        run_cli, "create", "nas:Topic", "--set", f"label={LABEL}", "--reason", "why", env=ENV
    )
    assert created.code == 0, created.envelope
    entity = created.data["id"]

    missing = admin(run_cli, "graph", "purge", "example_graph", entity)
    assert missing.code != 0  # --reason is required
    dry = admin(
        run_cli, "graph", "purge", "example_graph", entity, "--reason", "ticket 1", "--dry-run"
    )
    assert dry.code == 0, dry.envelope
    assert (dry.data["dry_run"], dry.data["purged"], dry.data["retracted"]) == (
        True,
        False,
        [entity],
    )
    done = admin(run_cli, "graph", "purge", "example_graph", entity, "--reason", "ticket 1")
    assert done.code == 0, done.envelope
    assert done.data["purged"] and done.data["keys"] == {
        "entity": 1,
        "tx": 2,
        "file": 0,
        "pending": 3,
    }

    got = app(run_cli, "get", entity, env=ENV)
    assert got.code == 0, got.envelope
    assert got.data["entity"]["shredded"] is True and got.data["entity"]["attrs"] == {}
    history = app(run_cli, "history", entity, env=ENV)
    assert [item.get("shredded") for item in history.data["items"]] == [True, True]
    for path in root.rglob("*"):
        if path.is_file():
            assert LABEL.encode() not in path.read_bytes(), path

    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    (graph,) = verified.data["graphs"]
    assert graph["shredded"] == {"txs": 2, "revisions": 2}
