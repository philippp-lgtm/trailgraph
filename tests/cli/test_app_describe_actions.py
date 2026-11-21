"""S: graph describe actions|action and the action surfaces of the other describe commands and of
``lookup --for`` (actions plan A6, A10): listing, errors, text forms, visibility by the write
rights an action needs, and graphs without a catalog."""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.actions import VALID
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import EXAMPLES, example_model
from support.policies import write_policy
from support.services import OpsFactory

from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog

ONTOLOGY = EXAMPLES / "company" / "ontology" / "1.1.0"
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
FACTORY = OpsFactory(example_model("company", "1.1.0"))
NAMES = ["assign_position", "create_task", "hand_over_position", "offboard_employee"]
NO_TASKS = """\
schema: graph_cli.policy/1
id: graph
types:
  "ops:Task": read
"""
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
NO_EXPLORE_WRITES = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(ONTOLOGY), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(VALID))
    assert published.code == 0, published.envelope
    return root


def restrict(run_cli: CliRunner, tmp_path: Path, policy: str, *options: str) -> None:
    path = write_policy(tmp_path / "work", "policy.yaml", policy)
    applied = admin(run_cli, "policy", "apply", str(path), *options, "--reason", "test policy")
    assert applied.code == 0, applied.envelope


def create(run_cli: CliRunner, cls: str, local: str) -> tuple[str, str]:
    """An entity of the class: its id and the value of its label attribute."""
    ops = FACTORY.create(cls, local)
    applied = app(run_cli, "apply", "-", env=ENV, input=json.dumps({"ops": ops}))
    assert applied.code == 0, applied.envelope
    entities = applied.data["results"][0]["result"]["entities"]
    attrs = next(op["attrs"] for op in ops if op.get("local") == local)
    return str(next(e["id"] for e in entities if e["local"] == local)), str(
        attrs.get("name") or attrs["title"]
    )


