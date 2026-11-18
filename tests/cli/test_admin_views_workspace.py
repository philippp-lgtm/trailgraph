"""S: views in the maintainer workspace and in ontology releases (views plan V9, step 5).

``graph pull`` writes ``views/<name>.yaml``; ``graph push`` checks the files against the ontology
and the functions the graph will have and publishes them last, so a release that breaks a view is
refused and one push can carry the ontology with the fixed or deleted view files, or a function
and the view that used it. A graph without views answers as it did before views existed.
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
VIEWS = FIXTURES / "views" / "inventory" / "valid"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
BASE = ".graph_cli_source.json"
OVERVIEW = "location_overview.yaml"
CODE_LINES = (
    "      code:\n"
    "        summary: Short unique code printed on the location.\n"
    "        type: string\n"
    "        required: true\n"
    "        unique: true\n"
    "        mutability: immutable\n"
)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    """A graph with the inventory functions and views published (1.0.0)."""
    graph_id = create_graph(run_cli, "example_graph", version="1.0.0")
    functions = admin(run_cli, "graph", "functions", "publish", "example_graph", str(FUNCTIONS))
    assert functions.code == 0, functions.envelope
    published = admin(run_cli, "graph", "views", "publish", "example_graph", str(VIEWS))
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


def removing_location_code(directory: Path) -> Path:
    """Version 2.0.0 of the inventory without ``Location.code``, which the published view
    ``location_overview`` names."""
    target = directory / "ontology" / "2.0.0"
    target.mkdir(parents=True)
    text = (INVENTORY / "1.0.0" / "inventory.yaml").read_text()
    assert CODE_LINES in text
    text = text.replace(CODE_LINES, "").replace("version: 1.0.0", "version: 2.0.0")
    (target / "inventory.yaml").write_text(text)
    return target


def fix_view(directory: Path) -> None:
    path = directory / "views" / OVERVIEW
    path.write_text(path.read_text().replace("fields: [name, code]", "fields: [name]"))


def graph_events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def problem_codes(outcome: Outcome) -> set[tuple[str, str]]:
    return {(p["view"], p["code"]) for p in outcome.error["details"]["problems"]}


# ---- pull and push ------------------------------------------------------------------------


def test_pull_writes_the_published_views_byte_for_byte(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    before = tree_hash(root)
    target = tmp_path / "work"
    outcome = pull(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    digest = sources_hash(VIEWS)
    assert outcome.data["views"] == {"count": 3, "hash": digest}
    assert [f for f in outcome.data["files"] if f.startswith("views/")] == [
        "views/item_catalog.yaml",
        "views/items_in_location.yaml",
        "views/location_overview.yaml",
    ]
    for source in VIEWS.glob("*.yaml"):
        assert (target / "views" / source.name).read_bytes() == source.read_bytes()
    assert base(target)["views"] == {"count": 3, "hash": digest}
    assert tree_hash(root) == before
    refused = pull(run_cli, target)
    assert refused.error["code"] == "admin.source_exists"
    assert "views" in refused.error["details"]["paths"]
    (target / "views" / "stray.yaml").write_text("not a view\n")
    forced = pull(run_cli, target, "--force")
    assert forced.code == 0, forced.envelope
    assert not (target / "views" / "stray.yaml").exists()  # the folder was replaced


def test_a_graph_without_views_pulls_and_pushes_as_before(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    assert not (target / "views").exists()
    assert "views" not in base(target)  # the base of such a graph is what it always was
    pull_again = pull(run_cli, tmp_path / "again")
    assert "views" not in pull_again.data
    unchanged = push(run_cli, target)
    assert unchanged.code == 0 and unchanged.data["unchanged"] is True
    assert "views" not in unchanged.data and "views" not in unchanged.data["source"]
    assert "views" not in base(target)
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0 and "views" not in checked.data


def test_a_workspace_without_views_publishes_nothing_for_a_graph_that_has_none(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    (target / "views").mkdir()
    assert push(run_cli, target).data["unchanged"] is True  # an empty folder is a missing one
    shutil.copy(VIEWS / "item_catalog.yaml", target / "views")
    refused = push(run_cli, target)  # the function the view runs is not published
    assert (refused.code, refused.error["code"]) == (1, "admin.views_invalid")
    assert problem_codes(refused) == {("item_catalog", "view.unknown_function")}


def test_push_publishes_edited_views_and_records_the_base(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    unchanged = push(run_cli, target)
    assert unchanged.code == 0 and unchanged.data["unchanged"] is True
    assert unchanged.data["views"]["published"] is False
    path = target / "views" / OVERVIEW
    path.write_text(path.read_text().replace("A location with", "A place with"))
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "reword")
    assert outcome.code == 0, outcome.envelope
    digest = sources_hash(target / "views")
    assert outcome.data["unchanged"] is False
    assert outcome.data["ontology"]["released"] is False
    assert outcome.data["views"]["published"] is True
    assert outcome.data["views"]["to"]["hash"] == digest
    assert outcome.data["views"]["views"] == [
        "item_catalog",
        "items_in_location",
        "location_overview",
    ]
    assert outcome.data["functions"] is None or outcome.data["functions"]["published"] is False
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [["maint.graph_configure"]]
    assert intents[0]["change"]["views"]["to"] == digest
    assert intents[0]["reason"] == "reword"
    assert base(target)["views"] == {"count": 3, "hash": digest}
    assert base(target) == outcome.data["source"]
    again = push(run_cli, target)
    assert again.code == 0 and again.data["unchanged"] is True
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_base_without_a_views_key_still_pushes(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A workspace pulled before views existed has no ``views`` key in its base."""
    create_graph(run_cli, "example_graph", version="1.0.0")
    target = pulled(run_cli, tmp_path)
    assert push(run_cli, target).data["unchanged"] is True


