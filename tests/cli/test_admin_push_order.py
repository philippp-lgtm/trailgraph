"""S: the order in which ``graph-admin graph push`` reads, checks, answers and warns.

The existing push tests pin what a push does; these pin the order that decides which defect
is reported first when a workspace has several, the key order of the answer and the order of
the warnings of one push, so that a restructuring of the command keeps all three.
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES, ONTOLOGIES

INVENTORY = ONTOLOGIES / "inventory"
PERSONNEL = ONTOLOGIES / "personnel"
TEMPLATE = FIXTURES / "skills" / "template" / "graph-example-graph"
FUNCTIONS = FIXTURES / "functions" / "inventory"
ACTIONS = FIXTURES / "actions" / "inventory"
VIEWS = FIXTURES / "views" / "inventory"
SKILL = Path("skill") / "graph-example-graph"
BASE = ".graph_cli_source.json"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def pull(run_cli: CliRunner, target: Path) -> Path:
    outcome = admin(run_cli, "graph", "pull", "example_graph", str(target))
    assert outcome.code == 0, outcome.envelope
    return target


def push(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "push", "example_graph", str(source), *args)


def put(source: Path, kind: str, fixture: Path, *names: str) -> None:
    """Copy the fixture files ``names`` into the ``kind`` folder of the workspace."""
    (source / kind).mkdir(exist_ok=True)
    for name in names:
        shutil.copy(fixture / name, source / kind / name)


def problem_names(outcome: Outcome, key: str) -> set[str]:
    return {p[key] for p in outcome.error["details"]["problems"]}


# ---- refusal precedence --------------------------------------------------------------------


def test_a_push_with_several_broken_parts_names_them_in_the_order_of_the_checks(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """Functions, then actions, then views, then the skill: fixing the first reveals the next."""
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    put(source, "functions", FUNCTIONS / "broken", "items_in_location.yaml")
    put(source, "actions", ACTIONS / "broken", "move_item.yaml")
    put(source, "views", VIEWS / "broken", "card.yaml")
    shutil.copytree(TEMPLATE, source / SKILL)
    skill_md = source / SKILL / "SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nRecord suppliers as `inv:Supplier`.\n")
    first = push(run_cli, source, "--dry-run")
    assert first.error["code"] == "admin.functions_invalid", first.envelope
    assert problem_names(first, "function") == {"items_in_location"}

    shutil.rmtree(source / "functions")
    put(source, "functions", FUNCTIONS / "valid", "items_in_location.yaml")
    second = push(run_cli, source, "--dry-run")
    assert second.error["code"] == "admin.actions_invalid", second.envelope
    assert problem_names(second, "action") == {"move_item"}

    shutil.rmtree(source / "actions")
    put(source, "actions", ACTIONS / "valid", "move_item.yaml")
    third = push(run_cli, source, "--dry-run")
    assert third.error["code"] == "admin.views_invalid", third.envelope
    assert problem_names(third, "view") == {"card"}

    shutil.rmtree(source / "views")
    fourth = push(run_cli, source, "--dry-run")
    assert fourth.error["code"] == "admin.skill_invalid", fourth.envelope
    assert [p["rule"] for p in fourth.error["details"]["problems"]] == ["term"]

    skill_md.write_text(skill_md.read_text().replace("\nRecord suppliers as `inv:Supplier`.\n", ""))
    fixed = push(run_cli, source, "--dry-run")
    assert fixed.code == 0, fixed.envelope


# ---- reading the workspace before the lock -------------------------------------------------


def test_the_ontology_directory_is_read_before_the_skill_directory(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    (source / "skill" / "graph-other").mkdir(parents=True)
    (source / "skill" / "graph-other" / "SKILL.md").write_text("another graph\n")
    skill_first = push(run_cli, source, "--dry-run")
    assert skill_first.error["code"] == "usage.invalid_argument"
    assert "the skill of this graph is graph-example-graph" in skill_first.error["message"]
    shutil.rmtree(source / "ontology" / "1.0.0")
    ontology_first = push(run_cli, source, "--dry-run")
    assert ontology_first.error["code"] == "usage.invalid_argument"
    assert "holds no version directory" in ontology_first.error["message"]


def test_the_migration_file_is_read_before_the_skill_directory(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    (source / "skill" / "graph-other").mkdir(parents=True)
    (source / "skill" / "graph-other" / "SKILL.md").write_text("another graph\n")
    migration = source / "ontology" / "1.0.0" / "migration.yaml"
    migration.write_text("format: graph_cli.migration/1\n")
    migration.chmod(0o000)
    try:
        if os.access(migration, os.R_OK):
            pytest.skip("file permissions do not bind this user")
        refused = push(run_cli, source, "--dry-run")
    finally:
        migration.chmod(0o600)
    assert refused.error["code"] == "usage.invalid_argument", refused.envelope
    assert f"cannot read {migration}" in refused.error["message"]  # not the skill directory


def test_the_skill_directory_is_read_before_the_ontology_is_judged_a_new_release(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    edited = source / "ontology" / "1.0.0" / "inventory.yaml"
    edited.write_text(edited.read_text() + "# a comment is a change too\n")
    (source / "skill" / "graph-other").mkdir(parents=True)
    (source / "skill" / "graph-other" / "SKILL.md").write_text("another graph\n")
    skill_first = push(run_cli, source, "--dry-run")
    assert skill_first.error["code"] == "usage.invalid_argument", skill_first.envelope
    shutil.rmtree(source / "skill")
    exists = push(run_cli, source, "--dry-run")
    assert exists.error["code"] == "admin.release_exists", exists.envelope


def test_the_graph_of_a_directory_is_judged_before_its_writability(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    create_graph(run_cli, "second", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    source.chmod(0o500)
    try:
        if os.access(source, os.W_OK):
            pytest.skip("directory permissions do not bind this user")
        other = admin(run_cli, "graph", "push", "second", str(source))
        assert "holds graph example_graph" in other.error["message"]
        unwritable = push(run_cli, source)
        assert "is not writable" in unwritable.error["message"]
        assert push(run_cli, source, "--dry-run").code == 0  # a dry run records nothing
    finally:
        source.chmod(0o700)


# ---- the answer ----------------------------------------------------------------------------

ANSWER_KEYS = [
    "graph_id",
    "ontology",
    "skill",
    "functions",
    "actions",
    "unchanged",
    "dry_run",
    "source",
]


def test_the_answer_of_a_graph_without_views_has_no_views_key(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    bare = push(run_cli, source)
    assert bare.code == 0, bare.envelope
    assert list(bare.data) == ANSWER_KEYS
    assert (bare.data["skill"], bare.data["functions"], bare.data["actions"]) == (None, None, None)
    for kind in ("functions", "actions"):  # an empty folder is a folder: the answer has the key
        (source / kind).mkdir()
    empty = push(run_cli, source)
    assert list(empty.data) == ANSWER_KEYS
    assert empty.data["functions"] == {
        "from": None,
        "to": None,
        "functions": [],
        "published": False,
    }
    assert empty.data["actions"] == {"from": None, "to": None, "actions": [], "published": False}
    assert empty.data["unchanged"] is True


def test_a_published_catalog_is_answered_even_when_its_folder_is_gone(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    put(source, "functions", FUNCTIONS / "valid", "items_in_location.yaml")
    put(source, "actions", ACTIONS / "valid", "move_item.yaml")
    published = push(run_cli, source)
    assert published.code == 0, published.envelope
    assert list(published.data) == ANSWER_KEYS
    assert published.data["functions"]["functions"] == ["items_in_location"]
    assert published.data["actions"]["actions"] == ["move_item"]
    shutil.rmtree(source / "functions")
    shutil.rmtree(source / "actions")
    emptied = push(run_cli, source)  # the missing folders unpublish both catalogs
    assert emptied.code == 0, emptied.envelope
    assert emptied.data["functions"]["functions"] == []
    assert emptied.data["functions"]["published"] is True
    assert emptied.data["actions"]["actions"] == []
    assert emptied.data["actions"]["published"] is True


def test_the_views_key_comes_last_and_only_for_a_graph_with_views(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    put(source, "functions", FUNCTIONS / "valid", "items_in_location.yaml")
    put(source, "views", VIEWS / "valid", "items_in_location.yaml")
    created = push(run_cli, source)
    assert created.code == 0, created.envelope
    assert list(created.data) == [*ANSWER_KEYS, "views"]
    assert created.data["views"]["published"] is True
    again = push(run_cli, source, "--dry-run")
    assert list(again.data) == [*ANSWER_KEYS, "views"]
    shutil.rmtree(source / "views")
    emptied = push(run_cli, source)  # the key follows the slot: an empty catalog is a catalog
    assert list(emptied.data) == [*ANSWER_KEYS, "views"]
    assert emptied.data["views"]["views"] == []


def test_the_answer_names_the_release_the_push_made(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    source = pull(run_cli, tmp_path / "work")
    shutil.copytree(INVENTORY / "1.1.0", source / "ontology" / "1.1.0")
    dry = push(run_cli, source, "--dry-run")
    released: dict[str, Any] = dry.data["ontology"]
    assert "graph_id" not in released
    assert (released["released"], released["pinned"]) == (False, False)
    done = push(run_cli, source)
    assert done.data["ontology"]["to"]["version"] == "1.1.0"
    assert done.data["source"]["ontology"]["version"] == "1.1.0"


# ---- the warnings --------------------------------------------------------------------------

TEAM_FUNCTION = """\
schema: graph_cli.function/1
name: people_in_team
summary: The people of one team.
use_when: Who works in a given team.
params:
  team:
    type: string
    summary: The team to list.
