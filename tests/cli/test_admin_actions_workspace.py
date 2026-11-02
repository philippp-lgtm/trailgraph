"""S: actions in the maintainer workspace and in ontology releases (actions plan A8, A9).

``graph pull`` writes ``actions/<name>.yaml``; ``graph push`` checks the files against the
ontology the graph will have and publishes them after the release, so a release that breaks
an action is refused and one push can carry the ontology with the fixed or deleted action
files. Functions and actions share one namespace, also inside one push.
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
from graph_cli.kernel.ports.catalog import sources_hash as hash_of
from graph_cli.storage.files.audit_log import FileAuditLog

INVENTORY = ONTOLOGIES / "inventory"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
BASE = ".graph_cli_source.json"
MOVE = "move_item.yaml"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    graph_id = create_graph(run_cli, "example_graph", version="1.0.0")
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(ACTIONS))
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


def sources_hash(directory: Path) -> str:
    return hash_of({p.name: p.read_bytes() for p in directory.glob("*.yaml")})


def next_major(directory: Path) -> Path:
    """Version 2.0.0 of the inventory without the relation ``located_in`` (renamed to
    ``stored_in``): the published action ``move_item`` names the old one."""
    target = directory / "ontology" / "2.0.0"
    target.mkdir(parents=True)
    text = (INVENTORY / "1.0.0" / "inventory.yaml").read_text()
    text = text.replace("located_in", "stored_in").replace("version: 1.0.0", "version: 2.0.0")
    (target / "inventory.yaml").write_text(text)
    return target


def fix_action(directory: Path) -> None:
    path = directory / "actions" / MOVE
    path.write_text(path.read_text().replace("located_in", "stored_in"))


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def problem_codes(outcome: Outcome) -> set[tuple[str, str]]:
    key = "action" if outcome.error["code"].startswith("admin.actions") else "function"
    return {(p[key], p["code"]) for p in outcome.error["details"]["problems"]}


# ---- pull and push ------------------------------------------------------------------------


def test_pull_writes_the_published_actions_byte_for_byte(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    before = tree_hash(root)
    target = tmp_path / "work"
    outcome = pull(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    digest = sources_hash(ACTIONS)
    assert outcome.data["actions"] == {"count": 2, "hash": digest}
    assert outcome.data["functions"] is None
    assert [f for f in outcome.data["files"] if f.startswith("actions/")] == [
        "actions/move_item.yaml",
        "actions/retire_item.yaml",
    ]
    for source in ACTIONS.glob("*.yaml"):
        assert (target / "actions" / source.name).read_bytes() == source.read_bytes()
    assert base(target)["actions"] == {"count": 2, "hash": digest}
    assert not (target / "functions").exists()
    assert tree_hash(root) == before
    refused = pull(run_cli, target)
    assert refused.error["code"] == "admin.source_exists"
    assert "actions" in refused.error["details"]["paths"]
    (target / "actions" / "stray.yaml").write_text("not an action\n")
    forced = pull(run_cli, target, "--force")
    assert forced.code == 0, forced.envelope
    assert not (target / "actions" / "stray.yaml").exists()  # the folder was replaced


def test_a_graph_without_actions_pulls_no_folder(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    assert not (target / "actions").exists()
    assert base(target)["actions"] is None
    unchanged = push(run_cli, target)
    assert unchanged.code == 0 and unchanged.data["unchanged"] is True
    assert unchanged.data["actions"] is None


def test_a_base_without_an_actions_key_still_pushes(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    document = base(target)
    del document["actions"]
    (target / BASE).write_text(json.dumps(document))
    assert push(run_cli, target).data["unchanged"] is True


def test_push_publishes_edited_actions_and_records_the_base(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    unchanged = push(run_cli, target)
    assert unchanged.code == 0 and unchanged.data["unchanged"] is True
    assert unchanged.data["actions"]["published"] is False
    path = target / "actions" / MOVE
    path.write_text(path.read_text().replace("Store an item", "Put an item"))
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "reword")
    assert outcome.code == 0, outcome.envelope
    digest = sources_hash(target / "actions")
    assert outcome.data["unchanged"] is False
    assert outcome.data["ontology"]["released"] is False
    assert outcome.data["actions"]["published"] is True
    assert outcome.data["actions"]["to"]["hash"] == digest
    assert outcome.data["actions"]["actions"] == ["move_item", "retire_item"]
    assert outcome.data["functions"] is None
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [["maint.graph_configure"]]
    assert intents[0]["change"]["actions"]["to"] == digest
    assert intents[0]["reason"] == "reword"
    assert base(target)["actions"] == {"count": 2, "hash": digest}
    assert base(target) == outcome.data["source"]
    again = push(run_cli, target)
    assert again.code == 0 and again.data["unchanged"] is True
    assert admin(run_cli, "audit", "verify").code == 0


def test_deleting_every_action_file_unpublishes_the_catalog(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.rmtree(target / "actions")  # a git checkout has no empty folder either
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["actions"]["published"] is True
    assert outcome.data["actions"]["actions"] == []
    assert admin(run_cli, "graph", "show", "example_graph").data["actions"]["count"] == 0
    assert push(run_cli, target).data["unchanged"] is True


def test_dry_run_checks_the_actions_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    (target / "actions" / MOVE).unlink()
    before, written = tree_hash(root), (target / BASE).read_bytes()
    outcome = push(run_cli, target, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["actions"]["published"] is False
    assert outcome.data["actions"]["to"]["count"] == 1
    assert tree_hash(root) == before
    assert (target / BASE).read_bytes() == written


def test_invalid_action_files_stop_a_push_before_any_write(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.1.0")
    path = target / "actions" / MOVE
    path.write_text(path.read_text().replace("inv:located_in", "inv:no_such_relation"))
    (target / "actions" / ".DS_Store").write_text("ignored\n")
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.actions_invalid", refused.envelope
    assert {c for _, c in problem_codes(refused)} == {"action.unknown_relation"}
    assert {n for n, _ in problem_codes(refused)} == {"move_item"}
    assert tree_hash(root) == before  # the release of 1.1.0 did not happen either
    (target / "actions" / "notes.txt").write_text("stray\n")
    path.write_bytes((ACTIONS / MOVE).read_bytes())
    stray = push(run_cli, target)  # a stray file is refused even if nothing else changed
    assert stray.error["code"] == "admin.actions_invalid"
    assert problem_codes(stray) == {("notes.txt", "action.invalid_file")}
    assert tree_hash(root) == before


def test_a_graph_whose_actions_changed_is_a_conflict(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    mine = pulled(run_cli, tmp_path, "mine")
    theirs = pulled(run_cli, tmp_path, "theirs")
    (theirs / "actions" / MOVE).unlink()
    assert push(run_cli, theirs).code == 0
    (mine / "actions" / "retire_item.yaml").unlink()
    before = tree_hash(root)
    refused = push(run_cli, mine)
    assert (refused.code, refused.error["code"]) == (4, "admin.source_conflict")
    details = refused.error["details"]
    assert details["base"]["actions"] == base(mine)["actions"]["hash"]
    assert details["current"]["actions"] == base(theirs)["actions"]["hash"]
    assert tree_hash(root) == before


# ---- releases -----------------------------------------------------------------------------


def test_a_release_that_removes_a_term_an_action_writes_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    source = next_major(tmp_path)
    before = tree_hash(root)
    refused = admin(run_cli, "ontology", "release", "example_graph", str(source))
    assert (refused.code, refused.error["code"]) == (1, "admin.actions_broken")
    assert refused.error["category"] == "refused" and refused.error["hint"] is None
    details = refused.error["details"]
    assert (details["from"], details["to"]) == ("1.0.0", "2.0.0")
    assert details["actions"] == ["move_item"]
    assert {n for n, _ in problem_codes(refused)} == {"move_item"}
    assert "move_item" in refused.error["message"]
    assert "published action would not resolve under 2.0.0" in refused.error["message"]
    assert tree_hash(root) == before
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.0.0"


def test_a_push_of_the_ontology_alone_is_refused_the_same_way(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.actions_broken", refused.envelope
    assert tree_hash(root) == before


def test_the_ontology_and_the_fixed_action_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    fix_action(target)
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "rename the relation")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is True
    assert outcome.data["actions"]["published"] is True
    assert outcome.data["actions"]["to"]["published_against"]["version"] == "2.0.0"
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "2.0.0"
    assert shown["actions"]["hash"] == sources_hash(target / "actions")
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [
        ["maint.ontology_release"],
        ["maint.ontology_pin"],
        ["maint.graph_configure"],
    ]
    assert admin(run_cli, "audit", "verify").code == 0
    assert admin(run_cli, "graph", "check", "example_graph").code == 0
    assert push(run_cli, target).data["unchanged"] is True


def test_the_ontology_and_a_deleted_action_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    (target / "actions" / MOVE).unlink()
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["actions"]["actions"] == ["retire_item"]
    assert admin(run_cli, "graph", "show", "example_graph").data["actions"]["count"] == 1


def test_an_action_that_stays_broken_in_the_push_is_actions_invalid(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    before = tree_hash(root)
    (target / "actions" / "retire_item.yaml").write_text("name: [unclosed\n")
    refused = push(run_cli, target)  # move_item still names located_in
    assert refused.error["code"] == "admin.actions_invalid", refused.envelope
    assert {n for n, _ in problem_codes(refused)} >= {"move_item", "retire_item"}
    assert tree_hash(root) == before


def test_a_minor_that_only_adds_terms_passes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.1.0")
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is True
    assert outcome.data["actions"]["published"] is False  # the files did not change
    assert "admin.actions_affected" not in outcome.warning_codes
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "1.1.0"
    assert shown["actions"]["hash"] == sources_hash(ACTIONS)
    released = admin(run_cli, "ontology", "release", "example_graph", str(INVENTORY / "1.1.0"))
    assert released.code == 0  # already pinned: nothing to check, nothing to do


# ---- ontology diff --graph and graph check ------------------------------------------------


def test_diff_warns_about_the_actions_a_release_would_break(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    source = next_major(tmp_path)
    outcome = admin(run_cli, "ontology", "diff", str(source), "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    assert "admin.actions_affected" in outcome.warning_codes
    assert "admin.functions_affected" not in outcome.warning_codes  # no functions published
    warning = next(w for w in outcome.envelope["warnings"] if w["code"] == "admin.actions_affected")
    assert warning["details"]["actions"] == ["move_item"]
    assert warning["details"]["to"] == "2.0.0"
    quiet = admin(run_cli, "ontology", "diff", str(INVENTORY / "1.1.0"), "--graph", "example_graph")
    assert "admin.actions_affected" not in quiet.warning_codes
    against = admin(run_cli, "ontology", "diff", str(source), "--against", str(INVENTORY / "1.0.0"))
    assert "admin.actions_affected" not in against.warning_codes  # needs a graph


def test_graph_check_lists_the_published_actions(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    clean = admin(run_cli, "graph", "check", "example_graph")
    assert clean.code == 0, clean.envelope
    assert clean.data["actions"] == {"count": 2, "problems": []}
    assert clean.data["functions"] == {"count": 0, "problems": []}
    source = next_major(tmp_path)
    under = admin(run_cli, "graph", "check", "example_graph", "--ontology", str(source))
    assert under.error["code"] == "admin.actions_broken", under.envelope
    assert {n for n, _ in problem_codes(under)} == {"move_item"}


# ---- one namespace across the kinds -------------------------------------------------------


def test_a_function_and_an_action_of_one_name_are_refused_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    function = (FUNCTIONS / "items_in_location.yaml").read_text()
    (target / "functions").mkdir()
    (target / "functions" / MOVE).write_text(function.replace("items_in_location", "move_item"))
    before = tree_hash(root)
    refused = push(run_cli, target)  # the published action move_item holds the name
    assert refused.error["code"] == "admin.functions_invalid", refused.envelope
    assert problem_codes(refused) == {("move_item.yaml", "function.name_taken")}
    assert tree_hash(root) == before
    # A function and an action of one new name in the same push: the function check sees the
    # pushed action names.
    (target / "functions" / MOVE).unlink()
    (target / "functions" / "fresh_name.yaml").write_text(
        function.replace("items_in_location", "fresh_name")
    )
    (target / "actions" / "fresh_name.yaml").write_text(
        (ACTIONS / MOVE).read_text().replace("name: move_item", "name: fresh_name")
    )
    clash = push(run_cli, target)
    assert clash.error["code"] == "admin.functions_invalid", clash.envelope
    assert problem_codes(clash) == {("fresh_name.yaml", "function.name_taken")}
    assert tree_hash(root) == before


def test_a_name_moves_from_functions_to_actions_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(FUNCTIONS))
    assert published.code == 0, published.envelope
    target = pulled(run_cli, tmp_path)
    (target / "functions" / "items_in_location.yaml").unlink()
    moved = (ACTIONS / MOVE).read_text().replace("name: move_item", "name: items_in_location")
    (target / "actions" / "items_in_location.yaml").write_text(moved)
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["functions"]["functions"] == ["items_by_condition"]
    assert "items_in_location" in outcome.data["actions"]["actions"]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert (shown["functions"]["count"], shown["actions"]["count"]) == (1, 3)
    assert admin(run_cli, "graph", "check", "example_graph").code == 0
    assert push(run_cli, target).data["unchanged"] is True
