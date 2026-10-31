"""S: graph run over the company fixture (functions plan K2, K8, K10, K11): parameter types,
reference resolution and class checks, the deprecated warning, paging, denials, graphs
without a catalog, and that no parameter value reaches the access log, the activity signal
or the audit trail."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.activity import short_dir
from support.admin import admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES, ONTOLOGIES
from support.functions import VALID
from support.policies import write_policy

from graph_cli.catalog.activity import ActivityListener
from graph_cli.kernel.changeset import read_documents
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
MARKER = "run-marker-5c1e"
NO_EXPLORE_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
"""
WEIGHTS = """\
schema: graph_cli.function/1
name: items_over_weight
summary: Items that weigh at least a given weight and were bought after a date.
use_when: Heavy items, items above a weight.
params:
  min_weight: {type: decimal, summary: Lowest weight in kilograms.}
  since: {type: date, default: "2000-01-01", summary: Earliest acquisition date.}
  min_quantity: {type: integer, default: 0, summary: Lowest quantity.}
  conditions:
    type: enum
    enum: inv:Condition
    many: true
    default: [new, good, damaged, refurbished]
    summary: Accepted conditions.
query: |
  MATCH (i:Item)
  WHERE i.weight >= $min_weight AND i.acquired_on >= $since AND i.quantity >= $min_quantity
    AND i.condition IN $conditions
  RETURN i.label AS item, i.weight AS weight
  ORDER BY item
returns:
  - {column: item, summary: Name of the item.}
  - {column: weight, summary: Weight in kilograms.}
examples:
  - question: Which items weigh more than ten kilograms?
"""


def seeds() -> list[dict[str, Any]]:
    folder = FIXTURES / "seeds" / "company"
    text = b"".join(
        (folder / f).read_bytes() for f in ("changes.jsonl", "changes_operations.jsonl")
    )
    return read_documents(text)  # type: ignore[return-value]


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    created = admin(
        run_cli,
        "graph",
        "create",
        "example_graph",
        "--ontology",
        str(ONTOLOGIES / "company" / "1.1.0"),
        "--module",
        "company",
    )
    assert created.code == 0, created.envelope
    write_documents(root, "example_graph", seeds())
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(VALID))
    assert published.code == 0, published.envelope
    return root


def alias_of(run_cli: CliRunner, cls: str, name: str) -> dict[str, Any]:
    found = app(run_cli, "find", "--type", cls, "--limit", "50", env=ENV)
    (item,) = [i for i in found.data["items"] if i["attrs"].get("name") == name]
    entity: dict[str, Any] = item
    return entity


def run(run_cli: CliRunner, *args: str, env: dict[str, str] = ENV) -> Any:
    return app(run_cli, "run", *args, env=env)


def test_a_run_equals_the_query_of_its_normalized_text(run_cli: CliRunner, root: Path) -> None:
    unit = alias_of(run_cli, "org:Department", "Department A")
    described = app(run_cli, "describe", "function", "members_of_unit", env=ENV)
    ran = run(run_cli, "members_of_unit", "--param", f"unit={unit['id']}")
    assert ran.code == 0, ran.envelope
    asked = app(
        run_cli,
        "query",
        described.data["function"]["query"],
        "--param",
        f"unit={unit['id']}",
        env=ENV,
    )
    assert ran.data["function"] == {
        "name": "members_of_unit",
        "hash": described.data["function"]["hash"],
    }
    assert ran.data["columns"] == ["person", "position", "unit"] and ran.data["items"]
    assert {k: v for k, v in ran.data.items() if k != "function"} == asked.data
    assert ran.envelope["page"] == asked.envelope["page"]
    assert ran.envelope["graph"] == asked.envelope["graph"]