query: |
  MATCH (p:Person)
  WHERE p.team = $team
  RETURN p.name AS person
  ORDER BY person
returns:
  - column: person
    summary: Name of the person.
examples:
  - question: Who works in the platform team?
    params: {team: platform}
featured: true
status: deprecated
"""
EMAIL_ACTION = """\
schema: graph_cli.action/1
name: change_email
summary: Replace the e-mail address of a person that has one.
use_when: A person's private e-mail address changes.
params:
  person:
    type: ref
    class: ex:Person
    summary: The person.
  email:
    type: string
    summary: The new address.
preconditions:
  - id: has_email
    on: person
    summary: The person has an address on record.
    require:
      - {attr: email, value_presence: PRESENT}
ops:
  - {op: update, id: $person, set: {email: $email}}
examples:
  - request: Pat has a new e-mail address.
"""
TEAM_VIEW = """\
schema: graph_cli.view/1
name: team_roster
summary: %s
use_when: Who works in a team, as a view.
params:
  team:
    type: string
    summary: The team to list.
sections:
  - kind: rows
    function: people_in_team
    args: {team: $team}
"""
PEOPLE_SKILL = """\
---
name: graph-example-graph
description: "People and teams of example_graph. Use to ask who works where. Needs graph-cli."
---

# Graph `example_graph`: people and teams

