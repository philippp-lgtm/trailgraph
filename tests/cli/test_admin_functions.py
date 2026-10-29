"""S: graph-admin graph functions publish|show (K6, K7; B6 ``maint.graph_configure``, W.e).

The publish event pair (graph intent plus installation anchor) verifies
clean, a re-publish of the same files changes nothing, an empty folder
unpublishes every function, and every problem of every function is reported
in one answer.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES, ONTOLOGIES
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.ports.catalog import functions_hash
from graph_cli.storage.files.audit_log import FileAuditLog

FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
BROKEN = FIXTURES / "functions" / "inventory" / "broken"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    return create_graph(run_cli, "example_graph")


def publish(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "functions", "publish", "example_graph", str(source), *args)


def show(run_cli: CliRunner) -> Outcome:
    return admin(run_cli, "graph", "functions", "show", "example_graph")


def folder(tmp_path: Path, *names: str, name: str = "functions") -> Path:
    """A functions folder with the named valid files (none: an empty folder)."""
    target = tmp_path / "work" / name
    target.mkdir(parents=True, exist_ok=True)
    for file_name in names:
        shutil.copy(FUNCTIONS / file_name, target)
    return target


def sources_hash(directory: Path) -> str:
    return functions_hash({p.name: p.read_bytes() for p in directory.glob("*.yaml")})


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def installation_events(root: Path) -> list[dict[str, Any]]:
    log = FileAuditLog(root).read_lines(INSTALLATION_STREAM)
    report = verify_stream(log, stream=INSTALLATION_STREAM)
    assert report.findings == ()
    return [dict(event) for event in report.events]


def test_publish_records_the_change_and_verifies_clean(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["functions"] is None
    assert show(run_cli).data == {"graph_id": graph_id, "slot": None, "functions": []}
    outcome = publish(run_cli, FUNCTIONS, "--reason", "first functions")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert (data["published"], data["dry_run"], data["from"]) == (True, False, None)
    assert data["functions"] == ["items_by_condition", "items_in_location"]
    digest = sources_hash(FUNCTIONS)
    assert data["to"]["hash"] == digest and data["to"]["count"] == 2
    assert data["to"]["published_against"]["version"] == shown["ontology"]["version"]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["functions"] == {"count": 2, "hash": digest}
    listed = show(run_cli).data
    assert listed["slot"] == data["to"]
    assert [(f["name"], f["status"], f["featured"]) for f in listed["functions"]] == [
        ("items_by_condition", "active", False),
        ("items_in_location", "active", True),
    ]
    intent = graph_events(root, graph_id)[-1]
    assert intent["actions"] == ["maint.graph_configure"]
    assert intent["change"]["functions"]["from"] is None
    assert intent["change"]["functions"]["to"] == digest
    assert intent["change"]["functions"]["compiled_hash"] == data["to"]["compiled_hash"]
    assert intent["reason"] == "first functions"
    companion = installation_events(root)[-1]
    assert companion["actions"] == ["maint.graph_configure"]
    assert companion["anchors"] == [
        {"graph_id": graph_id, "seq": intent["seq"], "hash": intent["hash"]}
    ]
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_republishing_the_same_files_changes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    assert publish(run_cli, FUNCTIONS).code == 0
    before = tree_hash(root)
    again = publish(run_cli, folder(tmp_path, *(p.name for p in FUNCTIONS.glob("*.yaml"))))
    assert again.code == 0 and again.data["published"] is False
    assert again.data["from"] == again.data["to"]
    assert tree_hash(root) == before


def test_republishing_changed_files_names_both_hashes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    first = publish(run_cli, FUNCTIONS).data["to"]["hash"]
    changed = folder(tmp_path, "items_in_location.yaml")
    outcome = publish(run_cli, changed)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == first
    assert outcome.data["to"]["count"] == 1
    assert graph_events(root, graph_id)[-1]["change"]["functions"] == {
        "from": first,
        "to": sources_hash(changed),
        "compiled_hash": outcome.data["to"]["compiled_hash"],
    }
    assert [f["name"] for f in show(run_cli).data["functions"]] == ["items_in_location"]
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_new_release_makes_the_same_files_publishable_again(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    publish(run_cli, FUNCTIONS)
    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.0.1")
    )
    assert released.code == 0, released.envelope
    outcome = publish(run_cli, FUNCTIONS)
    assert outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == outcome.data["to"]["hash"]
    assert outcome.data["to"]["published_against"]["version"] == "1.0.1"
    assert admin(run_cli, "audit", "verify").code == 0


def test_dry_run_checks_and_writes_nothing(run_cli: CliRunner, root: Path, graph_id: str) -> None:
    before = tree_hash(root)
    outcome = publish(run_cli, FUNCTIONS, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["published"], outcome.data["dry_run"]) == (False, True)
    assert outcome.data["to"]["count"] == 2
    assert tree_hash(root) == before
    refused = publish(run_cli, BROKEN, "--dry-run")  # the dry run is the check
    assert refused.code == 1 and refused.error["code"] == "admin.functions_invalid"
    assert tree_hash(root) == before


def test_an_empty_folder_unpublishes_every_function(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    empty = folder(tmp_path)
    before = tree_hash(root)
    nothing = publish(run_cli, empty)  # nothing published: nothing to unpublish
    assert nothing.code == 0 and nothing.data["published"] is False
    assert tree_hash(root) == before
    first = publish(run_cli, FUNCTIONS).data["to"]["hash"]
    outcome = publish(run_cli, empty)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["functions"] == [] and outcome.data["to"]["count"] == 0
    assert outcome.data["from"]["hash"] == first
    assert admin(run_cli, "graph", "show", "example_graph").data["functions"]["count"] == 0
    assert show(run_cli).data["functions"] == []
    assert publish(run_cli, empty).data["published"] is False
    assert admin(run_cli, "audit", "verify").code == 0


def test_invalid_files_list_every_problem(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    broken = folder(tmp_path, "items_by_condition.yaml", name="broken")
    shutil.copy(BROKEN / "items_in_location.yaml", broken)
    renamed = (FUNCTIONS / "items_by_condition.yaml").read_text()
    (broken / "items_by_state.yaml").write_text(
        renamed.replace("name: items_by_condition", "name: items_by_state").replace(
            "- column: condition", "- column: state"
        )
    )
    (broken / "notes.txt").write_text("not a function\n")
    (broken / "sub").mkdir()
    (broken / "unparsable.yaml").write_text("name: [unclosed\n")
    before = tree_hash(root)
    outcome = publish(run_cli, broken)
    assert outcome.code == 1
    assert outcome.error["code"] == "admin.functions_invalid"
    assert outcome.error["category"] == "invalid" and outcome.error["hint"] is None
    problems = outcome.error["details"]["problems"]
    assert {(p["function"], p["code"]) for p in problems} >= {
        ("notes.txt", "function.invalid_file"),
        ("sub", "function.invalid_file"),
        ("unparsable", "function.invalid_yaml"),
        ("items_in_location", "term.unknown"),
        ("items_by_state", "function.returns_mismatch"),
    }
    assert not [p for p in problems if p["function"] == "items_by_condition"]
    assert tree_hash(root) == before


def test_a_clean_copy_publishes(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    """Positive control for the refusal above: the same folder without the breakage."""
    assert publish(run_cli, folder(tmp_path, "items_by_condition.yaml")).code == 0


def test_text_mode_names_the_problems(run_cli: CliRunner, graph_id: str) -> None:
    result = run_cli("graph-admin", "graph", "functions", "publish", "example_graph", str(BROKEN))
    assert result.returncode == 1
    assert "admin.functions_invalid" in result.stderr and "items_in_location" in result.stderr


def test_an_unknown_graph_is_not_found(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    outcome = admin(run_cli, "graph", "functions", "show", "nothing")
    assert outcome.error["code"] == "graph.not_found"
    outcome = admin(run_cli, "graph", "functions", "publish", "nothing", str(folder(tmp_path)))
    assert outcome.error["code"] == "graph.not_found"
