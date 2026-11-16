"""S: search answers for a match in an attribute without a label role (output schema, B9)."""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES

from graph_cli.kernel.changeset import read_documents

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    seeds = read_documents((FIXTURES / "seeds" / "inventory" / "changes.jsonl").read_bytes())
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    return root


def test_search_in_a_field_without_a_label_role_has_a_null_role(
    run_cli: CliRunner, root: Path
) -> None:
    """The match carries ``role: null``; the output schema allows it (lookup keeps its enum)."""
    for args in (("--field", "serial_number"), ()):
        outcome = app(run_cli, "search", "SN-3001", *args, env=ENV)
        assert outcome.code == 0, outcome.envelope
        (item,) = outcome.data["items"]
        assert item["match"]["attr"] == "serial_number"
        assert item["match"]["role"] is None
        assert "serial_number" in item["fields"]
    text = run_cli("graph", "search", "SN-3001", "--field", "serial_number", env=ENV)
    assert text.returncode == 0 and text.stderr == ""
