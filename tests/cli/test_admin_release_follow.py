"""S: policies and published catalogs across a release that renames terms (migration plan M8).

The inventory series with ``inv:Supplier`` moved to the module ``sup``: a deny rule that names
only the old type would fail open and refuses the release; functions and actions that name the
class with a bare name are compiled again and published with the release, those that name
the old CURIE block until a push brings them fixed.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.moved_supplier import (
    ACTION,
    BARE_FUNCTION,
    CURIE_FUNCTION,
    INVENTORY,
    moved_source,
    write_folder,
)
from support.policies import write_policy
from support.trees import tree_hash

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog

BOTH = "inv:Supplier"
HIDE_OLD = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Supplier": none
"""
HIDE_BOTH = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Supplier": none
  "sup:Supplier": none
"""
HIDE_GLOB = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:*": none
"""
DENY_TERM = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_supplier_names, type: ["inv:Supplier"], attribute: [name], action: ["write.update"]}
"""
DENY_TERM_BOTH = DENY_TERM.replace(
    'type: ["inv:Supplier"]', 'type: ["inv:Supplier", "sup:Supplier"]'
)
SUPPLIER = {"op": "create", "type": "inv:Supplier", "attrs": {"name": "Example supplier"}}


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


def apply_policy(run_cli: CliRunner, tmp_path: Path, text: str) -> None:
    path = write_policy(tmp_path / "policies", f"p{abs(hash(text))}.yaml", text)
    applied = admin(
        run_cli, "policy", "apply", str(path), "--graph", "example_graph", "--reason", "test"
    )
    assert applied.code == 0, applied.envelope


def publish(run_cli: CliRunner, kind: str, folder: Path) -> None:
    published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


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


# ---- deny rules ----------------------------------------------------------------------------


def test_a_hidden_type_is_not_released_under_its_new_name_unless_the_policy_names_both(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    apply_policy(run_cli, tmp_path, HIDE_OLD)
    source = moved_source(tmp_path)
    before = tree_hash(root)
    refused = release(run_cli, source)
    assert (refused.code, refused.error["code"]) == (1, "admin.policy_renamed_term")
    assert refused.error["category"] == "refused"
    details = refused.error["details"]
    assert (details["from"], details["to"], details["count"]) == ("1.1.0", "2.0.0", 1)
    rule = details["rules"][0]
    assert (rule["rule"], rule["kind"], rule["from"], rule["to"]) == (
        "type.inv:Supplier",
        "class",
        "inv:Supplier",
        "sup:Supplier",
    )
    assert rule["covered"] is False and rule["effect"] == "deny"
    assert "apply a policy that names both" in refused.error["message"]
    assert "policy apply" in refused.error["hint"]
    assert tree_hash(root) == before and pin(run_cli) == "1.1.0"
    # a dry run is refused the same way
    dry = release(run_cli, source, "--dry-run")
    assert dry.error["code"] == "admin.policy_renamed_term"
    # a policy naming both lets it through
    apply_policy(run_cli, tmp_path, HIDE_BOTH)
    released = release(run_cli, source)
    assert released.code == 0, released.envelope
    assert pin(run_cli) == "2.0.0"


@pytest.mark.parametrize("policy", [True, False])
def test_the_hidden_type_stays_hidden_after_the_rename(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path, policy: bool
) -> None:
    """The control (no policy) shows the entity under its new type, so the refusal below is the
    policy and not a missing type."""
    write_documents(root, "example_graph", [{"ops": [SUPPLIER]}])
    assert app(run_cli, "find", "--type", "inv:Supplier", env=ENV).code == 0
    if policy:
        apply_policy(run_cli, tmp_path, HIDE_BOTH)
    released = release(run_cli, moved_source(tmp_path, migration=True))
    assert released.code == 0, released.envelope
    assert released.data["migration"]["migrated"]["entities"] == 1
    seen = app(run_cli, "find", "--type", "sup:Supplier", env=ENV)
    if policy:
        assert seen.code != 0, seen.envelope
        nothing = app(run_cli, "find", "--type", "sup:Nothing", env=ENV)
        assert seen.error["code"] == nothing.error["code"]
    else:
        assert seen.code == 0, seen.envelope
        assert [i["alias"] for i in seen.data["items"]] == ["SUP-0001"]
    assert audit_errors(run_cli) == 0


def test_a_glob_that_no_longer_matches_the_new_name_is_refused(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """``inv:*`` hides every type of the module; the moved type leaves it."""
    apply_policy(run_cli, tmp_path, HIDE_GLOB)
    refused = release(run_cli, moved_source(tmp_path))
    assert refused.error["code"] == "admin.policy_renamed_term"
    assert [r["from"] for r in refused.error["details"]["rules"]] == ["inv:Supplier"]


def test_a_term_deny_rule_follows_the_class_it_names(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    apply_policy(run_cli, tmp_path, DENY_TERM)
    source = moved_source(tmp_path)
    refused = release(run_cli, source)
    assert refused.error["code"] == "admin.policy_renamed_term", refused.envelope
    rule = refused.error["details"]["rules"][0]
    assert rule["target_resource"].endswith("/type:sup:Supplier/attr:name")
    apply_policy(run_cli, tmp_path, DENY_TERM_BOTH)
    assert release(run_cli, source).code == 0


def test_a_policy_without_deny_rules_on_the_term_does_not_stop_a_release(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """Hiding another type of the module is not touched by moving this one."""
    apply_policy(run_cli, tmp_path, HIDE_OLD.replace("inv:Supplier", "inv:Location"))
    assert release(run_cli, moved_source(tmp_path)).code == 0


def test_diff_and_check_show_the_deny_rules_the_release_would_refuse(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    apply_policy(run_cli, tmp_path, HIDE_OLD)
    source = moved_source(tmp_path)
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
    assert shown.code == 0, shown.envelope
    assert "admin.policy_renamed_term_affected" in shown.warning_codes
    [reference] = shown.data["references"]
    assert (reference["kind"], reference["from"], reference["to"]) == (
        "class",
        "inv:Supplier",
        "sup:Supplier",
    )
    assert reference["policy"][0]["rule"] == "type.inv:Supplier"
    assert reference["policy"][0]["covered"] is False
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
    assert checked.error["code"] == "admin.policy_renamed_term"
    apply_policy(run_cli, tmp_path, HIDE_BOTH)
    ok = admin(
        run_cli,
        "ontology",
        "diff",
        str(source),
        "--module",
        "inventory",
        "--graph",
        "example_graph",
    )
    assert "admin.policy_renamed_term_affected" not in ok.warning_codes
    assert ok.data["references"][0]["policy"][0]["covered"] is True


# ---- catalogs ------------------------------------------------------------------------------


def published_scene(run_cli: CliRunner, tmp_path: Path, functions: dict[str, str]) -> None:
    publish(run_cli, "functions", write_folder(tmp_path, "functions", functions))


def test_functions_with_bare_names_are_published_again_with_the_release(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    published_scene(run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION})
    before = admin(run_cli, "graph", "functions", "show", "example_graph").data["slot"]
    released = release(run_cli, moved_source(tmp_path), "--reason", "move the suppliers")
    assert released.code == 0, released.envelope
    again = released.data["republished"]
    assert again["functions"]["names"] == ["suppliers_by_name"]
    assert "actions" not in again  # nothing published
    assert again["functions"]["compiled_hash"]["from"] == before["compiled_hash"]
    after = admin(run_cli, "graph", "functions", "show", "example_graph").data["slot"]
    assert after["hash"] == before["hash"]  # the same sources
    assert (
        after["compiled_hash"]
        == again["functions"]["compiled_hash"]["to"]
        != before["compiled_hash"]
    )
    assert after["published_against"]["version"] == "2.0.0"
    # nothing is left to republish: the graph checks clean and a second release is a no-op
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0, checked.envelope
    assert "republished" not in checked.data
    assert release(run_cli, moved_source(tmp_path, "again")).data["pinned"] is False
    # the audit trail shows two publishes of the same sources, with the reason
    log = events(root, graph_id)
    publishes = [
        e for e in log if "functions" in e.get("change", {}) and "from" in e["change"]["functions"]
    ]
    last = publishes[-1]["change"]["functions"]
    assert last["from"] == last["to"] == before["hash"]
    assert last["compiled_hash"] == after["compiled_hash"]
    assert publishes[-1]["reason"] == "move the suppliers"
    assert audit_errors(run_cli) == 0
    ran = app(run_cli, "run", "suppliers_by_name", env=ENV)
    assert ran.code == 0, ran.envelope


def test_a_republish_goes_with_a_migration_of_the_data(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    write_documents(root, "example_graph", [{"ops": [SUPPLIER]}])
    published_scene(run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION})
    released = release(run_cli, moved_source(tmp_path, migration=True))
    assert released.code == 0, released.envelope
    assert released.data["migration"]["migrated"]["entities"] == 1
    assert released.data["republished"]["functions"]["count"] == 1
    ran = app(run_cli, "run", "suppliers_by_name", env=ENV)
    assert ran.code == 0, ran.envelope
    assert json.dumps(ran.data).count("Example supplier") == 1
    assert audit_errors(run_cli) == 0


def test_a_dry_run_shows_the_republish_and_writes_nothing(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    published_scene(run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION})
    source = moved_source(tmp_path)
    before = tree_hash(root)
    dry = release(run_cli, source, "--dry-run")
    assert dry.code == 0, dry.envelope
    assert dry.data["republished"]["functions"]["names"] == ["suppliers_by_name"]
    assert dry.data["pinned"] is False
    assert tree_hash(root) == before


def test_functions_with_old_curies_block_the_release(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    published_scene(
        run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION, "suppliers_exact": CURIE_FUNCTION}
    )
    source = moved_source(tmp_path)
    before = tree_hash(root)
    refused = release(run_cli, source)
    assert (refused.code, refused.error["code"]) == (1, "admin.functions_broken")
    assert refused.error["details"]["functions"] == ["suppliers_exact"]
    assert tree_hash(root) == before and pin(run_cli) == "1.1.0"
    # the same in a push of the ontology alone, and in the check and diff previews
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
    assert checked.error["code"] == "admin.functions_broken"
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
    assert shown.warning_codes.count("admin.functions_affected") == 1
    [reference] = shown.data["references"]
    named = {f["name"]: f["explicit"] for f in reference["functions"]}
    assert named == {"suppliers_by_name": False, "suppliers_exact": True}
    assert "republished" not in shown.data


def test_a_push_brings_the_fixed_function_and_the_fixed_action(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    published_scene(
        run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION, "suppliers_exact": CURIE_FUNCTION}
    )
    publish(run_cli, "actions", write_folder(tmp_path, "actions", {"rename_supplier": ACTION}))
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "pull", "example_graph", str(work)).code == 0
    moved_source(work / "ontology", "2.0.0")
    refused = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert refused.error["code"] == "admin.functions_broken", refused.envelope
    (work / "functions" / "suppliers_exact.yaml").write_text(
        CURIE_FUNCTION.replace("inv:Supplier", "sup:Supplier")
    )
    refused = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert refused.error["code"] == "admin.actions_broken", refused.envelope
    assert refused.error["details"]["actions"] == ["rename_supplier"]
    (work / "actions" / "rename_supplier.yaml").write_text(
        ACTION.replace("inv:Supplier", "sup:Supplier")
    )
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work), "--reason", "move")
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["functions"]["published"] is True
    assert pushed.data["actions"]["published"] is True
    assert "republished" not in pushed.data["ontology"]
    assert audit_errors(run_cli) == 0


def test_a_push_republishes_the_catalog_it_does_not_replace(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    published_scene(run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION})
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "pull", "example_graph", str(work)).code == 0
    moved_source(work / "ontology", "2.0.0")
    base_before = (work / ".graph_cli_source.json").read_text()
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work), "--dry-run")
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["ontology"]["republished"]["functions"]["names"] == ["suppliers_by_name"]
    assert (work / ".graph_cli_source.json").read_text() == base_before
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work), "--reason", "move")
    assert pushed.code == 0, pushed.envelope
    assert pushed.data["ontology"]["republished"]["functions"]["count"] == 1
    assert pushed.data["functions"] is None or pushed.data["functions"]["published"] is False
    slot = admin(run_cli, "graph", "functions", "show", "example_graph").data["slot"]
    assert slot["published_against"]["version"] == "2.0.0"
    # the workspace still describes the graph: pushing again changes nothing
    again = admin(run_cli, "graph", "push", "example_graph", str(work))
    assert again.code == 0 and again.data["unchanged"] is True, again.envelope


def test_a_function_without_a_module_blocks_when_the_release_has_several(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """The sources of a single-module graph name no module; a release that splits the module
    makes the key mandatory, so those functions need a ``module:`` line pushed with it."""
    published_scene(
        run_cli,
        tmp_path,
        {"suppliers_by_name": BARE_FUNCTION.replace("module: inventory\n", "")},
    )
    refused = release(run_cli, moved_source(tmp_path))
    assert refused.error["code"] == "admin.functions_broken", refused.envelope
    [problem] = refused.error["details"]["problems"]
    assert problem["code"] == "function.module_required"


def test_actions_with_old_curies_block_the_release(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    publish(run_cli, "actions", write_folder(tmp_path, "actions", {"rename_supplier": ACTION}))
    refused = release(run_cli, moved_source(tmp_path))
    assert (refused.code, refused.error["code"]) == (1, "admin.actions_broken")
    assert refused.error["details"]["actions"] == ["rename_supplier"]
    shown = admin(
        run_cli,
        "ontology",
        "diff",
        str(moved_source(tmp_path, "m2")),
        "--module",
        "inventory",
        "--graph",
        "example_graph",
    )
    assert "admin.actions_affected" in shown.warning_codes
    [reference] = shown.data["references"]
    assert reference["actions"] == [{"name": "rename_supplier", "explicit": True}]


def test_a_release_without_renames_still_blocks_a_broken_catalog(
    run_cli: CliRunner, root: Path, graph_id: str, tmp_path: Path
) -> None:
    """A removed term is not a rename: no recompile, the release is refused as before."""
    published_scene(run_cli, tmp_path, {"suppliers_by_name": BARE_FUNCTION})
    removed = tmp_path / "removed"
    shutil.copytree(INVENTORY / "1.1.0", removed)
    path = removed / "inventory.yaml"
    text = path.read_text().replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    start, end = text.index("  Supplier:\n"), text.index("  Category:\n")
    path.write_text(text[:start] + text[end:])
    text = path.read_text().replace("range: Supplier", "range: Location", 1)
    path.write_text(text)
    refused = admin(run_cli, "ontology", "release", "example_graph", str(removed))
    assert refused.error["code"] == "admin.functions_broken", refused.envelope
