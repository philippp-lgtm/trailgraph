"""S: graph-admin graph check (contracts R.7, B7.14): the batch check of a graph's data."""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.fixtures import ONTOLOGIES

from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog

INVENTORY = ONTOLOGIES / "inventory"

LOCATIONS = [
    {
        "op": "create",
        "type": "inv:Location",
        "local": "room",
        "attrs": {"name": "Room", "code": "R1"},
    },
    {
        "op": "create",
        "type": "inv:Location",
        "local": "shelf",
        "attrs": {"name": "Shelf", "code": "R1-S1"},
        "edges": [{"rel": "inv:within", "target": "@room"}],
    },
]
ITEM = {
    "op": "create",
    "type": "inv:Item",
    "attrs": {"name": "Hammer"},
    "edges": [{"rel": "inv:located_in", "target": "@shelf"}],
}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")
    )
    assert outcome.code == 0, outcome.envelope
    return root


def check(run_cli: CliRunner, *extra: str) -> Outcome:
    return admin(run_cli, "graph", "check", "example_graph", *extra)


def test_clean_data_checks_clean(run_cli: CliRunner, root: Path) -> None:
    write_documents(root, "example_graph", [{"ops": [*LOCATIONS, ITEM]}])
    outcome = check(run_cli)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["findings"] == []
    assert outcome.data["ontology"] == {"bundle": "inventory", "version": "1.1.0"}
    assert outcome.data["pinned"] is True
    assert outcome.envelope["graph"]["seq"] == 1


def test_batch_rules_report_their_warnings(run_cli: CliRunner, root: Path) -> None:
    """A batch warning rule runs on no write; the check reports it and still succeeds."""
    write_documents(root, "example_graph", [{"ops": LOCATIONS}])
    outcome = check(run_cli)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["codes"] == {"data.rule.violated": 1}
    assert outcome.data["counts"]["warning"] == 1
    [finding] = outcome.data["findings"]
    assert finding["rule_id"] == "inv.location_not_empty"


def test_data_errors(run_cli: CliRunner, root: Path) -> None:
    """Under an authored major version: the findings a release would refuse, unreleased."""
    write_documents(root, "example_graph", [{"ops": [*LOCATIONS, ITEM]}])
    before = FileAuditLog(root).head(INSTALLATION_STREAM)
    outcome = check(run_cli, "--ontology", str(INVENTORY / "2.0.0"))
    assert (outcome.code, outcome.error["code"]) == (1, "admin.data_errors")
    details = outcome.error["details"]
    assert details["ontology"] == {"bundle": "inventory", "version": "2.0.0"}
    assert details["codes"] == {
        "data.referential.cardinality_out": 1,
        "data.structural.missing_required": 1,
        "data.structural.unknown_attribute": 1,
    }
    assert FileAuditLog(root).head(INSTALLATION_STREAM) == before
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.1.0"
