"""S: functions in the maintainer workspace and in ontology releases (K5, K7).

``graph pull`` writes ``functions/<name>.yaml``; ``graph push`` checks the
files against the ontology the graph will have and publishes them after the
release, so a release that breaks a function is refused and one push can carry
the ontology with the fixed or deleted function files.
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
from graph_cli.kernel.ports.catalog import functions_hash
from graph_cli.storage.files.audit_log import FileAuditLog

INVENTORY = ONTOLOGIES / "inventory"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
BASE = ".graph_cli_source.json"
LOCATION = "items_in_location.yaml"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    graph_id = create_graph(run_cli, "example_graph", version="1.0.0")
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(FUNCTIONS))
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
    return functions_hash({p.name: p.read_bytes() for p in directory.glob("*.yaml")})


def next_major(directory: Path) -> Path:
    """Version 2.0.0 of the inventory without the relation ``located_in`` (renamed to
    ``stored_in``): the published function ``items_in_location`` names the old one."""
    target = directory / "ontology" / "2.0.0"
    target.mkdir(parents=True)
    text = (INVENTORY / "1.0.0" / "inventory.yaml").read_text()
    text = text.replace("located_in", "stored_in").replace("version: 1.0.0", "version: 2.0.0")
    (target / "inventory.yaml").write_text(text)
    return target


def fix_function(directory: Path) -> None:
    path = directory / "functions" / LOCATION
    path.write_text(path.read_text().replace("located_in", "stored_in"))


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def problem_codes(outcome: Outcome) -> set[tuple[str, str]]:
    return {(p["function"], p["code"]) for p in outcome.error["details"]["problems"]}


# ---- pull and push ------------------------------------------------------------------------


def test_pull_writes_the_published_functions_byte_for_byte(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    before = tree_hash(root)
    target = tmp_path / "work"
    outcome = pull(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    digest = sources_hash(FUNCTIONS)
    assert outcome.data["functions"] == {"count": 2, "hash": digest}
    assert [f for f in outcome.data["files"] if f.startswith("functions/")] == [
        "functions/items_by_condition.yaml",
        "functions/items_in_location.yaml",
    ]
    for source in FUNCTIONS.glob("*.yaml"):
        assert (target / "functions" / source.name).read_bytes() == source.read_bytes()
    assert base(target)["functions"] == {"count": 2, "hash": digest}
    assert tree_hash(root) == before
    refused = pull(run_cli, target)
    assert refused.error["code"] == "admin.source_exists"
    assert "functions" in refused.error["details"]["paths"]
    (target / "functions" / "stray.yaml").write_text("not a function\n")
    forced = pull(run_cli, target, "--force")
    assert forced.code == 0, forced.envelope
    assert not (target / "functions" / "stray.yaml").exists()  # the folder was replaced


def test_a_graph_without_functions_pulls_no_folder(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    assert not (target / "functions").exists()
    assert base(target)["functions"] is None
    unchanged = push(run_cli, target)
    assert unchanged.code == 0 and unchanged.data["unchanged"] is True
    assert unchanged.data["functions"] is None


def test_a_base_without_a_functions_key_still_pushes(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    document = base(target)
    del document["functions"]
    (target / BASE).write_text(json.dumps(document))
    assert push(run_cli, target).data["unchanged"] is True


def test_push_publishes_edited_functions_and_records_the_base(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    unchanged = push(run_cli, target)
    assert unchanged.code == 0 and unchanged.data["unchanged"] is True
    assert unchanged.data["functions"]["published"] is False
    path = target / "functions" / LOCATION
    path.write_text(path.read_text().replace("Items stored in a location.", "Items in a place."))
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "reword")
    assert outcome.code == 0, outcome.envelope
    digest = sources_hash(target / "functions")
    assert outcome.data["unchanged"] is False
    assert outcome.data["ontology"]["released"] is False
    assert outcome.data["functions"]["published"] is True
    assert outcome.data["functions"]["to"]["hash"] == digest
    assert outcome.data["functions"]["functions"] == ["items_by_condition", "items_in_location"]
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [["maint.graph_configure"]]
    assert intents[0]["change"]["functions"]["to"] == digest
    assert intents[0]["reason"] == "reword"
    assert base(target)["functions"] == {"count": 2, "hash": digest}
    assert base(target) == outcome.data["source"]
    again = push(run_cli, target)
    assert again.code == 0 and again.data["unchanged"] is True
    assert admin(run_cli, "audit", "verify").code == 0


def test_deleting_every_function_file_unpublishes_the_catalog(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.rmtree(target / "functions")  # a git checkout has no empty folder either
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["functions"]["published"] is True
    assert outcome.data["functions"]["functions"] == []
    assert admin(run_cli, "graph", "show", "example_graph").data["functions"]["count"] == 0
    assert push(run_cli, target).data["unchanged"] is True


def test_dry_run_checks_the_functions_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    (target / "functions" / LOCATION).unlink()
    before, written = tree_hash(root), (target / BASE).read_bytes()
    outcome = push(run_cli, target, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["functions"]["published"] is False
    assert outcome.data["functions"]["to"]["count"] == 1
    assert tree_hash(root) == before
    assert (target / BASE).read_bytes() == written


def test_invalid_function_files_stop_a_push_before_any_write(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.1.0")
    path = target / "functions" / LOCATION
    path.write_text(path.read_text().replace("located_in", "no_such_relation"))
    (target / "functions" / ".DS_Store").write_text("ignored\n")
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.functions_invalid", refused.envelope
    assert problem_codes(refused) == {("items_in_location", "term.unknown")}
    assert tree_hash(root) == before  # the release of 1.1.0 did not happen either
    (target / "functions" / "notes.txt").write_text("stray\n")
    path.write_bytes((FUNCTIONS / LOCATION).read_bytes())
    stray = push(run_cli, target)  # a stray file is refused even if nothing else changed
    assert stray.error["code"] == "admin.functions_invalid"
    assert problem_codes(stray) == {("notes.txt", "function.invalid_file")}
    assert tree_hash(root) == before


def test_a_graph_whose_functions_changed_is_a_conflict(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    mine = pulled(run_cli, tmp_path, "mine")
    theirs = pulled(run_cli, tmp_path, "theirs")
    (theirs / "functions" / LOCATION).unlink()
    assert push(run_cli, theirs).code == 0
    (mine / "functions" / "items_by_condition.yaml").unlink()
    before = tree_hash(root)
    refused = push(run_cli, mine)
    assert (refused.code, refused.error["code"]) == (4, "admin.source_conflict")
    details = refused.error["details"]
    assert details["base"]["functions"] == base(mine)["functions"]["hash"]
    assert details["current"]["functions"] == base(theirs)["functions"]["hash"]
    assert tree_hash(root) == before


# ---- releases -----------------------------------------------------------------------------


def test_a_release_that_removes_a_term_a_function_names_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    source = next_major(tmp_path)
    before = tree_hash(root)
    refused = admin(run_cli, "ontology", "release", "example_graph", str(source))
    assert (refused.code, refused.error["code"]) == (1, "admin.functions_broken")
    assert refused.error["category"] == "refused" and refused.error["hint"] is None
    details = refused.error["details"]
    assert (details["from"], details["to"]) == ("1.0.0", "2.0.0")
    assert details["functions"] == ["items_in_location"]
    assert problem_codes(refused) == {
        ("items_in_location", "term.unknown"),
        ("items_in_location", "function.stale"),  # the stored query text does not resolve either
    }
    assert "items_in_location" in refused.error["message"]
    assert tree_hash(root) == before
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.0.0"


def test_a_push_of_the_ontology_alone_is_refused_the_same_way(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.functions_broken", refused.envelope
    assert tree_hash(root) == before


def test_the_ontology_and_the_fixed_function_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    fix_function(target)
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "rename the relation")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is True
    assert outcome.data["functions"]["published"] is True
    assert outcome.data["functions"]["to"]["published_against"]["version"] == "2.0.0"
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "2.0.0"
    assert shown["functions"]["hash"] == sources_hash(target / "functions")
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [
        ["maint.ontology_release"],
        ["maint.ontology_pin"],
        ["maint.graph_configure"],
    ]
    assert admin(run_cli, "audit", "verify").code == 0
    assert admin(run_cli, "graph", "check", "example_graph").code == 0
    assert push(run_cli, target).data["unchanged"] is True


def test_the_ontology_and_a_deleted_function_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    (target / "functions" / LOCATION).unlink()
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["functions"]["functions"] == ["items_by_condition"]
    assert admin(run_cli, "graph", "show", "example_graph").data["functions"]["count"] == 1


def test_a_function_that_stays_broken_in_the_push_is_functions_invalid(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    next_major(target)
    (target / "functions" / "items_by_condition.yaml").write_text("name: [unclosed\n")
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.functions_invalid", refused.envelope
    assert {p["function"] for p in refused.error["details"]["problems"]} >= {
        "items_by_condition",
        "items_in_location",
    }
    assert tree_hash(root) == before


def test_a_minor_that_only_adds_terms_passes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.1.0")
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is True
    assert outcome.data["functions"]["published"] is False  # the files did not change
    assert "admin.functions_affected" not in outcome.warning_codes
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "1.1.0"
    assert shown["functions"]["hash"] == sources_hash(FUNCTIONS)
    released = admin(run_cli, "ontology", "release", "example_graph", str(INVENTORY / "1.1.0"))
    assert released.code == 0  # already pinned: nothing to check, nothing to do


# ---- ontology diff --graph and graph check ------------------------------------------------


def test_diff_warns_about_the_functions_a_release_would_break(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    source = next_major(tmp_path)
    outcome = admin(run_cli, "ontology", "diff", str(source), "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    assert "admin.functions_affected" in outcome.warning_codes
    warning = next(
        w for w in outcome.envelope["warnings"] if w["code"] == "admin.functions_affected"
    )
    assert warning["details"]["functions"] == ["items_in_location"]
    assert warning["details"]["to"] == "2.0.0"
    quiet = admin(run_cli, "ontology", "diff", str(INVENTORY / "1.1.0"), "--graph", "example_graph")
    assert "admin.functions_affected" not in quiet.warning_codes
    against = admin(run_cli, "ontology", "diff", str(source), "--against", str(INVENTORY / "1.0.0"))
    assert "admin.functions_affected" not in against.warning_codes  # needs a graph


def test_graph_check_lists_the_published_functions(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    clean = admin(run_cli, "graph", "check", "example_graph")
    assert clean.code == 0, clean.envelope
    assert clean.data["functions"] == {"count": 2, "problems": []}
    source = next_major(tmp_path)
    under = admin(run_cli, "graph", "check", "example_graph", "--ontology", str(source))
    assert under.error["code"] == "admin.functions_broken", under.envelope
    assert problem_codes(under) == {
        ("items_in_location", "term.unknown"),
        ("items_in_location", "function.stale"),
    }
