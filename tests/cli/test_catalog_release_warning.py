"""C: what an ontology release does to the entries a catalog grant names (catalog callers plan,
step 5, Q8): a release that renames a term compiles the published catalogs again from their
sources, so a granted function or view gets another entry hash and the callers of the grant run
the new definition. The release warns ``catalog.granted_entry_changed``, the code and shape of a
publish, also on a dry run; an entry no grant names is republished without a warning.

The scene is the inventory series with ``inv:Supplier`` moved to its own module
(``support.moved_supplier``): the function ``suppliers_by_name`` and the views ``supplier_catalog``
(which runs the function) follow the move. The role ``catalog_agent`` is granted the function and
the role ``viewer`` the view only.
"""

from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.moved_supplier import BARE_FUNCTION, BARE_VIEW, INVENTORY, moved_source, write_folder
from support.policies import write_policy

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  catalog_agent: {description: "Runs one function of example_graph"}
  viewer: {description: "Shows one view of example_graph"}
grants:
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [suppliers_by_name]}
    reads: [inv:Item]
  - entrypoint: server
    principal: {role: viewer, project: example_project}
    graphs: [example_graph]
    level: catalog
    catalog: {views: [supplier_catalog]}
    reads: [inv:Item]
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
CHANGED = "catalog.granted_entry_changed"


@pytest.fixture
def scene(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """The graph at 1.1.0 with the function and the view published, the grants installed; the
    path of the 2.0.0 source."""
    init(run_cli, isolated_env)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")
    )
    assert created.code == 0, created.envelope
    folders = {
        "functions": write_folder(tmp_path, "functions", {"suppliers_by_name": BARE_FUNCTION}),
        "views": write_folder(tmp_path, "views", {"supplier_catalog": BARE_VIEW}),
    }
    for kind, folder in folders.items():
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "catalog caller")
    assert applied.code == 0, applied.envelope
    return moved_source(tmp_path)


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


def hashes(run_cli: CliRunner, kind: str) -> dict[str, str]:
    shown = admin(run_cli, "graph", kind, "show", "example_graph")
    assert shown.code == 0, shown.envelope
    return {r["name"]: r["hash"] for r in shown.data[kind]}


def catalog_warnings(outcome: Outcome) -> list[dict[str, Any]]:
    return [w for w in outcome.warnings if w["code"].startswith("catalog.")]


def test_a_dry_run_warns_for_the_granted_entries_a_release_compiles_again(
    run_cli: CliRunner, scene: Path
) -> None:
    before = (hashes(run_cli, "functions"), hashes(run_cli, "views"))
    dry = release(run_cli, scene, "--dry-run")
    assert dry.code == 0, dry.envelope
    assert set(dry.data["republished"]) == {"functions", "views"}
    found = catalog_warnings(dry)
    assert [(w["code"], w["details"]["kind"], w["details"]["name"]) for w in found] == [
        (CHANGED, "function", "suppliers_by_name"),
        (CHANGED, "view", "supplier_catalog"),
    ]
    function, view = found
    assert function["details"]["grants"] == [
        {"grant": "grant.1", "principal": {"role": ["catalog_agent"]}}
    ]
    assert view["details"]["grants"] == [
        {"grant": "grant.2", "principal": {"project": ["example_project"], "role": ["viewer"]}}
    ]
    assert "grant.1 (role catalog_agent)" in function["message"]
    assert function["details"]["hash_before"] == before[0]["suppliers_by_name"]
    assert view["details"]["hash_before"] == before[1]["supplier_catalog"]
    assert function["details"]["hash_after"] != function["details"]["hash_before"]
    # a dry run publishes nothing
    assert (hashes(run_cli, "functions"), hashes(run_cli, "views")) == before


def test_the_release_warns_with_the_hash_it_publishes(run_cli: CliRunner, scene: Path) -> None:
    released = release(run_cli, scene, "--reason", "move the suppliers")
    assert released.code == 0, released.envelope
    found = {w["details"]["name"]: w for w in catalog_warnings(released)}
    assert sorted(found) == ["supplier_catalog", "suppliers_by_name"]
    assert (
        found["suppliers_by_name"]["details"]["hash_after"]
        == hashes(run_cli, "functions")["suppliers_by_name"]
    )
    assert (
        found["supplier_catalog"]["details"]["hash_after"]
        == hashes(run_cli, "views")["supplier_catalog"]
    )
    # the same release again has nothing left to compile and warns about nothing
    again = release(run_cli, scene, "--reason", "again")
    assert catalog_warnings(again) == []


def test_an_entry_no_grant_names_is_compiled_again_without_a_warning(
    run_cli: CliRunner, scene: Path, tmp_path: Path
) -> None:
    only_function = POLICY.replace(
        "catalog: {views: [supplier_catalog]}", "catalog: {views: [other_view]}"
    )
    path = write_policy(tmp_path / "policies", "installation.yaml", only_function)
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "narrow").code == 0
    dry = release(run_cli, scene, "--dry-run")
    assert set(dry.data["republished"]) == {"functions", "views"}
    assert [w["details"]["name"] for w in catalog_warnings(dry)] == ["suppliers_by_name"]


def test_without_a_catalog_grant_a_release_warns_about_no_entry(
    run_cli: CliRunner, scene: Path, tmp_path: Path
) -> None:
    path = write_policy(
        tmp_path / "policies",
        "installation.yaml",
        "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        '  - {entrypoint: app, graphs: "*", level: write}\n'
        'no_project: {"*": write}\n',
    )
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "no grants").code == 0
    dry = release(run_cli, scene, "--dry-run")
    assert dry.code == 0, dry.envelope
    assert set(dry.data["republished"]) == {"functions", "views"}
    assert catalog_warnings(dry) == []


def test_a_push_that_carries_the_release_warns_like_the_release(
    run_cli: CliRunner, scene: Path, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "pull", "example_graph", str(work)).code == 0
    moved_source(work / "ontology", "2.0.0")
    dry = admin(run_cli, "graph", "push", "example_graph", str(work), "--dry-run")
    assert dry.code == 0, dry.envelope
    assert [w["details"]["name"] for w in catalog_warnings(dry)] == [
        "suppliers_by_name",
        "supplier_catalog",
    ]
    pushed = admin(run_cli, "graph", "push", "example_graph", str(work), "--reason", "move")
    assert pushed.code == 0, pushed.envelope
    assert [w["code"] for w in catalog_warnings(pushed)] == [CHANGED, CHANGED]
