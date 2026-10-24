"""S: graph-admin graph pull|push (plan origin_lifecycle E6, E7; contracts B14.19).

A pull writes the release's authored modules and the published skill byte
for byte, plus the base; a push releases and publishes what changed under
one lock, refuses when the graph moved on since the pull, checks everything
before the first write, and is safe to repeat.
"""

import json
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
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.kernel.ports.catalog import skill_hash
from graph_cli.skills.tree import read_tree
from graph_cli.storage.files.audit_log import FileAuditLog

TEMPLATE = FIXTURES / "skills" / "template" / "graph-example-graph"
INVENTORY = ONTOLOGIES / "inventory"
SKILL = Path("skill") / "graph-example-graph"
BASE = ".graph_cli_source.json"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    graph_id = create_graph(run_cli, "example_graph", version="1.0.0")
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(TEMPLATE))
    assert published.code == 0, published.envelope
    return graph_id


def pull(run_cli: CliRunner, target: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "pull", "example_graph", str(target), *args)


def push(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "push", "example_graph", str(source), *args)


def pulled(run_cli: CliRunner, tmp_path: Path, name: str = "work") -> Path:
    target = tmp_path / name
    outcome = pull(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    return target


def base(directory: Path) -> Any:
    return json.loads((directory / BASE).read_text())


def new_version(directory: Path, version: str) -> None:
    shutil.copytree(INVENTORY / version, directory / "ontology" / version)


def edit_skill(directory: Path, text: str = "\nOne more recipe.\n") -> None:
    recipes = directory / SKILL / "references" / "recipes.md"
    recipes.write_text(recipes.read_text() + text)


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


# ---- pull --------------------------------------------------------------------------------


def test_pull_writes_the_release_sources_and_the_skill_byte_for_byte(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    before = tree_hash(root)
    target = tmp_path / "work"
    outcome = pull(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert data["path"] == str(target)
    assert data["files"] == [
        BASE,
        "ontology/1.0.0/inventory.yaml",
        "skill/graph-example-graph/SKILL.md",
        "skill/graph-example-graph/references/recipes.md",
    ]
    assert (data["ontology"]["version"], data["ontology"]["pinned"]) == ("1.0.0", True)
    assert data["ontology"]["modules"] == ["inventory"]
    assert (target / "ontology/1.0.0/inventory.yaml").read_bytes() == (
        INVENTORY / "1.0.0" / "inventory.yaml"
    ).read_bytes()
    template, _ = read_tree(TEMPLATE)
    assert read_tree(target / SKILL)[0] == template
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert base(target) == {
        "schema": "graph_cli.graph_source/1",
        "graph": {"id": graph_id, "name": "example_graph"},
        "ontology": shown["ontology"] | {"artifact_hash": shown["ontology"]["artifact_hash"]},
        "skill": {"name": "graph-example-graph", "kind": "generated", "hash": skill_hash(template)},
    }
    assert tree_hash(root) == before  # a pull changes nothing in the data root


def test_a_graph_without_a_skill_pulls_its_ontology_only(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    assert not (target / "skill").exists()
    assert base(target)["skill"] is None


def test_pull_refuses_its_own_entries_unless_forced_and_keeps_the_rest(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    (target / "README.md").write_text("notes\n")
    new_version(target, "1.0.1")  # a draft the pull does not own
    (target / SKILL / "stale.md").write_text("removed upstream\n")
    refused = pull(run_cli, target)
    assert (refused.code, refused.error["code"]) == (1, "admin.source_exists")
    assert refused.error["details"]["paths"] == [
        BASE,
        "ontology/1.0.0",
        "skill/graph-example-graph",
    ]
    assert refused.error["hint"] == f"graph-admin graph pull example_graph {target} --force"
    assert (target / SKILL / "stale.md").exists()
    forced = pull(run_cli, target, "--force")
    assert forced.code == 0, forced.envelope
    assert not (target / SKILL / "stale.md").exists()  # the skill directory was replaced
    assert (target / "README.md").read_text() == "notes\n"
    assert (target / "ontology" / "1.0.1" / "inventory.yaml").exists()


def test_pull_an_older_release(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    assert (
        admin(run_cli, "ontology", "release", "example_graph", str(INVENTORY / "1.0.1")).code == 0
    )
    outcome = pull(run_cli, tmp_path / "old", "--version", "1.0.0")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is False
    assert base(tmp_path / "old")["ontology"]["version"] == "1.0.0"
    missing = pull(run_cli, tmp_path / "none", "--version", "9.9.9")
    assert missing.error["code"] == "usage.invalid_argument"
    assert missing.error["details"]["installed"] == ["1.0.0", "1.0.1"]
    assert not (tmp_path / "none").exists()


# ---- push --------------------------------------------------------------------------------


def test_push_of_an_unchanged_pull_changes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    before, written = tree_hash(root), (target / BASE).read_bytes()
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["unchanged"] is True
    assert (outcome.data["ontology"]["released"], outcome.data["skill"]["published"]) == (
        False,
        False,
    )
    assert tree_hash(root) == before
    assert (target / BASE).read_bytes() == written


def test_push_releases_and_publishes_what_changed(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    new_version(target, "1.1.0")
    edit_skill(target)
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "new terms")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert data["unchanged"] is False
    assert (data["ontology"]["released"], data["ontology"]["pinned"]) == (True, True)
    assert data["ontology"]["to"]["version"] == "1.1.0"
    assert data["skill"]["published"] is True
    assert data["skill"]["to"]["published_against"]["version"] == "1.1.0"
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "1.1.0"
    assert shown["skill"]["hash"] == skill_hash(read_tree(target / SKILL)[0])
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [
        ["maint.ontology_release"],
        ["maint.ontology_pin"],
        ["maint.graph_configure"],
    ]
    assert {e["reason"] for e in intents} == {"new terms"}
    assert {e["request"]["command"] for e in intents} == {"graph push"}
    assert base(target) == data["source"]
    assert base(target)["ontology"]["version"] == "1.1.0"
    assert admin(run_cli, "audit", "verify").code == 0
    again = push(run_cli, target)
    assert again.code == 0 and again.data["unchanged"] is True


def test_dry_run_checks_everything_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    new_version(target, "1.1.0")
    edit_skill(target)
    before, written = tree_hash(root), (target / BASE).read_bytes()
    outcome = push(run_cli, target, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["unchanged"], outcome.data["dry_run"]) == (False, True)
    assert outcome.data["ontology"]["released"] is False
    assert outcome.data["skill"]["published"] is False
    assert outcome.data["source"]["ontology"]["version"] == "1.1.0"
    assert tree_hash(root) == before
    assert (target / BASE).read_bytes() == written


def test_a_graph_that_moved_on_is_a_conflict(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    mine = pulled(run_cli, tmp_path, "mine")
    theirs = pulled(run_cli, tmp_path, "theirs")
    edit_skill(theirs, "\nTheirs.\n")
    assert push(run_cli, theirs).code == 0
    edit_skill(mine, "\nMine.\n")
    before = tree_hash(root)
    refused = push(run_cli, mine)
    assert (refused.code, refused.error["code"]) == (4, "admin.source_conflict")
    assert refused.error["category"] == "conflict"
    details = refused.error["details"]
    assert details["base"]["skill"] == base(mine)["skill"]["hash"]
    assert details["current"]["skill"] == base(theirs)["skill"]["hash"]
    assert tree_hash(root) == before
    # The remedy: pull again, carry the change over, push.
    again = pulled(run_cli, tmp_path, "again")
    edit_skill(again, "\nMine.\n")
    assert push(run_cli, again).code == 0


def test_a_push_whose_base_was_lost_can_be_repeated(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    """The data root holds what is pushed already: like a push that succeeded but whose
    answer (and so the new base) never arrived."""
    target = pulled(run_cli, tmp_path)
    old_base = (target / BASE).read_bytes()
    new_version(target, "1.0.1")
    edit_skill(target)
    assert push(run_cli, target).code == 0
    (target / BASE).write_bytes(old_base)
    again = push(run_cli, target)
    assert again.code == 0, again.envelope
    assert again.data["unchanged"] is True
    assert base(target)["ontology"]["version"] == "1.0.1"


def test_the_skill_is_checked_against_the_ontology_it_will_render_with(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    skill_md = target / SKILL / "SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nRecord suppliers as `inv:Supplier`.\n")
    before = tree_hash(root)
    refused = push(run_cli, target)  # inv:Supplier comes with 1.1.0
    assert refused.error["code"] == "admin.skill_invalid", refused.envelope
    assert [p["rule"] for p in refused.error["details"]["problems"]] == ["term"]
    assert tree_hash(root) == before
    new_version(target, "1.1.0")
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["skill"]["published"] is True


def test_a_failing_release_check_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.0.1")  # declares 1.1.0
    edit_skill(target)
    before = tree_hash(root)
    misnamed = push(run_cli, target)
    assert misnamed.error["code"] == "usage.invalid_argument"
    assert "declares version 1.1.0" in misnamed.error["message"]
    shutil.rmtree(target / "ontology" / "1.0.1")
    edited = target / "ontology" / "1.0.0" / "inventory.yaml"
    edited.write_text(edited.read_text() + "# a comment is a change too\n")  # no version bump
    exists = push(run_cli, target)
    assert exists.error["code"] == "admin.release_exists", exists.envelope
    assert "bump the module version" in exists.error["message"]
    assert tree_hash(root) == before


def test_push_refuses_a_directory_of_another_graph(
    run_cli: CliRunner, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    create_graph(run_cli, "second", version="1.0.0")
    other = admin(run_cli, "graph", "push", "second", str(target))
    assert other.error["code"] == "usage.invalid_argument"
    assert "holds graph example_graph" in other.error["message"]
    (target / BASE).unlink()
    missing = push(run_cli, target)
    assert missing.error["code"] == "usage.invalid_argument"
    assert "pull the graph into it first" in missing.error["message"]