def test_references_take_id_alias_and_trailing_hex_but_no_name(
    run_cli: CliRunner, root: Path
) -> None:
    unit = alias_of(run_cli, "org:Department", "Department A")
    by_id = run(run_cli, "members_of_unit", "--param", f"unit={unit['id']}")
    for ref in (unit["alias"], unit["id"].replace("-", "")[-12:]):
        same = run(run_cli, "members_of_unit", "--param", f"unit={ref}")
        assert same.code == 0 and same.data == by_id.data, ref
    team = alias_of(run_cli, "org:Team", "Team A1")  # a subclass of the declared class
    assert run(run_cli, "members_of_unit", "--param", f"unit={team['alias']}").code == 0
    named = run(run_cli, "members_of_unit", "--param", "unit=Department A")
    assert (named.code, named.error["code"]) == (2, "function.parameter_invalid")
    assert "looked up first" in named.error["message"]
    assert named.error["hint"] == "graph lookup <text> --for members_of_unit.unit"
    gone = run(run_cli, "members_of_unit", "--param", "unit=0192f3c0")
    assert (gone.code, gone.error["code"]) == (1, "entity.not_found")


def test_a_reference_of_the_wrong_class_is_refused(run_cli: CliRunner, root: Path) -> None:
    cost_center = alias_of(run_cli, "org:CostCenter", "Operations costs")
    refused = run(run_cli, "members_of_unit", "--param", f"unit={cost_center['alias']}")
    assert (refused.code, refused.error["code"]) == (1, "function.parameter_type")
    assert refused.error["details"]["expected"] == "org:OrgUnit"
    assert refused.error["details"]["found"] == "org:CostCenter"
    assert refused.error["hint"] == "graph lookup <text> --for members_of_unit.unit"


def test_usage_errors_exit_2(run_cli: CliRunner, root: Path) -> None:
    missing = run(run_cli, "members_of_unit")
    assert (missing.code, missing.error["code"]) == (2, "function.parameter_missing")
    assert missing.error["details"]["parameter"] == "unit"
    unknown = run(run_cli, "members_of_unit", "--param", "unit=PER-0001", "--param", "unti=1")
    assert (unknown.code, unknown.error["code"]) == (2, "function.parameter_unknown")
    shape = run(run_cli, "members_of_unit", "--param", "unit")
    assert (shape.code, shape.error["code"]) == (2, "usage.invalid_argument")
    twice = run(run_cli, "members_of_unit", "--param", "unit=a1b2c3d4", "--param", "unit=PER-1")
    assert (twice.code, twice.error["code"]) == (2, "function.parameter_invalid")
    paging = run(run_cli, "members_of_unit", "--limit", "0")
    assert (paging.code, paging.error["code"]) == (2, "usage.invalid_argument")


def test_types_lists_defaults_and_dates(run_cli: CliRunner, root: Path) -> None:
    titles = ["--param", "titles=Safety basics", "--param", "titles=Budget tools"]
    done = run(run_cli, "training_completions", *titles)
    assert done.code == 0, done.envelope
    assert done.data["items"] == [
        {"person": "Sam Sample", "training": "Safety basics", "completed_on": "2024-07-01"}
    ]
    late = run(run_cli, "training_completions", *titles, "--param", "since=2024-07-02")
    assert late.code == 0 and late.data["items"] == []
    early = run(run_cli, "training_completions", *titles, "--param", "since=2024-07-01")
    assert len(early.data["items"]) == 1  # the date is inclusive
    one = run(run_cli, "training_completions", "--param", "titles=Safety basics")
    assert len(one.data["items"]) == 1  # one value is a list of one
    bad = run(run_cli, "training_completions", *titles, "--param", "since=last week")
    assert (bad.code, bad.error["code"]) == (2, "function.parameter_invalid")
    assert bad.error["details"]["parameter"] == "since"
    none = run(run_cli, "training_completions")
    assert none.error["code"] == "function.parameter_missing"
    text = run(run_cli, "people_by_email_text", "--param", "text=example.test")
    assert [i["person"] for i in text.data["items"]] == [
        "Alex Example",
        "Kim Specimen",
        "Sam Sample",
    ]


