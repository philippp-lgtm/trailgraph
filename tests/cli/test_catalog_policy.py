"""``policy check|apply`` warnings of catalog grants: ``policy.catalog_widened`` (a grant of the
whole graph also covers the callers of a catalog grant) and ``policy.entry_unknown`` (a granted
name the graph has not published). Toy graph ``example_graph`` (inventory) only.
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES
from support.policies import GRAPH, write_policy

HEAD = """\
schema: graph_cli.policy/1
id: installation
roles:
  support: {description: "Support agents"}
  reader: {description: "Readers"}
grants:
"""
CATALOG = """\
  - entrypoint: server
    principal: {role: support}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [items_by_condition], actions: [move_item], views: [item_catalog]}
    reads: ["inv:Item"]
"""
READER_OF_SUPPORT = (
    "  - {entrypoint: server, principal: {role: support}, graphs: [example_graph], level: read}\n"
)
READER = (
    "  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}\n"
)
TAIL = 'no_project: {"*": write}\n'


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    return root


def check(run_cli: CliRunner, tmp_path: Path, *grants: str, command: str = "check") -> Outcome:
    path = write_policy(tmp_path / "work", "installation.yaml", HEAD + "".join(grants) + TAIL)
    if command == "apply":
        return admin(run_cli, "policy", "apply", str(path), "--reason", "test policy")
    return admin(run_cli, "policy", "check", str(path))


@pytest.mark.parametrize("command", ["check", "apply"])
def test_a_grant_of_the_graph_for_the_same_role_widens_the_catalog_grant(
    run_cli: CliRunner, root: Path, tmp_path: Path, command: str
) -> None:
    outcome = check(run_cli, tmp_path, CATALOG, READER_OF_SUPPORT, command=command)
    assert outcome.code == 0, outcome.envelope
    warnings = [w for w in outcome.warnings if w["code"] == "policy.catalog_widened"]
    assert [w["details"] for w in warnings] == [
        {"graph": "example_graph", "catalog_grant": "grant.1", "covered_by": "grant.2"}
    ]
    assert "reads and markings of the catalog grant count for every read" in warnings[0]["message"]


def test_a_principal_less_read_grant_widens_every_catalog_grant(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    open_read = "  - {entrypoint: server, graphs: [example_graph], level: read}\n"
    outcome = check(run_cli, tmp_path, CATALOG, open_read)
    assert "policy.catalog_widened" in outcome.warning_codes


def test_grants_that_meet_only_in_a_token_do_not_warn(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """Another role (a token may hold both), another graph or another entrypoint: the policy
    alone cannot tell, so there is nothing certain to warn about."""
    outcome = check(run_cli, tmp_path, CATALOG, READER)
    assert outcome.code == 0, outcome.envelope
    assert "policy.catalog_widened" not in outcome.warning_codes
    app_read = "  - {entrypoint: app, graphs: [example_graph], level: read}\n"
    again = check(run_cli, tmp_path, CATALOG, app_read)
    assert "policy.catalog_widened" not in again.warning_codes


# ---- policy.entry_unknown -----------------------------------------------------------------


def published(run_cli: CliRunner, *kinds: str) -> None:
    for kind in kinds:
        folder = FIXTURES / kind / "inventory" / "valid"
        done = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert done.code == 0, done.envelope


@pytest.mark.parametrize("command", ["check", "apply"])
def test_a_granted_name_the_graph_has_not_published_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path, command: str
) -> None:
    published(run_cli, "functions", "actions")  # no views published
    outcome = check(run_cli, tmp_path, CATALOG, command=command)
    assert outcome.code == 0, outcome.envelope
    found = [w for w in outcome.warnings if w["code"] == "policy.entry_unknown"]
    assert [w["details"] for w in found] == [
        {"graph": "example_graph", "kind": "view", "name": "item_catalog"}
    ]
    assert "gives nothing until it does" in found[0]["message"]


def test_nothing_is_published_so_every_granted_name_warns_sorted(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    outcome = check(run_cli, tmp_path, CATALOG)
    assert [(w["details"]["kind"], w["details"]["name"]) for w in outcome.warnings] == [
        ("function", "items_by_condition"),
        ("action", "move_item"),
        ("view", "item_catalog"),
    ]


def test_published_names_do_not_warn_and_a_read_grant_with_actions_is_checked_too(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    published(run_cli, "functions", "actions", "views")
    assert "policy.entry_unknown" not in check(run_cli, tmp_path, CATALOG).warning_codes
    closer = (
        "  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph],"
        " level: read, catalog: {actions: [retire_itemm]}}\n"
    )
    outcome = check(run_cli, tmp_path, closer)
    assert [
        w["details"]["name"] for w in outcome.warnings if w["code"] == "policy.entry_unknown"
    ] == ["retire_itemm"]


def test_a_graph_policy_has_no_catalog_to_check(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    path = write_policy(tmp_path / "work", "g.yaml", GRAPH)
    outcome = admin(run_cli, "policy", "check", str(path), "--graph", "example_graph")
    assert outcome.code == 0
    assert "policy.entry_unknown" not in outcome.warning_codes