def test_listing_and_module_filter(run_cli: CliRunner, root: Path) -> None:
    listed = app(run_cli, "describe", "actions", env=ENV)
    assert listed.code == 0, listed.envelope
    assert [a["name"] for a in listed.data["actions"]] == NAMES
    assert listed.data["schema"] == "graph_cli.model/1" and "module" not in listed.data
    assert listed.data["actions"][0]["signature"] == (
        "person: ref ppl:Person, position: ref org:Position, since: date"
    )
    one = app(run_cli, "describe", "actions", "--module", "org", "--include-deprecated", env=ENV)
    assert one.data["module"] == "organization"
    assert [a["name"] for a in one.data["actions"]] == ["assign_position", "hand_over_position"]
    unknown = app(run_cli, "describe", "actions", "--module", "no_such", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")


def test_describe_action_and_not_found(run_cli: CliRunner, root: Path) -> None:
    found = app(run_cli, "describe", "action", "offboard_employee", env=ENV)
    assert found.code == 0, found.envelope
    a = found.data["action"]
    assert a["reason"] == "required" and a["hash"].startswith("sha256:")
    assert [n["action"] for n in a["needs"]] == ["write.transition", "write.unlink"]
    optional = app(run_cli, "describe", "action", "create_task", env=ENV).data["action"]
    assert [p["name"] for p in optional["params"]] == ["title", "due", "assignee"]
    missing = app(run_cli, "describe", "action", "assign_positions", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "action.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "assign_position"
    assert missing.error["hint"] == "graph describe search <words> --kind action"


def test_text_forms(run_cli: CliRunner, root: Path) -> None:
    listed = run_cli("graph", "describe", "actions", "--module", "org", env=ENV)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.splitlines()[0] == (
        "assign_position(person: ref ppl:Person, position: ref org:Position, since: date)"
        "  [organization]  Record that a person takes over a position nobody holds, from a date."
    )
    action = run_cli("graph", "describe", "action", "offboard_employee", env=ENV).stdout
    lines = action.splitlines()
    assert lines[0] == "offboard_employee(employee: ref ppl:Employee)  [people]"
    assert "  Requires --reason" in lines and "Parameters:" in lines
    assert "  employee: ref ppl:Employee  The employee who leaves." in lines
    assert "  not_left_yet on employee: The employee has not left already." in lines
    assert "  write.transition on ppl:Employee" in lines
    assert '  {"id": "$employee", "op": "transition", "to": "left"}' in lines
    assert lines[-1].startswith("Hash: sha256:") and "Examples:" in lines
    task = run_cli("graph", "describe", "action", "create_task", env=ENV).stdout
    assert "  due: date  Date the task is due" in task and "Preconditions:\n  -\n" in task
    module = run_cli("graph", "describe", "module", "org", env=ENV).stdout
    assert "\nActions:\n  assign_position(person: ref ppl:Person" in module
    modules = run_cli("graph", "describe", "modules", env=ENV).stdout
    (head,) = [line for line in modules.splitlines() if line.startswith("organization [org]")]
    assert head.endswith(", 2 actions")
    found = run_cli("graph", "describe", "search", "checklist", "--kind", "action", env=ENV)
    assert found.stdout.startswith("action  create_task  [operations]")
    assert found.stdout.rstrip().endswith("<use_when: phrase>")


def test_modules_and_search_json(run_cli: CliRunner, root: Path) -> None:
    modules = app(run_cli, "describe", "modules", env=ENV).data["modules"]
    assert {m["id"]: m["actions"] for m in modules}["organization"] == 2
    assert "functions" not in modules[0]  # the graph has no functions
    section = app(run_cli, "describe", "module", "org", env=ENV).data["module"]["actions"]
    assert [a["name"] for a in section] == ["assign_position", "hand_over_position"]
    found = app(run_cli, "describe", "search", "checklist", env=ENV)
    assert [(i["kind"], i["curie"], i["match"]["field"]) for i in found.data["items"]] == [
        ("action", "create_task", "use_when")
    ]
    only = app(run_cli, "describe", "search", "position", "--kind", "action", env=ENV)
    assert {i["kind"] for i in only.data["items"]} == {"action"}
    assert {i["curie"] for i in only.data["items"]} >= {"assign_position", "hand_over_position"}


def test_a_denied_write_right_marks_the_action_not_allowed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """The caller sees all the action names but may not write tasks: lists and counts say what
    it may use plus how many it may not; search and ``describe action`` show it marked."""
    restrict(run_cli, tmp_path, NO_TASKS, "--graph", "example_graph")
    log = FileAuditLog(root)
    before = list(log.read_lines(INSTALLATION_STREAM))
    listed = app(run_cli, "describe", "actions", env=ENV)
    assert [a["name"] for a in listed.data["actions"]] == [n for n in NAMES if n != "create_task"]
    assert listed.data["not_allowed"] == 1
    assert "1 more not allowed for you; --include-not-allowed lists them" in (
        run_cli("graph", "describe", "actions", env=ENV).stdout
    )
    both = app(run_cli, "describe", "actions", "--include-not-allowed", env=ENV)
    assert [a["name"] for a in both.data["actions"]] == [
        *(n for n in NAMES if n != "create_task"),
        "create_task",
    ]
    marked = both.data["actions"][-1]
    assert marked["allowed"] is False
    assert marked["missing"] == [{"action": "write.create", "resource": "type:ops:Task"}]
    assert all("allowed" not in a for a in both.data["actions"][:-1])  # allowed items unchanged
    shown = app(run_cli, "describe", "action", "create_task", env=ENV)
    assert shown.code == 0, shown.envelope
    assert shown.data["action"]["allowed"] is False
    assert shown.data["action"]["missing"] == marked["missing"]
    text = run_cli("graph", "describe", "action", "create_task", env=ENV).stdout
    assert "NOT ALLOWED for you; missing write.create on type:ops:Task" in text
    assert "Ask the maintainer for a role" in text
    assert "create_task" in [a["name"] for a in both.data["actions"]]
    hits = app(run_cli, "describe", "search", "checklist", env=ENV).data["items"]
    assert [(i["curie"], i["allowed"], i["missing"]) for i in hits] == [
        ("create_task", False, marked["missing"])
    ]
    line = run_cli("graph", "describe", "search", "checklist", env=ENV).stdout
    assert line.rstrip().endswith("[not allowed: missing write.create on type:ops:Task]")
    modules = app(run_cli, "describe", "modules", env=ENV).data["modules"]
    operations = {m["id"]: m for m in modules}["operations"]
    assert (operations["actions"], operations["not_allowed"]) == (0, {"actions": 1})
    assert "not_allowed" not in {m["id"]: m for m in modules}["organization"]
    section = app(run_cli, "describe", "module", "ops", env=ENV).data["module"]
    assert section["actions"] == [] and section["not_allowed"] == {"actions": 1}
    flagged = app(run_cli, "describe", "module", "ops", "--include-not-allowed", env=ENV)
    (row,) = flagged.data["module"]["actions"]
    assert row["name"] == "create_task" and row["allowed"] is False
    assert list(log.read_lines(INSTALLATION_STREAM)) == before  # a listing records no denial


def test_a_call_of_a_not_allowed_action_is_a_recorded_denial(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    restrict(run_cli, tmp_path, NO_TASKS, "--graph", "example_graph")
    log = FileAuditLog(root)
    before = len(list(log.read_lines(INSTALLATION_STREAM)))
    denied = app(run_cli, "do", "create_task", "--param", "title=x", env=ENV)
    assert (denied.code, denied.error["code"]) == (3, "access.denied")
    assert denied.error["details"]["action"] == "write.create"
    assert denied.error["details"]["resource"].endswith("/type:ops:Task")
    assert len(list(log.read_lines(INSTALLATION_STREAM))) == before + 1
    unknown = app(run_cli, "do", "create_tasks", "--param", "title=x", env=ENV)
    assert unknown.error["code"] == "action.not_found"  # a name that is no action
    assert len(list(log.read_lines(INSTALLATION_STREAM))) == before + 1


def test_a_caller_who_may_run_no_action_sees_them_all_not_allowed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    restrict(run_cli, tmp_path, NO_EXPLORE_WRITES)
    listed = app(run_cli, "describe", "actions", env=EXPLORE)
    assert listed.data["actions"] == [] and listed.data["not_allowed"] == len(NAMES)
    shown = app(run_cli, "describe", "action", "assign_position", env=EXPLORE)
    assert shown.data["action"]["allowed"] is False
    assert {m["action"] for m in shown.data["action"]["missing"]} == {"write.link"}
    missing = app(run_cli, "describe", "action", "assign_positions", env=EXPLORE)
    assert missing.error["code"] == "action.not_found"
    assert missing.error["details"]["did_you_mean"][0] == "assign_position"
    hits = app(run_cli, "describe", "search", "checklist", env=EXPLORE).data["items"]
    assert [(i["curie"], i["allowed"]) for i in hits] == [("create_task", False)]
    modules = app(run_cli, "describe", "modules", env=EXPLORE).data["modules"]
    assert all(m["actions"] == 0 for m in modules if "actions" in m)
    assert sum(m.get("not_allowed", {}).get("actions", 0) for m in modules) == len(NAMES)
    function = app(run_cli, "lookup", "x", "--for", "assign_position.person", env=EXPLORE)
    assert function.error["code"] == "function.not_found"  # a parameter fit is for what may run
    assert function.error["message"] == "no function 'assign_position'"


def test_graphs_without_a_catalog_are_unchanged(run_cli: CliRunner, root: Path) -> None:
    create_graph(run_cli, "plain_graph", "inventory")
    env = {"GRAPH_CLI_GRAPH": "plain_graph"}
    assert app(run_cli, "describe", "actions", env=env).data["actions"] == []
    missing = app(run_cli, "describe", "action", "anything", env=env)
    assert missing.error["code"] == "action.not_found"
    assert missing.error["details"]["did_you_mean"] == []
    modules = app(run_cli, "describe", "modules", env=env)
    assert all("actions" not in m for m in modules.data["modules"])
    assert "action" not in run_cli("graph", "describe", "modules", env=env).stdout
    assert "Actions:" not in run_cli("graph", "describe", "module", "inv", env=env).stdout
    unknown = app(run_cli, "lookup", "x", "--for", "no_such.param", env=env)
    assert unknown.error["message"] == "no function 'no_such'"


def test_lookup_for_an_action_parameter(run_cli: CliRunner, root: Path) -> None:
    employee, name = create(run_cli, "ppl:Employee", "e")
    position, title = create(run_cli, "org:Position", "pos")
    both = app(run_cli, "lookup", name, "--for", "assign_position.person", env=ENV)
    assert both.code == 0, both.envelope
    assert [i["id"] for i in both.data["items"]] == [employee]  # a subtype of the class fits
    fit = both.data["fit"]
    assert fit["for"] == "assign_position.person" and "ppl:Employee" in fit["types"]
    assert "org:Position" not in fit["types"]
    narrowed = app(run_cli, "lookup", name, "--for", "assign_position.position", env=ENV)
    assert narrowed.data["items"] == [] and narrowed.data["texts"][0]["excluded"] == 1
    assert narrowed.data["fit"]["types"] == ["org:Position"]
    assert [
        i["id"]
        for i in app(run_cli, "lookup", title, "--for", "assign_position.position", env=ENV).data[
            "items"
        ]
    ] == [position]


def test_lookup_for_errors(run_cli: CliRunner, root: Path) -> None:
    unknown = app(run_cli, "lookup", "x", "--for", "assign_position.persn", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (2, "action.parameter_unknown")
    details = unknown.error["details"]
    assert details["action"] == "assign_position" and "function" not in details
    assert details["expected"] == ["person", "position", "since"]
    assert details["did_you_mean"] == ["person"]
    plain = app(run_cli, "lookup", "x", "--for", "assign_position.since", env=ENV)
    assert (plain.code, plain.error["code"]) == (2, "usage.invalid_argument")
    assert plain.error["details"] == {
        "argument": "--for",
        "action": "assign_position",
        "parameter": "since",
    }
    nothing = app(run_cli, "lookup", "x", "--for", "assign_postion.person", env=ENV)
    assert (nothing.code, nothing.error["code"]) == (1, "function.not_found")
    assert nothing.error["message"] == "no function or action 'assign_postion'"
    assert nothing.error["details"]["did_you_mean"][0] == "assign_position"