## Is it available here?

<!-- graph_cli:block start -->

## What is in it

<!-- graph_cli:block types -->

## Rules for you

- Preview every write with `--dry-run` first.
"""
RELEASE_WARNINGS = ["admin.marking_removed", "admin.marking_ungranted", "admin.marking_ungranted"]


def test_the_warnings_of_one_push_come_in_the_order_release_actions_views_skill(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(PERSONNEL / "1.0.0")
    )
    assert created.code == 0, created.envelope
    source = pull(run_cli, tmp_path / "work")
    for kind, name, text in (
        ("functions", "people_in_team", TEAM_FUNCTION),
        ("actions", "change_email", EMAIL_ACTION),
        ("views", "team_roster", TEAM_VIEW % "The people of a team."),
    ):
        (source / kind).mkdir()
        (source / kind / f"{name}.yaml").write_text(text)
    first = push(run_cli, source)  # publishes the catalogs on 1.0.0
    assert first.code == 0, first.envelope
    assert first.warning_codes == [
        "admin.marking_in_expression",
        "view.function_deprecated",
    ]
    # One push that releases, replaces the actions and the views, and publishes a skill.
    shutil.copytree(PERSONNEL / "1.1.0", source / "ontology" / "1.1.0")
    action = source / "actions" / "change_email.yaml"
    action.write_text(action.read_text().replace("Replace the", "Change the"))
    view = source / "views" / "team_roster.yaml"
    view.write_text(TEAM_VIEW % "Everyone of a team.")
    (source / SKILL).mkdir(parents=True)
    (source / SKILL / "SKILL.md").write_text(PEOPLE_SKILL + "A rule that says a lot.\n" * 300)
    outcome = push(run_cli, source, "--reason", "all at once")
    assert outcome.code == 0, outcome.envelope
    assert outcome.warning_codes == [
        *RELEASE_WARNINGS,
        "admin.marking_in_expression",
        "view.function_deprecated",
        "admin.skill_over_budget",
    ]
    [precondition] = [w for w in outcome.warnings if w["code"] == "admin.marking_in_expression"]
    assert precondition["details"]["kind"] == "precondition"  # the pushed catalog, not the release
    again = push(run_cli, source, "--dry-run")  # nothing is left to publish or to judge
    assert again.warning_codes == []
