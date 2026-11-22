"""S: graph-admin graph views publish|show (views plan V9; B6 ``maint.graph_configure``, W.e).

The publish event pair (graph intent plus installation anchor) verifies clean, a re-publish of the
same files changes nothing, an empty folder unpublishes every view, every problem of every view is
reported in one answer, views have a namespace of their own, and views and functions depend on
each other: a view is checked against the published functions and publishing functions is
refused when a published view would break.

The graphs here carry the inventory test ontology; ``tests/fixtures/views/inventory`` holds the
views and ``tests/fixtures/functions/inventory`` the functions they run.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kernel.ports.catalog import sources_hash
from graph_cli.storage.files.audit_log import FileAuditLog

VIEWS = FIXTURES / "views" / "inventory" / "valid"
BROKEN = FIXTURES / "views" / "inventory" / "broken"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    return create_graph(run_cli, "example_graph")


def publish(run_cli: CliRunner, source: Path, *args: str, graph: str = "example_graph") -> Outcome:
    return admin(run_cli, "graph", "views", "publish", graph, str(source), *args)


def publish_functions(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    return admin(run_cli, "graph", "functions", "publish", "example_graph", str(source), *args)


def show(run_cli: CliRunner) -> Outcome:
    return admin(run_cli, "graph", "views", "show", "example_graph")


def folder(tmp_path: Path, *names: str, name: str = "views", source: Path = VIEWS) -> Path:
    """A folder with the named files of ``source`` (none: an empty folder)."""
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


@pytest.fixture
def with_functions(run_cli: CliRunner, graph_id: str) -> None:
    assert publish_functions(run_cli, FUNCTIONS).code == 0


def test_publish_records_the_change_and_verifies_clean(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None
) -> None:
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert "views" not in shown  # a graph without views answers as it did
    assert show(run_cli).data == {"graph_id": graph_id, "slot": None, "views": []}
    outcome = publish(run_cli, VIEWS, "--reason", "first views")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    assert (data["published"], data["dry_run"], data["from"]) == (True, False, None)
    assert data["views"] == ["item_catalog", "items_in_location", "location_overview"]
    digest = digest_of(VIEWS)
    assert data["to"]["hash"] == digest and data["to"]["count"] == 3
    assert data["to"]["published_against"]["version"] == shown["ontology"]["version"]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["views"] == {"count": 3, "hash": digest}
    assert shown["actions"] is None  # the other slots are untouched
    listed = show(run_cli).data
    assert listed["slot"] == data["to"]
    assert [(v["name"], v["takes"], v["default"]) for v in listed["views"]] == [
        ("item_catalog", None, False),
        ("items_in_location", None, False),
        ("location_overview", "inv:Location", True),
    ]
    intent = graph_events(root, graph_id)[-1]
    assert intent["actions"] == ["maint.graph_configure"]
    assert set(intent["change"]) == {"views"}
    assert intent["change"]["views"]["from"] is None
    assert intent["change"]["views"]["to"] == digest
    assert intent["change"]["views"]["compiled_hash"] == data["to"]["compiled_hash"]
    assert intent["reason"] == "first views"
    companion = installation_events(root)[-1]
    assert companion["actions"] == ["maint.graph_configure"]
    assert companion["anchors"] == [
        {"graph_id": graph_id, "seq": intent["seq"], "hash": intent["hash"]}
    ]
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_a_graph_without_views_verifies_clean_and_has_no_views_directory(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    assert admin(run_cli, "audit", "verify").code == 0
    assert publish_functions(run_cli, FUNCTIONS).code == 0
    assert admin(run_cli, "graph", "actions", "publish", "example_graph", str(ACTIONS)).code == 0
    assert not (root / "graphs" / graph_id / "views").exists()
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_republishing_the_same_files_changes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    assert publish(run_cli, VIEWS).code == 0
    before = tree_hash(root)
    again = publish(run_cli, folder(tmp_path, *(p.name for p in VIEWS.glob("*.yaml"))))
    assert again.code == 0 and again.data["published"] is False
    assert again.data["from"] == again.data["to"]
    assert tree_hash(root) == before


def test_republishing_changed_files_names_both_hashes(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    first = publish(run_cli, VIEWS).data["to"]["hash"]
    changed = folder(tmp_path, "location_overview.yaml")
    outcome = publish(run_cli, changed)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["from"]["hash"] == first and outcome.data["to"]["count"] == 1
    assert graph_events(root, graph_id)[-1]["change"]["views"] == {
        "from": first,
        "to": digest_of(changed),
        "compiled_hash": outcome.data["to"]["compiled_hash"],
    }
    assert [v["name"] for v in show(run_cli).data["views"]] == ["location_overview"]
    assert admin(run_cli, "audit", "verify").code == 0


def test_dry_run_checks_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None
) -> None:
    before = tree_hash(root)
    outcome = publish(run_cli, VIEWS, "--dry-run")
    assert outcome.code == 0, outcome.envelope
    assert (outcome.data["published"], outcome.data["dry_run"]) == (False, True)
    assert outcome.data["to"]["count"] == 3
    assert tree_hash(root) == before
    refused = publish(run_cli, BROKEN, "--dry-run")  # the dry run is the check
    assert refused.code == 1 and refused.error["code"] == "admin.views_invalid"
    assert tree_hash(root) == before


def test_an_empty_folder_unpublishes_every_view(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    empty = folder(tmp_path)
    before = tree_hash(root)
    nothing = publish(run_cli, empty)  # nothing published: nothing to unpublish
    assert nothing.code == 0 and nothing.data["published"] is False
    assert tree_hash(root) == before
    first = publish(run_cli, VIEWS).data["to"]["hash"]
    outcome = publish(run_cli, empty)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.data["views"] == [] and outcome.data["to"]["count"] == 0
    assert outcome.data["from"]["hash"] == first
    assert admin(run_cli, "graph", "show", "example_graph").data["views"]["count"] == 0
    assert show(run_cli).data["views"] == []
    assert publish(run_cli, empty).data["published"] is False
    assert admin(run_cli, "audit", "verify").code == 0


def test_invalid_files_list_every_problem(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    broken = folder(tmp_path, "location_overview.yaml", name="broken")
    for name in ("unknown_attribute.yaml", "unknown_function.yaml", "card.yaml"):
        shutil.copy(BROKEN / name, broken)
    (broken / "notes.txt").write_text("not a view\n")
    (broken / "sub").mkdir()
    (broken / "unparsable.yaml").write_text("name: [unclosed\n")
    before = tree_hash(root)
    outcome = publish(run_cli, broken)
    assert outcome.code == 1
    assert outcome.error["code"] == "admin.views_invalid"
    assert outcome.error["category"] == "invalid" and outcome.error["hint"] is None
    problems = outcome.error["details"]["problems"]
    assert {(p["view"], p["code"]) for p in problems} >= {
        ("notes.txt", "view.invalid_file"),
        ("sub", "view.invalid_file"),
        ("unparsable", "view.invalid_yaml"),
        ("unknown_attribute", "view.unknown_attribute"),
        ("unknown_function", "view.unknown_function"),
        ("card", "view.reserved_name"),
    }
    assert all("function" not in p and "action" not in p for p in problems)
    assert not [p for p in problems if p["view"] == "location_overview"]
    assert tree_hash(root) == before


def test_a_view_that_runs_a_function_the_graph_lacks_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    refused = publish(run_cli, VIEWS)
    assert (refused.code, refused.error["code"]) == (1, "admin.views_invalid")
    codes = {(p["view"], p["code"]) for p in refused.error["details"]["problems"]}
    assert codes == {
        ("item_catalog", "view.unknown_function"),
        ("items_in_location", "view.unknown_function"),
        ("location_overview", "view.unknown_function"),
    }
    assert publish_functions(run_cli, FUNCTIONS).code == 0  # positive control
    assert publish(run_cli, VIEWS).code == 0


def test_text_mode_names_the_problems(run_cli: CliRunner, graph_id: str) -> None:
    result = run_cli("graph-admin", "graph", "views", "publish", "example_graph", str(BROKEN))
    assert result.returncode == 1
    assert "admin.views_invalid" in result.stderr and "unknown_attribute" in result.stderr


def test_an_unknown_graph_is_not_found(run_cli: CliRunner, graph_id: str, tmp_path: Path) -> None:
    assert admin(run_cli, "graph", "views", "show", "nothing").error["code"] == "graph.not_found"
    outcome = admin(run_cli, "graph", "views", "publish", "nothing", str(folder(tmp_path)))
    assert outcome.error["code"] == "graph.not_found"


def test_an_archived_graph_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None
) -> None:
    manifest = root / "graphs" / graph_id / "manifest.toml"
    manifest.write_text(manifest.read_text().replace('state = "active"', 'state = "archived"'))
    before = tree_hash(root)
    for dry in ([], ["--dry-run"]):
        outcome = publish(run_cli, VIEWS, *dry)
        assert (outcome.code, outcome.error["code"]) == (1, "graph.archived")
    assert tree_hash(root) == before


def test_a_view_may_be_named_like_a_function_or_an_action(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    """Views have a namespace of their own: items_in_location is a function and a view."""
    assert publish(run_cli, folder(tmp_path, "items_in_location.yaml")).code == 0
    assert admin(run_cli, "graph", "actions", "publish", "example_graph", str(ACTIONS)).code == 0
    assert admin(run_cli, "audit", "verify").code == 0
    names = [v["name"] for v in show(run_cli).data["views"]]
    assert names == ["items_in_location"]


def test_functions_cannot_be_published_when_a_published_view_would_break(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    assert publish(run_cli, VIEWS).code == 0
    before = tree_hash(root)
    only_condition = folder(tmp_path, "items_by_condition.yaml", name="fns", source=FUNCTIONS)
    for dry in ([], ["--dry-run"]):
        refused = publish_functions(run_cli, only_condition, *dry)
        assert (refused.code, refused.error["code"]) == (1, "admin.views_broken"), dry
    details = refused.error["details"]
    assert details["views"] == ["items_in_location", "location_overview"]
    assert {(p["view"], p["code"]) for p in details["problems"]} == {
        ("items_in_location", "view.unknown_function"),
        ("location_overview", "view.unknown_function"),
    }
    assert tree_hash(root) == before  # nothing was written, not even by the dry run
    # the same functions publish once the views that need the missing one are gone
    keep = folder(tmp_path, "item_catalog.yaml", name="only_catalog")
    assert publish(run_cli, keep).code == 0
    assert publish_functions(run_cli, only_condition).code == 0
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_changed_parameter_of_a_function_breaks_the_view_that_binds_it(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    assert publish(run_cli, folder(tmp_path, "items_in_location.yaml")).code == 0
    changed = folder(
        tmp_path, *(p.name for p in FUNCTIONS.glob("*.yaml")), name="fns2", source=FUNCTIONS
    )
    text = (changed / "items_in_location.yaml").read_text()
    (changed / "items_in_location.yaml").write_text(
        text.replace("class: inv:Location", "class: inv:Item").replace(
            "WHERE l.id = $location", "WHERE i.id = $location"
        )
    )
    refused = publish_functions(run_cli, changed)
    assert (refused.code, refused.error["code"]) == (1, "admin.views_broken"), refused.envelope
    assert [p["code"] for p in refused.error["details"]["problems"]] == ["view.type_mismatch"]


def test_unpublishing_the_views_first_lets_the_functions_go(
    run_cli: CliRunner, root: Path, graph_id: str, with_functions: None, tmp_path: Path
) -> None:
    assert publish(run_cli, VIEWS).code == 0
    nothing = folder(tmp_path, name="no_functions")
    assert publish_functions(run_cli, nothing).error["code"] == "admin.views_broken"
    assert publish(run_cli, folder(tmp_path, name="no_views")).code == 0
    assert publish_functions(run_cli, nothing).code == 0
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_deprecated_function_behind_a_view_is_a_warning_not_a_refusal(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    functions = folder(tmp_path, "items_in_location.yaml", name="fns", source=FUNCTIONS)
    text = (functions / "items_in_location.yaml").read_text()
    (functions / "items_in_location.yaml").write_text(
        text.replace("featured: true", "featured: true\nstatus: deprecated")
    )
    assert publish_functions(run_cli, functions).code == 0
    outcome = publish(run_cli, folder(tmp_path, "location_overview.yaml"))
    assert outcome.code == 0 and outcome.data["published"] is True
    assert outcome.warning_codes == ["view.function_deprecated"]
    assert outcome.warnings[0]["details"]["function"] == "items_in_location"
    dry = publish(run_cli, folder(tmp_path, "location_overview.yaml"), "--dry-run")
    assert dry.warning_codes == ["view.function_deprecated"]  # also when nothing changes
    assert admin(run_cli, "audit", "verify").code == 0


def test_a_function_a_published_view_runs_is_exempt_from_the_single_ref_warning(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    functions = folder(tmp_path, "items_in_location.yaml", name="fns", source=FUNCTIONS)
    first = publish_functions(run_cli, functions)
    assert first.warning_codes == ["admin.function_single_ref"]  # no view runs it yet
    assert publish(run_cli, folder(tmp_path, "location_overview.yaml")).code == 0
    again = publish_functions(run_cli, functions, "--dry-run")
    assert again.code == 0 and again.warnings == []  # a view hands it one entity


def test_publishing_actions_and_functions_without_views_answers_as_before(
    run_cli: CliRunner, root: Path, graph_id: str
) -> None:
    outcome = publish_functions(run_cli, FUNCTIONS)
    assert outcome.code == 0 and outcome.data["published"] is True
    assert "views" not in outcome.data
