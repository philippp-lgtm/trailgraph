"""S: graph describe functions|function and the function surfaces of the other describe
commands (plan K8-K10): listing, errors, text forms, visibility by ``read.function`` and graphs
without a catalog."""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import ONTOLOGIES
from support.functions import VALID
from support.policies import write_policy

from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
NO_EXPLORE_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
"""


def create_company(run_cli: CliRunner, name: str = "example_graph") -> None:
    outcome = admin(
        run_cli,
        "graph",
        "create",
        name,
        "--ontology",
        str(ONTOLOGIES / "company" / "1.1.0"),
        "--module",
        "company",
    )
    assert outcome.code == 0, outcome.envelope


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_company(run_cli)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(VALID))
    assert published.code == 0, published.envelope
    return root


def test_listing_module_filter_and_deprecated(run_cli: CliRunner, root: Path) -> None:
    listed = app(run_cli, "describe", "functions", env=ENV)
    assert listed.code == 0, listed.envelope
    names = [f["name"] for f in listed.data["functions"]]
    assert len(names) == 6 and "unit_members" not in names
    assert listed.data["schema"] == "graph_cli.model/1" and "module" not in listed.data
    one = app(run_cli, "describe", "functions", "--module", "ppl", "--include-deprecated", env=ENV)
    assert [f["name"] for f in one.data["functions"]] == ["people_by_email_text"]
    assert one.data["module"] == "people"
    every = app(
        run_cli, "describe", "functions", "--module", "org", "--include-deprecated", env=ENV
    )
    assert [(f["name"], f["status"]) for f in every.data["functions"]] == [
        ("members_of_unit", "active"),
        ("unit_members", "deprecated"),
        ("vacant_positions", "active"),
    ]
    unknown = app(run_cli, "describe", "functions", "--module", "orgg", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    assert unknown.error["details"]["did_you_mean"]


def test_describe_function_and_not_found(run_cli: CliRunner, root: Path) -> None:
    found = app(run_cli, "describe", "function", "members_of_unit", env=ENV)
    assert found.code == 0, found.envelope
    f = found.data["function"]
    assert f["signature"] == "unit: ref org:OrgUnit"
    assert f["query"].startswith("MATCH (p:`ppl:Person`)") and f["hash"].startswith("sha256:")
    missing = app(run_cli, "describe", "function", "member_of_unit", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "function.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "members_of_unit"
    assert missing.error["hint"] == "graph describe search <words> --kind function"


def test_text_forms(run_cli: CliRunner, root: Path) -> None:
    listed = run_cli("graph", "describe", "functions", "--module", "org", env=ENV)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.splitlines()[0] == (
        "members_of_unit(unit: ref org:OrgUnit)  [organization]  "
        "People holding a position in a unit or in any unit below it."
    )
    module = run_cli("graph", "describe", "module", "org", env=ENV).stdout
    assert "\nFunctions:\n  members_of_unit(unit: ref org:OrgUnit)  People holding" in module
    modules = run_cli("graph", "describe", "modules", env=ENV).stdout
    assert "organization [org] 1.0.0  5 types, 5 relations, 2 functions" in modules
    function = run_cli("graph", "describe", "function", "training_completions", env=ENV).stdout
    assert function.startswith("training_completions(titles: list of string, since?: date)")
    assert '  since: date (default "2000-01-01")  Only completions' in function
    assert "Relations touched: ops:completed\n" in function and "\nHash: sha256:" in function
    found = run_cli("graph", "describe", "search", "headcount", "--kind", "function", env=ENV)
    assert found.stdout.startswith("function  members_of_unit  [organization]")
    assert found.stdout.rstrip().endswith("<use_when: phrase>")


def test_search_finds_functions_in_the_json_answer(run_cli: CliRunner, root: Path) -> None:
    found = app(run_cli, "describe", "search", "headcount", env=ENV)
    assert [(i["kind"], i["curie"], i["match"]["field"]) for i in found.data["items"]] == [
        ("function", "members_of_unit", "use_when")
    ]
    kinds = app(run_cli, "describe", "search", "x", "--kind", "bogus", env=ENV)
    assert kinds.error["code"] == "usage.invalid_argument"


def test_a_deny_on_read_function_marks_every_function_not_allowed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE_FUNCTIONS)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    allowed = app(run_cli, "describe", "functions", env=ENV)
    assert len(allowed.data["functions"]) == 6 and "not_allowed" not in allowed.data
    who = app(run_cli, "whoami", env=EXPLORE).data["permissions"]["graphs"]
    assert [g["withheld"] for g in who] == [["read.function"]]

    log = FileAuditLog(root)
    before = list(log.read_lines(INSTALLATION_STREAM))
    listed = app(run_cli, "describe", "functions", env=EXPLORE)
    assert listed.data["functions"] == [] and listed.data["not_allowed"] == 6
    both = app(run_cli, "describe", "functions", "--include-not-allowed", env=EXPLORE)
    assert len(both.data["functions"]) == 6
    assert all(f["allowed"] is False for f in both.data["functions"])
    assert {m["resource"] for f in both.data["functions"] for m in f["missing"]} == {"graph"}
    shown = app(run_cli, "describe", "function", "members_of_unit", env=EXPLORE)
    assert shown.code == 0, shown.envelope
    assert shown.data["function"]["allowed"] is False
    assert shown.data["function"]["missing"] == [{"action": "read.function", "resource": "graph"}]
    unknown = app(run_cli, "describe", "function", "no_such_function", env=EXPLORE)
    assert (unknown.code, unknown.error["code"]) == (1, "function.not_found")
    assert "members_of_unit" not in unknown.error["details"]["did_you_mean"]  # not close
    hits = app(run_cli, "describe", "search", "headcount", env=EXPLORE).data["items"]
    assert hits and all(h["allowed"] is False for h in hits)
    modules = app(run_cli, "describe", "modules", env=EXPLORE).data["modules"]
    assert all(m["functions"] == 0 for m in modules if "functions" in m)
    assert sum(m.get("not_allowed", {}).get("functions", 0) for m in modules) == 6
    section = app(run_cli, "describe", "module", "org", env=EXPLORE).data["module"]
    assert section["functions"] == [] and section["not_allowed"]["functions"] >= 1
    assert list(log.read_lines(INSTALLATION_STREAM)) == before  # a listing records no denial
    ran = app(run_cli, "run", "members_of_unit", env=EXPLORE)
    assert (ran.code, ran.error["code"]) == (3, "access.denied")  # a call is a recorded access
    assert len(list(log.read_lines(INSTALLATION_STREAM))) == len(before) + 1


def test_graphs_without_a_catalog_are_unchanged(run_cli: CliRunner, root: Path) -> None:
    create_graph(run_cli, "plain_graph", "inventory")
    env = {"GRAPH_CLI_GRAPH": "plain_graph"}
    assert app(run_cli, "describe", "functions", env=env).data["functions"] == []
    missing = app(run_cli, "describe", "function", "anything", env=env)
    assert missing.error["code"] == "function.not_found"
    assert missing.error["details"]["did_you_mean"] == []
    modules = app(run_cli, "describe", "modules", env=env)
    assert all("functions" not in m for m in modules.data["modules"])
    text = run_cli("graph", "describe", "modules", env=env).stdout
    assert "function" not in text
    assert "Functions:" not in run_cli("graph", "describe", "module", "inv", env=env).stdout
