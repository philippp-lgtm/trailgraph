"""``graph-admin audit verify`` reads the migration marker of every graph (plan M3, M5).

The full migration sequences are in ``test_migration_verify.py`` (storage API); these run the
real command on a graph whose marker names no committed tx, or is damaged: neither is a finding.
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph

from graph_cli.kernel.canonical import pretty_bytes


@pytest.fixture
def graph_dir(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph")
    graph_id = admin(run_cli, "graph", "show", "example_graph").data["id"]
    return root / "graphs" / graph_id


def verify_codes(run_cli: CliRunner) -> list[str]:
    outcome = admin(run_cli, "audit", "verify")
    assert outcome.code == 0, outcome.envelope
    return [f["code"] for f in outcome.data["findings"]]


def test_a_marker_whose_tx_was_never_committed_is_no_finding(
    run_cli: CliRunner, graph_dir: Path
) -> None:
    marker = {
        "schema": "graph_cli.migration_marker/1",
        "seq": 5,
        "tx_id": "0192f6d4-2a10-7b41-9c3d-5e6f70819a2b",
        "from": "1.0.0",
        "to": "2.0.0",
        "artifact_hash": "sha256:" + "a" * 64,
        "migration_hash": "sha256:" + "b" * 64,
    }
    (graph_dir / "data" / "migration.json").write_bytes(pretty_bytes(marker))
    assert verify_codes(run_cli) == []


def test_a_damaged_marker_does_not_stop_the_verification(
    run_cli: CliRunner, graph_dir: Path
) -> None:
    (graph_dir / "data" / "migration.json").write_bytes(b"not json\n")
    assert verify_codes(run_cli) == []