def test_decimal_integer_enum_and_list_defaults(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "stock_graph", "inventory", version="2.1.0")
    env = {"GRAPH_CLI_GRAPH": "stock_graph"}
    category = app(run_cli, "create", "inv:Category", "--set", "name=Misc", env=env)
    assert category.code == 0, category.envelope
    for name, weight, bought, quantity, condition in (
        ("Anvil", "12.50", "2025-03-01", 1, "good"),
        ("Pebble", "0.05", "2025-03-01", 5, "good"),
        ("Crate", "12.5", "2020-01-01", 2, "refurbished"),
    ):
        place = app(
            run_cli,
            "create",
            "inv:Location",
            "--set",
            f"name=Shelf {name}",
            "--set",
            f"code={name}",
            env=env,
        )
        assert place.code == 0, place.envelope
        item = app(
            run_cli, "create", "inv:Item",
            "--set", f"label={name}", "--set", f"weight={weight}", "--set", f"acquired_on={bought}",
            "--set", f"quantity={quantity}", "--set", f"condition={condition}",
            "--edge", f"inv:located_in={place.data['entities'][0]['id']}",
            "--edge", f"inv:in_category={category.data['entities'][0]['id']}",
            env=env,
        )  # fmt: skip
        assert item.code == 0, item.envelope
    folder = tmp_path / "stock_functions"
    folder.mkdir()
    (folder / "items_over_weight.yaml").write_text(WEIGHTS)
    published = admin(run_cli, "graph", "functions", "publish", "stock_graph", str(folder))
    assert published.code == 0, published.envelope

    def names(*args: str) -> list[str]:
        ran = run(run_cli, "items_over_weight", *args, env=env)
        assert ran.code == 0, ran.envelope
        return [i["item"] for i in ran.data["items"]]

    assert names("--param", "min_weight=10") == ["Anvil", "Crate"]
    assert names("--param", "min_weight=12.50") == ["Anvil", "Crate"]  # decimals compare by value
    assert names("--param", "min_weight=10", "--param", "since=2024-01-01") == ["Anvil"]
    assert names("--param", "min_weight=0", "--param", "min_quantity=3") == ["Pebble"]
    assert names("--param", "min_weight=0", "--param", "conditions=refurbished") == ["Crate"]
    both = ("--param", "conditions=refurbished", "--param", "conditions=good")
    assert names("--param", "min_weight=10", *both) == ["Anvil", "Crate"]
    for bad in ("min_weight=heavy", "min_quantity=1.5", "conditions=mint", "since=2024-13-01"):
        refused = run(
            run_cli, "items_over_weight", "--param", "min_weight=1", "--param", bad, env=env
        )
        assert (refused.code, refused.error["code"]) == (2, "function.parameter_invalid"), bad
    assert run(run_cli, "items_over_weight", env=env).error["code"] == "function.parameter_missing"


def test_a_deprecated_function_runs_with_a_warning(run_cli: CliRunner, root: Path) -> None:
    unit = alias_of(run_cli, "org:Department", "Department A")
    ran = run(run_cli, "unit_members", "--param", f"unit={unit['alias']}")
    assert ran.code == 0, ran.envelope
    (warning,) = [w for w in ran.envelope["warnings"] if w["code"] == "function.deprecated"]
    assert warning["details"] == {"name": "unit_members", "replaced_by": "members_of_unit"}
    assert "members_of_unit" in warning["message"]
    assert "function.deprecated" not in str(
        run(run_cli, "members_of_unit", "--param", f"unit={unit['alias']}").envelope["warnings"]
    )