def test_deleting_every_view_file_unpublishes_the_catalog(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.rmtree(target / "views")  # a git checkout has no empty folder either
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["views"]["published"] is True
    assert outcome.data["views"]["views"] == []
    assert admin(run_cli, "graph", "show", "example_graph").data["views"]["count"] == 0
    again = push(run_cli, target)
    assert again.data["unchanged"] is True
    assert again.data["views"]["views"] == []  # the key follows the slot, not the folder
    assert admin(run_cli, "audit", "verify").code == 0


def test_dry_run_checks_the_views_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    (target / "views" / OVERVIEW).unlink()
    before, written = tree_hash(root), (target / BASE).read_bytes()
    outcome = push(run_cli, target, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["views"]["published"] is False
    assert outcome.data["views"]["to"]["count"] == 2
    assert tree_hash(root) == before
    assert (target / BASE).read_bytes() == written


def test_invalid_view_files_stop_a_push_before_any_write(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.1.0")
    path = target / "views" / OVERVIEW
    path.write_text(path.read_text().replace("fields: [name, code]", "fields: [name, no_such]"))
    (target / "views" / ".DS_Store").write_text("ignored\n")
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.views_invalid", refused.envelope
    assert problem_codes(refused) == {("location_overview", "view.unknown_attribute")}
    assert tree_hash(root) == before  # the release of 1.1.0 did not happen either
    (target / "views" / "notes.txt").write_text("stray\n")
    path.write_bytes((VIEWS / OVERVIEW).read_bytes())
    stray = push(run_cli, target)  # a stray file is refused even if nothing else changed
    assert stray.error["code"] == "admin.views_invalid"
    assert problem_codes(stray) == {("notes.txt", "view.invalid_file")}
    assert tree_hash(root) == before


def test_a_graph_whose_views_changed_is_a_conflict(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    mine = pulled(run_cli, tmp_path, "mine")
    theirs = pulled(run_cli, tmp_path, "theirs")
    (theirs / "views" / OVERVIEW).unlink()
    assert push(run_cli, theirs).code == 0
    (mine / "views" / "item_catalog.yaml").unlink()
    before = tree_hash(root)
    refused = push(run_cli, mine)
    assert (refused.code, refused.error["code"]) == (4, "admin.source_conflict")
    details = refused.error["details"]
    assert details["base"]["views"] == base(mine)["views"]["hash"]
    assert details["current"]["views"] == base(theirs)["views"]["hash"]
    assert tree_hash(root) == before


def test_the_conflict_of_a_graph_without_views_names_no_views(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph", version="1.0.0")
    mine = pulled(run_cli, tmp_path, "mine")
    theirs = pulled(run_cli, tmp_path, "theirs")
    (theirs / "skill").mkdir(exist_ok=True)
    shutil.copytree(
        FIXTURES / "skills" / "template" / "graph-example-graph",
        theirs / "skill" / "graph-example-graph",
    )
    assert push(run_cli, theirs).code == 0
    (mine / "ontology" / "1.0.0" / "inventory.yaml").touch()
    shutil.copytree(INVENTORY / "1.1.0", mine / "ontology" / "1.1.0")
    refused = push(run_cli, mine)
    assert refused.error["code"] == "admin.source_conflict", refused.envelope
    assert "views" not in refused.error["details"]["base"]
    assert "views" not in refused.error["details"]["current"]


# ---- releases -----------------------------------------------------------------------------


def test_a_release_that_removes_a_term_a_view_names_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    source = removing_location_code(tmp_path)
    before = tree_hash(root)
    refused = admin(run_cli, "ontology", "release", "example_graph", str(source))
    assert (refused.code, refused.error["code"]) == (1, "admin.views_broken")
    assert refused.error["category"] == "refused" and refused.error["hint"] is None
    details = refused.error["details"]
    assert (details["from"], details["to"]) == ("1.0.0", "2.0.0")
    assert details["views"] == ["location_overview"]
    assert problem_codes(refused) == {("location_overview", "view.unknown_attribute")}
    assert "published view would not resolve under 2.0.0" in refused.error["message"]
    assert tree_hash(root) == before
    assert admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"] == "1.0.0"
    dry = admin(run_cli, "ontology", "release", "example_graph", str(source), "--dry-run")
    assert dry.error["code"] == "admin.views_broken"


def test_a_push_of_the_ontology_alone_is_refused_the_same_way(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    removing_location_code(target)
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.views_broken", refused.envelope
    assert tree_hash(root) == before


def test_the_ontology_and_the_fixed_view_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    removing_location_code(target)
    fix_view(target)
    events = len(graph_events(root, graph_id))
    outcome = push(run_cli, target, "--reason", "drop the code")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is True
    assert outcome.data["views"]["published"] is True
    assert outcome.data["views"]["to"]["published_against"]["version"] == "2.0.0"
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "2.0.0"
    assert shown["views"]["hash"] == sources_hash(target / "views")
    intents = graph_events(root, graph_id)[events:]
    assert [e["actions"] for e in intents] == [
        ["maint.ontology_release"],
        ["maint.ontology_pin"],
        ["maint.graph_configure"],
    ]
    assert admin(run_cli, "audit", "verify").code == 0
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.data["views"] == {"count": 3, "problems": []}
    assert push(run_cli, target).data["unchanged"] is True


def test_the_ontology_and_a_deleted_view_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    removing_location_code(target)
    (target / "views" / OVERVIEW).unlink()
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["views"]["views"] == ["item_catalog", "items_in_location"]
    assert admin(run_cli, "graph", "show", "example_graph").data["views"]["count"] == 2


def test_a_view_that_stays_broken_in_the_push_is_views_invalid(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    removing_location_code(target)
    before = tree_hash(root)
    (target / "views" / "item_catalog.yaml").write_text("name: [unclosed\n")
    refused = push(run_cli, target)  # location_overview still names the code
    assert refused.error["code"] == "admin.views_invalid", refused.envelope
    assert {n for n, _ in problem_codes(refused)} >= {"location_overview", "item_catalog"}
    assert tree_hash(root) == before


def test_a_minor_that_only_adds_terms_passes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    shutil.copytree(INVENTORY / "1.1.0", target / "ontology" / "1.1.0")
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["ontology"]["pinned"] is True
    assert outcome.data["views"]["published"] is False  # the files did not change
    assert "admin.views_affected" not in outcome.warning_codes
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["ontology"]["version"] == "1.1.0"
    assert shown["views"]["hash"] == sources_hash(VIEWS)
    assert admin(run_cli, "graph", "check", "example_graph").code == 0


# ---- functions and views in one push ------------------------------------------------------


def test_a_push_that_removes_a_function_and_the_view_using_it_passes(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    (target / "functions" / "items_in_location.yaml").unlink()
    before = tree_hash(root)
    refused = push(run_cli, target)  # two published views still run it
    assert refused.error["code"] == "admin.views_broken", refused.envelope
    assert refused.error["details"]["views"] == ["items_in_location", "location_overview"]
    assert tree_hash(root) == before
    (target / "views" / "items_in_location.yaml").unlink()
    still = push(run_cli, target)  # the pushed location_overview still runs it
    assert still.error["code"] == "admin.views_invalid", still.envelope
    assert problem_codes(still) == {("location_overview", "view.unknown_function")}
    assert tree_hash(root) == before
    (target / "views" / OVERVIEW).unlink()
    outcome = push(run_cli, target, "--reason", "retire the function")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["functions"]["functions"] == ["items_by_condition"]
    assert outcome.data["views"]["views"] == ["item_catalog"]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert (shown["functions"]["count"], shown["views"]["count"]) == (1, 1)
    assert admin(run_cli, "graph", "check", "example_graph").code == 0
    assert admin(run_cli, "audit", "verify").code == 0
    assert push(run_cli, target).data["unchanged"] is True


def test_a_new_function_and_the_view_that_runs_it_go_in_one_push(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """The views are checked against the functions the graph will have, not the published ones."""
    target = pulled(run_cli, tmp_path)
    added = (target / "functions" / "items_by_condition.yaml").read_text()
    function = target / "functions" / "worn_items.yaml"
    function.write_text(added.replace("name: items_by_condition", "name: worn_items"))
    view = (VIEWS / "items_in_location.yaml").read_text()
    (target / "views" / "worn.yaml").write_text(
        view.replace("name: items_in_location", "name: worn")
        .replace("function: items_in_location", "function: worn_items")
        .replace("args: {location: $location}", "args: {condition: worn}")
        .replace("params:\n  location:\n    type: ref\n    class: inv:Location\n", "")
        .replace("    summary: The location to list.\n", "")
    )
    function.rename(tmp_path / "worn_items.yaml")  # the view alone: the function does not exist
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.views_invalid", refused.envelope
    assert problem_codes(refused) == {("worn", "view.unknown_function")}
    assert tree_hash(root) == before
    (tmp_path / "worn_items.yaml").rename(function)
    outcome = push(run_cli, target)
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["functions"]["published"] is True
    assert outcome.data["views"]["published"] is True
    assert outcome.data["views"]["views"][-1] == "worn"
    assert admin(run_cli, "graph", "check", "example_graph").code == 0
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_function_name_clash_is_refused_before_a_view_the_release_breaks(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """The functions are judged first: a function and an action of one new name in the push are
    refused with their own problem, not hidden behind the view the release breaks."""
    target = pulled(run_cli, tmp_path)
    removing_location_code(target)
    (target / "actions").mkdir()
    action = (FIXTURES / "actions" / "inventory" / "valid" / "move_item.yaml").read_text()
    (target / "actions" / "fresh_name.yaml").write_text(
        action.replace("name: move_item", "name: fresh_name")
    )
    function = (FUNCTIONS / "items_by_condition.yaml").read_text()
    (target / "functions" / "fresh_name.yaml").write_text(
        function.replace("name: items_by_condition", "name: fresh_name")
    )
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.functions_invalid", refused.envelope
    codes = {p["code"] for p in refused.error["details"]["problems"]}
    assert codes == {"function.name_taken"}
    assert tree_hash(root) == before


def test_a_changed_function_parameter_in_a_push_breaks_the_published_view(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    target = pulled(run_cli, tmp_path)
    path = target / "functions" / "items_in_location.yaml"
    path.write_text(
        path.read_text()
        .replace("class: inv:Location", "class: inv:Item")
        .replace("WHERE l.id = $location", "WHERE i.id = $location")
    )
    before = tree_hash(root)
    refused = push(run_cli, target)
    assert refused.error["code"] == "admin.views_broken", refused.envelope
    assert tree_hash(root) == before


# ---- ontology diff --graph and graph check ------------------------------------------------


def test_diff_warns_about_the_views_a_release_would_break(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    source = removing_location_code(tmp_path)
    outcome = admin(run_cli, "ontology", "diff", str(source), "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    assert "admin.views_affected" in outcome.warning_codes
    assert "admin.functions_affected" not in outcome.warning_codes
    warning = next(w for w in outcome.envelope["warnings"] if w["code"] == "admin.views_affected")
    assert warning["details"]["views"] == ["location_overview"]
    assert warning["details"]["to"] == "2.0.0"
    quiet = admin(run_cli, "ontology", "diff", str(INVENTORY / "1.1.0"), "--graph", "example_graph")
    assert "admin.views_affected" not in quiet.warning_codes
    against = admin(run_cli, "ontology", "diff", str(source), "--against", str(INVENTORY / "1.0.0"))
    assert "admin.views_affected" not in against.warning_codes  # needs a graph


def test_graph_check_lists_the_published_views(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    clean = admin(run_cli, "graph", "check", "example_graph")
    assert clean.code == 0, clean.envelope
    assert clean.data["views"] == {"count": 3, "problems": []}
    assert clean.data["functions"] == {"count": 2, "problems": []}
    source = removing_location_code(tmp_path)
    under = admin(run_cli, "graph", "check", "example_graph", "--ontology", str(source))
    assert under.error["code"] == "admin.views_broken", under.envelope
    assert problem_codes(under) == {("location_overview", "view.unknown_attribute")}
