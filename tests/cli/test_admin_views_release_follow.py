"""S: published views across a release that renames terms (migration plan M8, views plan V9).

The inventory series with ``inv:Supplier`` moved to the module ``sup``: views that name the class
with a bare name are compiled again from their sources and published with the release (the same
sources hash, a new compiled hash, ``audit verify`` clean); views that name the old CURIE block
until a push brings them fixed. The views also depend on the functions their ``rows`` sections run,
which a push may change in the same release.
"""

from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.moved_supplier import (
    BARE_FUNCTION,
    BARE_VIEW,
    CURIE_FUNCTION,
    CURIE_VIEW,
    INVENTORY,
    moved_source,
    write_folder,
)
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@pytest.fixture
def graph_id(run_cli: CliRunner, root: Path) -> str:
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")
    )
    assert created.code == 0, created.envelope
    graph_id: str = created.data["id"]
    return graph_id


def release(run_cli: CliRunner, source: Path, *extra: str) -> Outcome:
    return admin(
        run_cli,
        "ontology",
        "release",
        "example_graph",
        str(source),
        "--module",
        "inventory",
        *extra,
    )


def publish(run_cli: CliRunner, kind: str, folder: Path) -> None:
    published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


def scene(run_cli: CliRunner, tmp_path: Path, views: dict[str, str]) -> None:
    """The bare function and the given views, published against 1.1.0."""
    publish(
        run_cli,
        "functions",
        write_folder(tmp_path, "functions", {"suppliers_by_name": BARE_FUNCTION}),
    )
    publish(run_cli, "views", write_folder(tmp_path, "views", views))


def pin(run_cli: CliRunner) -> str:
    version: str = admin(run_cli, "graph", "show", "example_graph").data["ontology"]["version"]
    return version


def events(root: Path, graph_id: str) -> list[dict[str, Any]]:
    report = verify_stream(
        FileAuditLog(root).read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    assert report.findings == ()
    return [dict(event) for event in report.events]


def audit_errors(run_cli: CliRunner) -> int:
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    errors: int = verified.data["counts"]["error"]
    return errors


def views_slot(run_cli: CliRunner) -> dict[str, Any]:
    slot: dict[str, Any] = admin(run_cli, "graph", "views", "show", "example_graph").data["slot"]
    return slot


def test_views_with_bare_names_are_published_again_with_the_release(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW})
    before = views_slot(run_cli)
    before_functions = admin(run_cli, "graph", "functions", "show", "example_graph").data["slot"]
    released = release(run_cli, moved_source(tmp_path), "--reason", "move the suppliers")
    assert released.code == 0, released.envelope
    again = released.data["republished"]
    assert again["views"]["names"] == ["supplier_catalog"]
    assert again["functions"]["names"] == ["suppliers_by_name"]
    assert "actions" not in again  # nothing published
    assert again["views"]["compiled_hash"]["from"] == before["compiled_hash"]
    after = views_slot(run_cli)
    assert after["hash"] == before["hash"]  # the same sources
    assert (
        after["compiled_hash"] == again["views"]["compiled_hash"]["to"] != before["compiled_hash"]
    )
    assert after["published_against"]["version"] == "2.0.0"
    after_functions = admin(run_cli, "graph", "functions", "show", "example_graph").data["slot"]
    assert after_functions["hash"] == before_functions["hash"]
    # nothing is left to republish: the graph checks clean and a second release is a no-op
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.data["views"] == {"count": 1, "problems": []}
    assert "republished" not in checked.data
    assert release(run_cli, moved_source(tmp_path, "again")).data["pinned"] is False
    # the audit trail shows two publishes of the same sources, with the reason
    log = events(root, graph_id)
    publishes = [
        e for e in log if "views" in e.get("change", {}) and "from" in e["change"]["views"]
    ]
    last = publishes[-1]["change"]["views"]
    assert last["from"] == last["to"] == before["hash"]
    assert last["compiled_hash"] == after["compiled_hash"]
    assert publishes[-1]["reason"] == "move the suppliers"
    assert audit_errors(run_cli) == 0


def test_a_dry_run_shows_the_republish_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW})
    source = moved_source(tmp_path)
    before = tree_hash(root)
    dry = release(run_cli, source, "--dry-run")
    assert dry.code == 0, dry.envelope
    assert dry.data["republished"]["views"]["names"] == ["supplier_catalog"]
    assert dry.data["pinned"] is False
    assert tree_hash(root) == before


def test_views_with_old_curies_block_the_release(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW, "supplier_exact": CURIE_VIEW})
    source = moved_source(tmp_path)
    before = tree_hash(root)
    refused = release(run_cli, source)
    assert (refused.code, refused.error["code"]) == (1, "admin.views_broken")
    assert refused.error["details"]["views"] == ["supplier_exact"]
    assert tree_hash(root) == before and pin(run_cli) == "1.1.0"
    # the problem says what the old term is called under the release (its hop is not pinned yet)
    problems = refused.error["details"]["problems"]
    assert {p["view"] for p in problems} == {"supplier_exact"}
    assert any(
        "inv:Supplier was renamed to sup:Supplier in 2.0.0" in p["message"] for p in problems
    )
    # the same in the check and diff previews
    checked = admin(
        run_cli,
        "graph",
        "check",
        "example_graph",
        "--ontology",
        str(source),
        "--module",
        "inventory",
    )
    assert checked.error["code"] == "admin.views_broken"
    shown = admin(
        run_cli,
        "ontology",
        "diff",
        str(source),
        "--module",
        "inventory",
        "--graph",
        "example_graph",
    )
    assert shown.warning_codes.count("admin.views_affected") == 1
    [reference] = shown.data["references"]
    named = {v["name"]: v["explicit"] for v in reference["views"]}
    assert named == {"supplier_catalog": False, "supplier_exact": True}
    assert shown.data["republished"].keys() == {"functions"}  # the catalog with a blocker stays


