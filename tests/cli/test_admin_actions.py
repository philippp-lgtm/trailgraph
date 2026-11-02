"""S: graph-admin graph actions publish|show (actions plan A8, A9; B6 ``maint.graph_configure``,
W.e).

The publish event pair (graph intent plus installation anchor) verifies clean, a re-publish of
the same files changes nothing, an empty folder unpublishes every action, every problem of every
action is reported in one answer, and actions and functions share one namespace per graph.

The graphs here carry the inventory test ontology; ``tests/fixtures/actions/inventory`` holds
actions that compile against all its versions. The company fixtures compile only against the
tracked example ontology and are published into a graph created from it.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import EXAMPLES, FIXTURES, ONTOLOGIES
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.ports.catalog import sources_hash
from graph_cli.storage.files.audit_log import FileAuditLog

ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
BROKEN = FIXTURES / "actions" / "inventory" / "broken"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
COMPANY_ACTIONS = FIXTURES / "actions" / "company" / "valid"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    return create_graph(run_cli, "example_graph")


def publish(run_cli: CliRunner, source: Path, *args: str, graph: str = "example_graph") -> Outcome:
    return admin(run_cli, "graph", "actions", "publish", graph, str(source), *args)


def publish_functions(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "functions", "publish", "example_graph", str(source), *args)


def show(run_cli: CliRunner) -> Outcome:
    return admin(run_cli, "graph", "actions", "show", "example_graph")


def folder(tmp_path: Path, *names: str, name: str = "actions", source: Path = ACTIONS) -> Path:
    """An actions folder with the named files of ``source`` (none: an empty folder)."""
    target = tmp_path / "work" / name
    target.mkdir(parents=True, exist_ok=True)
    for file_name in names:
        shutil.copy(source / file_name, target)
    return target


def digest_of(directory: Path) -> str:
    return sources_hash({p.name: p.read_bytes() for p in directory.glob("*.yaml")})


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
    assert shown["actions"] is None and shown["functions"] is None
    assert show(run_cli).data == {"graph_id": graph_id, "slot": None, "actions": []}
    outcome = publish(run_cli, ACTIONS, "--reason", "first actions")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert (data["published"], data["dry_run"], data["from"]) == (True, False, None)
    assert data["actions"] == ["move_item", "retire_item"]
    digest = digest_of(ACTIONS)
    assert data["to"]["hash"] == digest and data["to"]["count"] == 2
    assert data["to"]["published_against"]["version"] == shown["ontology"]["version"]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["actions"] == {"count": 2, "hash": digest}
    assert shown["functions"] is None  # the other slot is untouched
    listed = show(run_cli).data
    assert listed["slot"] == data["to"]
    assert [(a["name"], a["status"], a["featured"]) for a in listed["actions"]] == [
        ("move_item", "active", True),
        ("retire_item", "active", False),
    ]
    intent = graph_events(root, graph_id)[-1]
    assert intent["actions"] == ["maint.graph_configure"]
    assert set(intent["change"]) == {"actions"}
    assert intent["change"]["actions"]["from"] is None
    assert intent["change"]["actions"]["to"] == digest
    assert intent["change"]["actions"]["compiled_hash"] == data["to"]["compiled_hash"]
    assert intent["reason"] == "first actions"
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
    assert publish(run_cli, ACTIONS).code == 0
    before = tree_hash(root)
    again = publish(run_cli, folder(tmp_path, *(p.name for p in ACTIONS.glob("*.yaml"))))
    assert again.code == 0 and again.data["published"] is False
    assert again.data["from"] == again.data["to"]
    assert tree_hash(root) == before


def test_republishing_changed_files_names_both_hashes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    first = publish(run_cli, ACTIONS).data["to"]["hash"]
    changed = folder(tmp_path, "move_item.yaml")
    outcome = publish(run_cli, changed)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == first
    assert outcome.data["to"]["count"] == 1
    assert graph_events(root, graph_id)[-1]["change"]["actions"] == {
        "from": first,
        "to": digest_of(changed),
        "compiled_hash": outcome.data["to"]["compiled_hash"],
    }
    assert [a["name"] for a in show(run_cli).data["actions"]] == ["move_item"]
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_new_release_makes_the_same_files_publishable_again(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    publish(run_cli, ACTIONS)
    released = admin(
        run_cli, "ontology", "release", "example_graph", str(ONTOLOGIES / "inventory" / "1.0.1")
    )
    assert released.code == 0, released.envelope
    outcome = publish(run_cli, ACTIONS)
    assert outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == outcome.data["to"]["hash"]
    assert outcome.data["to"]["published_against"]["version"] == "1.0.1"
    assert admin(run_cli, "audit", "verify").code == 0


def test_dry_run_checks_and_writes_nothing(run_cli: CliRunner, root: Path, graph_id: str) -> None:
    before = tree_hash(root)
    outcome = publish(run_cli, ACTIONS, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["published"], outcome.data["dry_run"]) == (False, True)
    assert outcome.data["to"]["count"] == 2
    assert tree_hash(root) == before
    refused = publish(run_cli, BROKEN, "--dry-run")  # the dry run is the check
    assert refused.code == 1 and refused.error["code"] == "admin.actions_invalid"
    assert tree_hash(root) == before


def test_an_empty_folder_unpublishes_every_action(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    empty = folder(tmp_path)
    before = tree_hash(root)
    nothing = publish(run_cli, empty)  # nothing published: nothing to unpublish
    assert nothing.code == 0 and nothing.data["published"] is False
    assert tree_hash(root) == before
    first = publish(run_cli, ACTIONS).data["to"]["hash"]
    outcome = publish(run_cli, empty)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["actions"] == [] and outcome.data["to"]["count"] == 0
    assert outcome.data["from"]["hash"] == first
    assert admin(run_cli, "graph", "show", "example_graph").data["actions"]["count"] == 0
    assert show(run_cli).data["actions"] == []
    assert publish(run_cli, empty).data["published"] is False
    assert admin(run_cli, "audit", "verify").code == 0


def test_invalid_files_list_every_problem(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    broken = folder(tmp_path, "retire_item.yaml", name="broken")
    shutil.copy(BROKEN / "move_item.yaml", broken)
    renamed = (ACTIONS / "retire_item.yaml").read_text()
    (broken / "scrap_item.yaml").write_text(renamed.replace("name: retire_item", "name: other"))
    (broken / "notes.txt").write_text("not an action\n")
    (broken / "sub").mkdir()
    (broken / "unparsable.yaml").write_text("name: [unclosed\n")
    before = tree_hash(root)
    outcome = publish(run_cli, broken)
    assert outcome.code == 1
    assert outcome.error["code"] == "admin.actions_invalid"
    assert outcome.error["category"] == "invalid" and outcome.error["hint"] is None
    problems = outcome.error["details"]["problems"]
    assert {(p["action"], p["code"]) for p in problems} >= {
        ("notes.txt", "action.invalid_file"),
        ("sub", "action.invalid_file"),
        ("unparsable", "action.invalid_yaml"),
        ("move_item", "action.unknown_relation"),
        ("scrap_item", "action.name_mismatch"),
    }
    assert all("function" not in p for p in problems)
    assert not [p for p in problems if p["action"] == "retire_item"]
    assert tree_hash(root) == before


def test_a_clean_copy_publishes(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    """Positive control for the refusal above: the same folder without the breakage."""
    assert publish(run_cli, folder(tmp_path, "retire_item.yaml")).code == 0


def test_text_mode_names_the_problems(run_cli: CliRunner, graph_id: str) -> None:
    result = run_cli("graph-admin", "graph", "actions", "publish", "example_graph", str(BROKEN))
    assert result.returncode == 1
    assert "admin.actions_invalid" in result.stderr and "move_item" in result.stderr


def test_an_unknown_graph_is_not_found(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    outcome = admin(run_cli, "graph", "actions", "show", "nothing")
    assert outcome.error["code"] == "graph.not_found"
    outcome = admin(run_cli, "graph", "actions", "publish", "nothing", str(folder(tmp_path)))
    assert outcome.error["code"] == "graph.not_found"


def test_an_archived_graph_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    manifest = root / "graphs" / graph_id / "manifest.toml"
    manifest.write_text(manifest.read_text().replace('state = "active"', 'state = "archived"'))
    before = tree_hash(root)
    for dry in ([], ["--dry-run"]):
        outcome = publish(run_cli, ACTIONS, *dry)
        assert (outcome.code, outcome.error["code"]) == (1, "graph.archived")
    assert tree_hash(root) == before


def test_actions_and_functions_share_one_namespace(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """A1: an action may not be named like a published function, nor a function like a
    published action; the other kind's slot is never touched."""
    assert publish_functions(run_cli, FUNCTIONS).code == 0
    clash = folder(tmp_path, "items_in_location.yaml", name="clash", source=BROKEN)
    before = tree_hash(root)
    refused = publish(run_cli, clash)
    assert (refused.code, refused.error["code"]) == (1, "admin.actions_invalid")
    assert [(p["action"], p["code"]) for p in refused.error["details"]["problems"]] == [
        ("items_in_location", "action.name_taken")
    ]
    assert tree_hash(root) == before
    # Unpublish the function and the same folder publishes: positive control.
    assert publish_functions(run_cli, folder(tmp_path, name="no_functions")).code == 0
    assert publish(run_cli, clash).code == 0
    # Now the other way round: the function files clash with the published action.
    before = tree_hash(root)
    refused = publish_functions(run_cli, FUNCTIONS)
    assert (refused.code, refused.error["code"]) == (1, "admin.functions_invalid")
    problems = refused.error["details"]["problems"]
    assert [(p["function"], p["code"]) for p in problems] == [
        ("items_in_location.yaml", "function.name_taken")
    ]
    assert tree_hash(root) == before
    # Files without a clash still publish next to the action.
    other = folder(tmp_path, "items_by_condition.yaml", name="other", source=FUNCTIONS)
    assert publish_functions(run_cli, other).code == 0
    assert admin(run_cli, "audit", "verify").code == 0


def test_publishing_functions_without_actions_answers_as_before(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    outcome = publish_functions(run_cli, FUNCTIONS)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert "actions" not in outcome.data


def test_the_company_actions_publish_into_a_graph_of_the_example_ontology(
    run_cli: CliRunner, root: Path
) -> None:
    created = admin(
        run_cli,
        "graph",
        "create",
        "company_graph",
        "--ontology",
        str(EXAMPLES / "company" / "ontology" / "1.1.0"),
        "--module",
        "company",
    )
    assert created.code == 0, created.envelope
    outcome = publish(run_cli, COMPANY_ACTIONS, graph="company_graph")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["actions"] == sorted(p.stem for p in COMPANY_ACTIONS.glob("*.yaml"))
    assert admin(run_cli, "audit", "verify").code == 0