def test_unknown_function_and_graph_without_catalog(run_cli: CliRunner, root: Path) -> None:
    missing = run(run_cli, "member_of_unit")
    assert (missing.code, missing.error["code"]) == (1, "function.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "members_of_unit"
    assert missing.error["hint"] == "graph describe search <words> --kind function"
    create_graph(run_cli, "plain_graph", "inventory")
    plain = run(run_cli, "members_of_unit", env={"GRAPH_CLI_GRAPH": "plain_graph"})
    assert (plain.code, plain.error["code"]) == (1, "function.not_found")
    assert plain.error["details"]["did_you_mean"] == []


def test_paging_and_the_text_form(run_cli: CliRunner, root: Path) -> None:
    unit = alias_of(run_cli, "org:Department", "Department A")
    first = run(run_cli, "members_of_unit", "--param", f"unit={unit['alias']}", "--limit", "1")
    assert first.code == 0 and len(first.data["items"]) == 1
    page = first.envelope["page"]
    assert page["truncated"] and page["next_offset"] == 1 and page["total"] > 1
    (hint,) = first.envelope["hints"]
    assert hint["command"] == (
        f"graph run members_of_unit --graph example_graph --param unit={unit['alias']}"
        " --limit 1 --offset 1"
    )
    rest = run(run_cli, "members_of_unit", "--param", f"unit={unit['alias']}", "--offset", "1")
    everything = run(run_cli, "members_of_unit", "--param", f"unit={unit['alias']}")
    assert first.data["items"] + rest.data["items"] == everything.data["items"]
    text = run_cli("graph", "run", "members_of_unit", "--param", f"unit={unit['alias']}", env=ENV)
    assert text.returncode == 0, text.stderr
    lines = text.stdout.splitlines()
    assert lines[0].split() == ["person", "position", "unit"] and "Alex Example" in text.stdout
    assert len(lines) == 1 + len(everything.data["items"])


def enable_logs(env: dict[str, str]) -> Path:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    return Path(env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"


def test_the_access_log_names_the_function_and_no_value(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    log = enable_logs(isolated_env)
    described = app(run_cli, "describe", "function", "people_by_email_text", env=ENV)
    hash_ = described.data["function"]["hash"]
    ok = run(run_cli, "people_by_email_text", "--param", f"text={MARKER}")
    assert ok.code == 0 and ok.data["items"] == []
    bad = run(
        run_cli, "people_by_email_text", "--param", f"text={MARKER}", "--param", f"{MARKER}=1"
    )
    assert bad.error["code"] == "function.parameter_unknown"
    unknown = run(run_cli, f"{MARKER}_function")
    assert unknown.error["code"] == "function.not_found"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    ran, refused, absent = lines[-3:]
    assert ran["command"] == "run" and ran["result"] == {"status": "ok"}
    assert ran["function"] == {"name": "people_by_email_text", "hash": hash_}
    assert refused["function"] == {"name": "people_by_email_text", "hash": None}
    assert refused["result"]["code"] == "function.parameter_unknown"
    assert "function" not in absent and "function" not in lines[0]  # describe, and not found
    assert MARKER not in log.read_text()
    trail = FileAuditLog(root)
    streams = [INSTALLATION_STREAM]
    assert MARKER not in json.dumps([list(trail.read_lines(s)) for s in streams], default=str)


def test_the_activity_signal_names_the_function_and_no_value(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[activity]\nsignal_reads = true\n")
    unit = alias_of(run_cli, "org:Department", "Department A")
    with short_dir() as state:
        env = {**ENV, "XDG_STATE_HOME": str(state)}
        listener = ActivityListener.open(state / "graph_cli" / "activity")
        try:
            ran = run(run_cli, "members_of_unit", "--param", f"unit={unit['alias']}", env=env)
            assert ran.code == 0, ran.envelope
            events = listener.receive(2.0)
        finally:
            listener.close()
    assert events and {(e.command, e.function) for e in events} == {("run", "members_of_unit")}
    assert unit["id"] in {i for e in events for i in e.ids}  # what the query read, as for query
    assert unit["alias"] not in json.dumps([asdict(e) for e in events])


def test_a_denial_is_audited_without_the_values(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE_FUNCTIONS)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    unit = alias_of(run_cli, "org:Department", "Department A")
    assert run(run_cli, "members_of_unit", "--param", f"unit={unit['alias']}").code == 0
    log = FileAuditLog(root)
    before = list(log.read_lines(INSTALLATION_STREAM))
    denied = run(run_cli, "members_of_unit", "--param", f"unit={MARKER}", env=EXPLORE)
    assert (denied.code, denied.error["code"]) == (3, "access.denied")
    assert denied.error["details"]["action"] == "read.function"
    after = list(log.read_lines(INSTALLATION_STREAM))
    assert len(after) == len(before) + 1 and MARKER not in str(after[-1])
    adhoc = app(run_cli, "query", "MATCH (n:Department) RETURN count(n) AS c", env=EXPLORE)
    assert adhoc.code == 0  # ad-hoc queries stay readable