def test_a_push_brings_the_fixed_view(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW, "supplier_exact": CURIE_VIEW})
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "pull", "example_graph", str(work)).code == 0
    moved_source(work / "ontology", "2.0.0")
    before = tree_hash(root)
    refused = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert refused.error["code"] == "admin.views_broken", refused.envelope
    assert tree_hash(root) == before
    (work / "views" / "supplier_exact.yaml").write_text(
        CURIE_VIEW.replace("inv:Supplier", "sup:Supplier")
    )
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work), "--reason", "move")
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["views"]["published"] is True
    assert pushed.data["ontology"]["republished"].keys() == {"functions"}
    assert pin(run_cli) == "2.0.0"
    assert admin(run_cli, "graph", "check", "example_graph").code == 0
    assert audit_errors(run_cli) == 0
    again = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert again.code == 0 and again.data["unchanged"] is True, again.envelope


def test_a_push_republishes_the_views_it_does_not_replace(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW})
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "pull", "example_graph", str(work)).code == 0
    moved_source(work / "ontology", "2.0.0")
    base_before = (work / ".graph_cli_source.json").read_text()
    dry = admin(run_cli, "graph", "push", "example_graph", str(work), "--dry-run")
    assert dry.code == 0, dry.envelope
    assert dry.data["ontology"]["republished"]["views"]["names"] == ["supplier_catalog"]
    assert (work / ".graph_cli_source.json").read_text() == base_before
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work), "--reason", "move")
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["ontology"]["republished"]["views"]["count"] == 1
    assert pushed.data["views"]["published"] is False
    assert views_slot(run_cli)["published_against"]["version"] == "2.0.0"
    # the workspace still describes the graph: pushing again changes nothing
    again = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert again.code == 0 and again.data["unchanged"] is True, again.envelope
    assert audit_errors(run_cli) == 0


def test_a_push_checks_the_views_against_the_functions_it_brings(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """A release with renames and a pushed function: the published view is compiled again
    against the pushed function, not the published one."""
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW})
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "pull", "example_graph", str(work)).code == 0
    moved_source(work / "ontology", "2.0.0")
    functions = work / "functions"
    # the function the view runs is rewritten (same name, same column): the view still holds
    (functions / "suppliers_by_name.yaml").write_text(
        BARE_FUNCTION.replace("MATCH (s:Supplier)", "MATCH (s:Supplier)\n  WITH s")
    )
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["functions"]["published"] is True
    assert pushed.data["ontology"]["republished"].keys() == {"views"}
    assert audit_errors(run_cli) == 0
    # the function the view runs is removed in the same push: the view is refused
    (functions / "suppliers_by_name.yaml").unlink()
    refused = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert refused.error["code"] == "admin.views_broken", refused.envelope
    assert {p["code"] for p in refused.error["details"]["problems"]} == {"view.unknown_function"}


def test_functions_with_old_curies_are_refused_before_the_views(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW})
    publish(
        run_cli,
        "functions",
        write_folder(
            tmp_path,
            "functions2",
            {"suppliers_by_name": BARE_FUNCTION, "suppliers_exact": CURIE_FUNCTION},
        ),
    )
    refused = release(run_cli, moved_source(tmp_path))
    assert refused.error["code"] == "admin.functions_broken", refused.envelope


def test_a_release_without_renames_still_blocks_a_broken_view(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """A removed term is not a rename: no recompile, the release is refused as before. The
    functions are refused first; a view of its own that names the class is what is left."""
    scene(run_cli, tmp_path, {"supplier_catalog": BARE_VIEW})
    removed = tmp_path / "removed"
    removed.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    start, end = text.index("  Supplier:\n"), text.index("  Category:\n")
    text = (text[:start] + text[end:]).replace("range: Supplier", "range: Location", 1)
    (removed / "inventory.yaml").write_text(text)
    refused = admin(run_cli, "ontology", "release", "example_graph", str(removed))
    assert refused.error["code"] == "admin.functions_broken", refused.envelope
    # unpublish the view and the function, publish a view without the function
    lone = BARE_VIEW.split("  - kind: rows")[0]
    publish(run_cli, "views", write_folder(tmp_path, "lone", {"supplier_catalog": lone}))
    publish(run_cli, "views", write_folder(tmp_path, "no_views", {}))
    publish(run_cli, "functions", write_folder(tmp_path, "no_functions", {}))
    publish(run_cli, "views", write_folder(tmp_path, "again", {"supplier_catalog": lone}))
    again = admin(run_cli, "ontology", "release", "example_graph", str(removed))
    assert again.error["code"] == "admin.views_broken", again.envelope
    assert again.error["details"]["views"] == ["supplier_catalog"]
