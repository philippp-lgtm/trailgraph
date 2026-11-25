"""S: ``graph call`` over the inventory graph with functions, actions and views (tool manifest plan
TM7): a call answers what ``run``, ``do`` and ``view`` answer for the same input, the typed input
rules, the controls, refusals, idempotency, the audit and the access log."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from jsonschema import Draft202012Validator
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import EXAMPLES, FIXTURES
from support.read_limits import install
from support.tools import company
from support.views import VALID as VIEWS_VALID
from support.views import function_sources

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.kinetic.tool_types import input_value
from graph_cli.services.describe_tools import tool_name
from graph_cli.storage.files.audit_log import FileAuditLog

ENV = {"GRAPH_CLI_GRAPH": "example_graph", "GRAPH_CLI_SPILL_BYTES": "0"}
PUBLISHED = {
    "functions": FIXTURES / "functions" / "inventory" / "valid",
    "actions": FIXTURES / "actions" / "inventory" / "valid",
    "views": FIXTURES / "views" / "inventory" / "valid",
}


def documents() -> dict[str, Any]:
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "inv:Location", "local": place, "attrs": attrs}
        for place, attrs in (
            ("shelf", {"name": "Shelf room", "code": "SH"}),
            ("cellar", {"name": "Cellar", "code": "CE"}),
        )
    ]
    for index in range(30):
        ops.append(
            {
                "op": "create",
                "type": "inv:Item",
                "local": f"item{index}",
                "attrs": {
                    "name": f"Item number {index:02d} of the stock",
                    "condition": "damaged" if index % 3 else "good",
                },
                "edges": [
                    {"rel": "inv:located_in", "target": "@shelf" if index % 2 else "@cellar"}
                ],
            }
        )
    return {"ops": ops}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    for kind, folder in PUBLISHED.items():
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    write_documents(root, "example_graph", [documents()])
    return root


def j(value: Any) -> str:
    return json.dumps(value)


def call(run_cli: CliRunner, tool: str, *args: str, env: dict[str, str] | None = None) -> Outcome:
    return app(run_cli, "call", tool, *args, env=env or ENV)


def ok(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    outcome = app(run_cli, *args, env=env or ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def alias_of(run_cli: CliRunner, cls: str, **attrs: str) -> str:
    items = ok(run_cli, "find", "--type", cls).data["items"]
    return str(next(i for i in items if all(i["attrs"][k] == v for k, v in attrs.items()))["alias"])


def test_a_function_call_answers_like_run_with_the_page_in_data(
    run_cli: CliRunner, root: Path
) -> None:
    ran = ok(run_cli, "run", "items_by_condition", "--param", "condition=damaged")
    called = call(run_cli, "run_items_by_condition", "--input", f"condition={j('damaged')}")
    assert called.code == 0, called.envelope
    assert called.data == {**ran.data, "page": ran.envelope["page"]}
    assert "page" not in called.envelope and called.data["items"]
    assert called.envelope["graph"] == ran.envelope["graph"]
    assert called.data["page"]["total"] == len(called.data["items"]) == 20


def test_the_paging_controls_page_the_rows(run_cli: CliRunner, root: Path) -> None:
    tool, condition = "run_items_by_condition", f"condition={j('damaged')}"
    first = call(run_cli, tool, "--input", condition, "--input", "_limit=8")
    page = first.data["page"]
    assert (page["total"], page["shown"], page["limit"], page["next_offset"]) == (20, 8, 8, 8)
    second = call(
        run_cli, tool, "--input", condition, "--input", "_limit=8", "--input", "_offset=8"
    )
    ran = ok(
        run_cli,
        *("run", "items_by_condition", "--param", "condition=damaged", "--limit", "8"),
        *("--offset", "8"),
    )
    assert second.data["items"] == ran.data["items"] and second.data["page"] == {
        **ran.envelope["page"]
    }
    last = call(run_cli, tool, "--input", condition, "--input", "_offset=16")
    assert last.data["page"]["next_offset"] is None and len(last.data["items"]) == 4
    # null is not given; a whole number written as a float is a whole number
    same = call(
        run_cli, tool, "--input", condition, "--input", "_limit=null", "--input", "_offset=null"
    )
    assert same.data["page"]["limit"] == 50 and same.data["page"]["offset"] == 0
    floated = call(
        run_cli, tool, "--input", condition, "--input", "_limit=2.0", "--input", "_offset=1e0"
    )
    assert floated.data["page"]["limit"] == 2 and floated.data["page"]["offset"] == 1
    for bad in ("0", "501", "2.5", '"2"', "true", "[2]"):
        refused = call(run_cli, tool, "--input", condition, "--input", f"_limit={bad}")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), bad
        assert refused.error["details"]["argument"] == "_limit"
    refused = call(run_cli, tool, "--input", condition, "--input", "_offset=-1")
    assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument")


def test_a_call_never_moves_its_answer_to_a_file(run_cli: CliRunner, root: Path) -> None:
    small = {**ENV, "GRAPH_CLI_SPILL_BYTES": "300"}
    ran = app(run_cli, "run", "items_by_condition", "--param", "condition=damaged", env=small)
    assert ran.data is None and "output.spilled" in ran.warning_codes  # a run does
    called = call(
        run_cli, "run_items_by_condition", "--input", f"condition={j('damaged')}", env=small
    )
    assert called.data is not None and len(called.data["items"]) == 20
    assert "output.spilled" not in called.warning_codes
    page = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "262144", env=small)
    assert page.data is None  # a view does
    shown = call(run_cli, "view_item_catalog", "--input", "_page_bytes=262144", env=small)
    assert shown.data is not None and "output.spilled" not in shown.warning_codes


TYPED = """\
schema: graph_cli.function/1
name: items_acquired
summary: Items acquired on or after a date with at least a quantity.
use_when: Recently acquired items.
params:
  since: {type: date, default: "2000-01-01", summary: Earliest acquisition date.}
  min_quantity: {type: integer, default: 0, summary: Lowest quantity.}
  conditions:
    type: enum
    enum: inv:Condition
    many: true
    default: [good, damaged]
    summary: Accepted conditions.
  note: {type: string, default: "", summary: Free text.}
query: |
  MATCH (i:Item)
  WHERE i.acquired_on >= $since AND i.quantity >= $min_quantity AND i.condition IN $conditions
    AND i.name <> $note
  RETURN i.name AS item
  ORDER BY item
returns:
  - {column: item, summary: Name of the item.}
examples:
  - question: Which items were acquired this year?
"""


@pytest.fixture
def typed(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    folder = tmp_path / "typed_functions"
    folder.mkdir()
    for path in PUBLISHED["functions"].glob("*.yaml"):
        (folder / path.name).write_bytes(path.read_bytes())
    (folder / "items_acquired.yaml").write_text(TYPED)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


def test_the_input_is_read_by_the_declared_types(
    run_cli: CliRunner, root: Path, typed: None
) -> None:
    tool = "run_items_acquired"
    assert call(run_cli, tool).code == 0  # every parameter has a default
    given = call(
        run_cli,
        tool,
        *("--input", f"since={j('2026-01-31')}", "--input", "min_quantity=3.0"),
        *("--input", f"conditions={j(['good'])}"),
    )
    assert given.code == 0, given.envelope
    nulls = call(
        run_cli, tool, "--input", "since=null", "--input", "conditions=null", "--input", "note=null"
    )
    assert nulls.code == 0 and nulls.data == call(run_cli, tool).data
    for bad, code in (
        ("since=20260131", "function.parameter_invalid"),
        (f"since={j('last week')}", "function.parameter_invalid"),
        ("min_quantity=1.5", "function.parameter_invalid"),
        (f"min_quantity={j('3')}", "function.parameter_invalid"),
        ("min_quantity=true", "function.parameter_invalid"),
        (f"conditions={j('good')}", "function.parameter_invalid"),  # a list parameter takes a list
        (f"conditions={j(['unknown'])}", "function.parameter_invalid"),
        ("note=1.5", "function.parameter_invalid"),  # a float is no string
        ("note=7", "function.parameter_invalid"),
        ("other=1", "function.parameter_unknown"),
    ):
        refused = call(run_cli, tool, "--input", bad)
        assert refused.error["code"] == code, (bad, refused.envelope)
        assert refused.code == 2, bad
    refused = call(run_cli, tool, "--input", "min_quantity=1.5")
    assert refused.error["details"]["parameter"] == "min_quantity"
    assert "whole number" in refused.error["message"]


def test_a_required_parameter_cannot_be_null_and_an_unknown_input_is_refused(
    run_cli: CliRunner, root: Path
) -> None:
    tool = "run_items_by_condition"
    missing = call(run_cli, tool)
    assert (missing.code, missing.error["code"]) == (2, "function.parameter_missing")
    null = call(run_cli, tool, "--input", "condition=null")
    assert (null.code, null.error["code"]) == (2, "function.parameter_missing")
    unknown = call(run_cli, tool, "--input", f"condition={j('good')}", "--input", "conditon=1")
    assert (unknown.code, unknown.error["code"]) == (2, "function.parameter_unknown")
    assert unknown.error["details"]["did_you_mean"] == ["condition"]
    enum = call(run_cli, tool, "--input", f"condition={j('broken')}")
    assert (enum.code, enum.error["code"]) == (2, "function.parameter_invalid")
    for control in ("_reason", "_dry_run", "_entity", "_cursor", "_nothing"):
        refused = call(
            run_cli, tool, "--input", f"condition={j('good')}", "--input", f"{control}=1"
        )
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), control
        assert refused.error["details"]["expected"] == ["_limit", "_offset"]


def test_the_input_option_is_checked(run_cli: CliRunner, root: Path) -> None:
    tool = "run_items_by_condition"
    for bad in ("condition", "=1", "Bad=1", "a-b=1", "condition={oops", "condition="):
        refused = call(run_cli, tool, "--input", bad)
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), bad
        assert refused.error["details"]["argument"] == "--input"
    twice = call(
        run_cli, tool, "--input", f"condition={j('good')}", "--input", f"condition={j('new')}"
    )
    assert (twice.code, twice.error["code"]) == (2, "usage.invalid_argument")
    assert "given twice" in twice.error["message"]
    key = call(run_cli, tool, "--input", f"condition={j('good')}", "--idempotency-key", "k-1")
    assert (key.code, key.error["code"]) == (2, "usage.invalid_argument")
    assert key.error["details"]["argument"] == "--idempotency-key"
    assert "dry run" in key.error["message"] or "read" in key.error["message"]


def test_a_reference_is_an_id_an_alias_or_trailing_hex_and_no_name(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    shelf_id = ok(run_cli, "get", shelf).data["entity"]["id"]
    ran = ok(run_cli, "run", "items_in_location", "--param", f"location={shelf}")
    assert ran.data["items"]
    for ref in (shelf, shelf_id, shelf_id.replace("-", "")[-12:]):
        called = call(run_cli, "run_items_in_location", "--input", f"location={j(ref)}")
        assert called.code == 0 and called.data["items"] == ran.data["items"], ref
    item = alias_of(run_cli, "inv:Item", condition="good")
    wrong = call(run_cli, "run_items_in_location", "--input", f"location={j(item)}")
    assert (wrong.code, wrong.error["code"]) == (1, "function.parameter_type")
    assert wrong.error["details"]["expected"] == "inv:Location"
    gone = call(run_cli, "run_items_in_location", "--input", f"location={j('0192f3c0')}")
    assert (gone.code, gone.error["code"]) == (1, "entity.not_found")
    named = call(run_cli, "run_items_in_location", "--input", f"location={j('Shelf room')}")
    assert (named.code, named.error["code"]) == (2, "function.parameter_invalid")
    assert "looked up first" in named.error["message"]
    number = call(run_cli, "run_items_in_location", "--input", "location=12")
    assert (number.code, number.error["code"]) == (2, "function.parameter_invalid")


# ---- actions ---------------------------------------------------------------------------------


def move_input(run_cli: CliRunner, item: str, place: str) -> list[str]:
    return ["--input", f"item={j(item)}", "--input", f"location={j(place)}"]


def test_an_action_call_answers_like_do_dry_run_and_real(run_cli: CliRunner, root: Path) -> None:
    item = alias_of(run_cli, "inv:Item", condition="good")
    where = ok(run_cli, "neighbors", item, "--rel", "inv:located_in").data["items"]
    here = where[0]["entity"]["alias"]
    codes = (
        alias_of(run_cli, "inv:Location", code="SH"),
        alias_of(run_cli, "inv:Location", code="CE"),
    )
    place = next(alias for alias in codes if alias != here)
    by_do = ok(
        run_cli,
        *("do", "move_item", "--param", f"item={item}", "--param", f"location={place}"),
        "--dry-run",
    )
    inputs = move_input(run_cli, item, place)
    dry = call(run_cli, "do_move_item", *inputs, "--input", "_dry_run=true")
    assert dry.code == 0, dry.envelope
    assert dry.data == by_do.data and dry.data["dry_run"] is True
    assert dry.envelope["graph"] == by_do.envelope["graph"]
    written = call(run_cli, "do_move_item", *inputs)
    assert written.code == 0, written.envelope
    assert written.data["dry_run"] is False and written.data["seq"] > 0
    assert written.data["action"] == by_do.data["action"]
    now = ok(run_cli, "neighbors", item, "--rel", "inv:located_in").data["items"]
    assert [i["entity"]["alias"] for i in now] == [place]
    # null is not given: _dry_run null is the default, a real run (which now has nothing to do)
    again = call(
        run_cli, "do_move_item", *inputs, "--input", "_dry_run=null", "--input", "_reason=null"
    )
    assert again.code == 0 and again.data["dry_run"] is False


def test_a_reason_is_a_control_and_the_errors_are_those_of_do(
    run_cli: CliRunner, root: Path
) -> None:
    item = alias_of(run_cli, "inv:Item", condition="good")
    base = ["--input", f"item={j(item)}"]
    missing = call(run_cli, "do_retire_item", *base)
    assert (missing.code, missing.error["code"]) == (2, "action.reason_required")
    for bad in ('"  "', "7", "true", '""', "1.5", "2.0"):  # a number is no string
        refused = call(run_cli, "do_retire_item", *base, "--input", f"_reason={bad}")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), bad
        assert refused.error["details"]["argument"] == "_reason"
    dry = call(
        run_cli,
        "do_retire_item",
        *base,
        "--input",
        f"_reason={j('scrapped')}",
        "--input",
        "_dry_run=true",
    )
    assert dry.code == 0 and dry.data["dry_run"] is True
    done = call(run_cli, "do_retire_item", *base, "--input", f"_reason={j('scrapped')}")
    assert done.code == 0, done.envelope
    entity_id = ok(run_cli, "get", item).data["entity"]["id"]
    history = ok(run_cli, "history", item).data["items"]
    assert history[0]["reason"] == "scrapped" and history[0]["command"] == "do"
    # the state decides: the same call is now refused, and a dry run raises the same error
    for extra in ((), ("--input", "_dry_run=true")):
        refused = call(run_cli, "do_retire_item", *base, "--input", f"_reason={j('again')}", *extra)
        assert (refused.code, refused.error["code"]) == (1, "action.precondition_failed"), extra
        (failed,) = refused.error["details"]["failed"]
        assert failed["precondition"] == "not_retired" and failed["entity"] == entity_id
    for control in ("_limit", "_entity", "_cursor"):
        refused = call(run_cli, "do_retire_item", *base, "--input", f"{control}=1")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), control
        assert refused.error["details"]["expected"] == ["_reason", "_dry_run"]
    wrong = call(
        run_cli,
        "do_retire_item",
        "--input",
        f"item={j(alias_of(run_cli, 'inv:Location', code='SH'))}",
        "--input",
        f"_reason={j('x')}",
    )
    assert (wrong.code, wrong.error["code"]) == (1, "action.parameter_type")
    flag = call(run_cli, "do_retire_item", *base, "--input", "_dry_run=1")
    assert (flag.code, flag.error["code"]) == (2, "usage.invalid_argument")


# ---- idempotency ------------------------------------------------------------------------------


def test_a_key_makes_an_action_call_repeatable_and_is_the_key_of_do(
    run_cli: CliRunner, root: Path
) -> None:
    item = alias_of(run_cli, "inv:Item", condition="good")
    other = alias_of(run_cli, "inv:Item", condition="damaged")
    place = alias_of(run_cli, "inv:Location", code="SH")
    first = call(
        run_cli, "do_move_item", *move_input(run_cli, item, place), "--idempotency-key", "k-1"
    )
    assert first.code == 0, first.envelope
    again = call(
        run_cli, "do_move_item", *move_input(run_cli, item, place), "--idempotency-key", "k-1"
    )
    assert again.data["idempotent_replay"] is True and again.data["tx_id"] == first.data["tx_id"]
    # the same bound request through do is a replay of the call, and the other way round
    by_do = ok(
        run_cli,
        *("do", "move_item", "--param", f"item={item}", "--param", f"location={place}"),
        *("--idempotency-key", "k-1"),
    )
    assert by_do.data["idempotent_replay"] is True and by_do.data["tx_id"] == first.data["tx_id"]
    ok(
        run_cli,
        *("do", "move_item", "--param", f"item={other}", "--param", f"location={place}"),
        *("--idempotency-key", "k-2"),
    )
    swapped = call(
        run_cli, "do_move_item", *move_input(run_cli, other, place), "--idempotency-key", "k-2"
    )
    assert swapped.code == 0 and swapped.data["idempotent_replay"] is True
    # another input under the same key is a conflict, whichever way it arrives
    reused = call(
        run_cli, "do_move_item", *move_input(run_cli, other, place), "--idempotency-key", "k-1"
    )
    assert (reused.code, reused.error["code"]) == (4, "conflict.idempotency_key_reuse")
    by_do_reused = app(
        run_cli,
        *("do", "move_item", "--param", f"item={other}", "--param", f"location={place}"),
        *("--idempotency-key", "k-1"),
        env=ENV,
    )
    assert by_do_reused.error["code"] == "conflict.idempotency_key_reuse"
    bad = call(
        run_cli, "do_move_item", *move_input(run_cli, item, place), "--idempotency-key", "bad key"
    )
    assert (bad.code, bad.error["code"]) == (2, "usage.invalid_argument")


def test_a_read_takes_no_key(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    item = alias_of(run_cli, "inv:Item", condition="good")
    reads = (
        ("run_items_by_condition", [f"condition={j('good')}"]),
        ("view_location_overview", [f"_entity={j(shelf)}"]),
        ("view_item_catalog", []),
        ("do_move_item", [f"item={j(item)}", f"location={j(shelf)}", "_dry_run=true"]),
        ("do_nothing", ["_dry_run=true"]),  # a dry run is a read, whatever the tool
    )
    for tool, inputs in reads:
        args = [x for pair in inputs for x in ("--input", pair)]
        refused = call(run_cli, tool, *args, "--idempotency-key", "k-9")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), tool
        assert refused.error["details"]["argument"] == "--idempotency-key"
    # nothing was written by the refused calls, and a real run with the key goes through
    assert (
        call(
            run_cli,
            "do_move_item",
            "--input",
            f"item={j(item)}",
            "--input",
            f"location={j(shelf)}",
            "--idempotency-key",
            "k-9",
        ).code
        == 0
    )


# ---- tool names, the hash, what the caller may use ---------------------------------------------


def manifest(run_cli: CliRunner, env: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    tools = ok(run_cli, "describe", "tools", env=env).data["tools"]
    return {t["name"]: t for t in tools}


def test_an_unknown_tool_is_not_found_with_the_names_that_are_close(
    run_cli: CliRunner, root: Path
) -> None:
    missing = call(run_cli, "run_items_by_conditon", "--input", f"condition={j('good')}")
    assert (missing.code, missing.error["code"]) == (1, "tool.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "run_items_by_condition"
    assert missing.error["hint"] == "graph describe tools"
    for tool in ("items_by_condition", "run_", "move_item", "view_move_item", "lookup"):
        assert call(run_cli, tool).error["code"] == "tool.not_found", tool
    assert (
        call(run_cli, "do_move_item", env={**ENV, "GRAPH_CLI_GRAPH": "nowhere"}).error["code"]
        == "graph.not_found"
    )


def test_the_hash_of_the_manifest_guards_the_call(run_cli: CliRunner, root: Path) -> None:
    tools = manifest(run_cli)
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    item = alias_of(run_cli, "inv:Item", condition="good")
    cases = {
        "run_items_by_condition": [f"condition={j('good')}"],
        "do_retire_item": [f"item={j(item)}", f"_reason={j('x')}", "_dry_run=true"],
        "view_location_overview": [f"_entity={j(shelf)}"],
        "view_item_catalog": [],
    }
    for name, inputs in cases.items():
        args = [x for pair in inputs for x in ("--input", pair)]
        good = call(run_cli, name, *args, "--expect-hash", tools[name]["hash"])
        assert good.code == 0, (name, good.envelope)
        stale = call(run_cli, name, *args, "--expect-hash", "sha256:" + "0" * 64)
        assert (stale.code, stale.error["code"]) == (4, "tool.hash_mismatch"), name
        assert stale.error["details"] == {
            "tool": name,
            "expected": "sha256:" + "0" * 64,
            "actual": tools[name]["hash"],
        }
        assert stale.error["hint"] == "graph describe tools"
    # nothing ran: a write refused for its hash writes nothing
    refused = call(
        run_cli,
        "do_retire_item",
        "--input",
        f"item={j(item)}",
        "--input",
        f"_reason={j('x')}",
        "--expect-hash",
        "sha256:" + "1" * 64,
    )
    assert refused.error["code"] == "tool.hash_mismatch"
    assert ok(run_cli, "get", item).data["entity"]["attrs"]["status"] != "retired"
    unknown = call(run_cli, "run_nothing", "--expect-hash", "sha256:" + "0" * 64)
    assert unknown.error["code"] == "tool.not_found"  # the name is judged first


def test_the_hash_follows_what_the_caller_is_given(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    item = alias_of(run_cli, "inv:Item", condition="good")
    before = manifest(run_cli)["do_move_item"]["hash"]
    policy = READS_ITEMS.replace(
        "no_project", "obligations:\n  - {action: [write.unlink], require_reason: true}\nno_project"
    )
    install(run_cli, tmp_path / "policies", policy.replace("reads: [inv:Item]\n    ", ""))
    after = manifest(run_cli)["do_move_item"]["hash"]
    assert after != before  # the policy now demands a reason: the tool changed
    place = alias_of(run_cli, "inv:Location", code="SH")
    stale = call(
        run_cli, "do_move_item", *move_input(run_cli, item, place), "--expect-hash", before
    )
    assert stale.error["code"] == "tool.hash_mismatch" and stale.error["details"]["actual"] == after
    current = call(
        run_cli,
        "do_move_item",
        *move_input(run_cli, item, place),
        *("--input", f"_reason={j('tidy')}", "--expect-hash", after),
    )
    assert current.code == 0, current.envelope


READS_ITEMS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [inv:Item]
    writes:
      inv:Item: {transition: true, link: true, unlink: true}
no_project: {"*": write}
"""
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
NO_EXPLORE = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""


@pytest.mark.doc_claim("security_model.tool_call_is_the_command")
def test_a_tool_the_caller_may_not_use_is_denied_not_hidden(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    install(run_cli, tmp_path / "policies", NO_EXPLORE)
    item = alias_of(run_cli, "inv:Item", condition="good")
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    listed = {t["name"] for t in manifest(run_cli, EXPLORE).values()}
    assert "run_items_by_condition" not in listed and "do_move_item" not in listed
    for tool, inputs in (
        ("run_items_by_condition", [f"condition={j('good')}"]),
        ("do_move_item", [f"item={j(item)}", f"location={j(shelf)}"]),
        ("do_move_item", [f"item={j(item)}", f"location={j(shelf)}", "_dry_run=true"]),
        ("view_item_catalog", []),  # it runs a function and lists entities
    ):
        args = [x for pair in inputs for x in ("--input", pair)]
        denied = call(run_cli, tool, *args, env=EXPLORE)
        assert (denied.code, denied.error["code"]) == (3, "access.denied"), tool
        # the hash is not asked for a tool the caller may not use: the denial comes first
        hashed = call(run_cli, tool, *args, "--expect-hash", "sha256:" + "0" * 64, env=EXPLORE)
        assert hashed.error["code"] == "access.denied", tool
    by_run = app(run_cli, "run", "items_by_condition", "--param", "condition=good", env=EXPLORE)
    assert by_run.error["code"] == "access.denied"
    # another caller of the same graph still has them
    assert call(run_cli, "run_items_by_condition", "--input", f"condition={j('good')}").code == 0


NO_GET = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_views, agent_type: [explore], action: ["read.get"]}
"""


@pytest.mark.doc_claim("security_model.tool_call_is_the_command")
@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_view_tool_needs_read_get_like_the_view_command(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    install(run_cli, tmp_path / "policies", NO_GET)
    seen = manifest(run_cli, EXPLORE)
    assert {n for n in seen if n.startswith("view_")} == set()  # none is a tool for this caller
    assert "run_items_by_condition" in seen and "do_move_item" in seen
    for name, inputs in (
        ("view_location_overview", [f"_entity={j(shelf)}"]),
        ("view_item_catalog", []),
        ("view_items_in_location", [f"location={j(shelf)}"]),
    ):
        args = [x for pair in inputs for x in ("--input", pair)]
        denied = call(run_cli, name, *args, env=EXPLORE)
        assert (denied.code, denied.error["code"]) == (3, "access.denied"), name
        hashed = call(run_cli, name, *args, "--expect-hash", "sha256:" + "0" * 64, env=EXPLORE)
        assert hashed.error["code"] == "access.denied", name  # the denial comes first
        assert last_refusal(root)["request"]["via"] == "call", name
    listed = ok(run_cli, "describe", "views", "--include-not-allowed", env=EXPLORE)
    assert [v["missing"][0]["action"] for v in listed.data["views"]] == ["read.get"] * 3
    plain = app(run_cli, "view", shelf, "--name", "location_overview", env=EXPLORE)
    assert plain.error["code"] == "access.denied"
    # the other listings follow: search marks them, the counts call them not allowed
    found = ok(run_cli, "describe", "search", "location", "--kind", "view", env=EXPLORE)
    assert (
        [(h["allowed"], h["missing"][0]["action"]) for h in found.data["items"]]
        == [(False, "read.get")] * len(found.data["items"])
        != []
    )
    modules = ok(run_cli, "describe", "modules", env=EXPLORE).data["modules"]
    (inventory,) = [m for m in modules if m["id"] == "inventory"]
    assert (inventory["views"], inventory["not_allowed"]) == (0, {"views": 3})
    (counted,) = ok(run_cli, "start", env=EXPLORE).data["graphs"]
    assert (counted["counts"]["views"], counted["not_allowed"]) == (0, {"views": 3})
    # another caller of the same graph has them
    assert "view_location_overview" in manifest(run_cli)
    assert call(run_cli, "view_location_overview", "--input", f"_entity={j(shelf)}").code == 0


@pytest.mark.doc_claim("security_model.tool_call_is_the_command")
def test_a_tool_the_caller_cannot_see_is_not_found(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    item = alias_of(run_cli, "inv:Item", condition="good")
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    install(run_cli, tmp_path / "policies", READS_ITEMS)
    for tool, inputs in (
        ("do_move_item", [f"item={j(item)}", f"location={j(shelf)}"]),
        ("run_items_in_location", [f"location={j(shelf)}"]),
        ("view_location_overview", [f"_entity={j(shelf)}"]),
    ):
        args = [x for pair in inputs for x in ("--input", pair)]
        missing = call(run_cli, tool, *args)
        assert (missing.code, missing.error["code"]) == (1, "tool.not_found"), tool
        assert tool not in missing.error["details"]["did_you_mean"]
    assert "Location" not in json.dumps(call(run_cli, "run_items_in_location").envelope)
    seen = call(run_cli, "run_items_by_condition", "--input", f"condition={j('good')}")
    assert seen.code == 0
    nothing = call(
        run_cli, "run_items_by_condition", env={**ENV, "GRAPH_CLI_GRAPH": "example_graph"}
    )
    assert nothing.error["code"] == "function.parameter_missing"


# ---- views ------------------------------------------------------------------------------------


def test_an_entity_view_call_answers_like_view(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    shelf_id = ok(run_cli, "get", shelf).data["entity"]["id"]
    shown = ok(run_cli, "view", shelf, "--name", "location_overview")
    for ref in (shelf, shelf_id):
        called = call(run_cli, "view_location_overview", "--input", f"_entity={j(ref)}")
        assert called.code == 0, called.envelope
        assert called.data == shown.data and called.envelope["graph"] == shown.envelope["graph"]
    missing = call(run_cli, "view_location_overview")
    assert (missing.code, missing.error["code"]) == (2, "usage.missing_argument")
    null = call(run_cli, "view_location_overview", "--input", "_entity=null")
    assert null.error["code"] == "usage.missing_argument"
    item = alias_of(run_cli, "inv:Item", condition="good")
    wrong = call(run_cli, "view_location_overview", "--input", f"_entity={j(item)}")
    assert (wrong.code, wrong.error["code"]) == (1, "view.class_mismatch")
    gone = call(run_cli, "view_location_overview", "--input", f"_entity={j('0192f3c0')}")
    assert (gone.code, gone.error["code"]) == (1, "entity.not_found")
    for bad in ("1.5", "7", "true", '"Some Name"', '"not an id"'):  # no reference
        refused = call(run_cli, "view_location_overview", "--input", f"_entity={bad}")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), bad
        assert refused.error["details"]["argument"] == "_entity", bad  # the control, not ref
        assert refused.error["message"].startswith("_entity:"), refused.error["message"]
    for control, value in (("_cursor", '"x"'), ("_page_bytes", "2048"), ("_limit", "1")):
        refused = call(
            run_cli,
            "view_location_overview",
            "--input",
            f"_entity={j(shelf)}",
            "--input",
            f"{control}={value}",
        )
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), control
        assert refused.error["details"]["expected"] == ["_entity"]


def test_a_set_view_call_pages_by_cursor(run_cli: CliRunner, root: Path) -> None:
    whole = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "1500")
    first = call(run_cli, "view_item_catalog", "--input", "_page_bytes=1500")
    assert first.code == 0, first.envelope
    assert first.data == whole.data and first.data["more"] is True and first.data["next"]
    second = call(
        run_cli,
        *("view_item_catalog", "--input", "_page_bytes=1500.0"),
        *("--input", f"_cursor={j(first.data['next'])}"),
    )
    by_view = ok(
        run_cli,
        *("view", "--name", "item_catalog", "--page-bytes", "1500", "--cursor", first.data["next"]),
    )
    assert second.data == by_view.data
    for control, value in (("_page_bytes", "0"), ("_page_bytes", "2.5"), ("_page_bytes", "262145")):
        refused = call(run_cli, "view_item_catalog", "--input", f"{control}={value}")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), value
        assert refused.error["details"]["argument"] == "_page_bytes", value  # not --page-bytes
    for bad in ("1.5", "2.0", "7", "true"):  # a number is no cursor
        refused = call(run_cli, "view_item_catalog", "--input", f"_cursor={bad}")
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), bad
        assert refused.error["details"]["argument"] == "_cursor"
    cursor = call(run_cli, "view_item_catalog", "--input", f"_cursor={j('nonsense')}")
    assert cursor.error["code"] == "view.cursor_invalid"
    nulls = call(
        run_cli, "view_item_catalog", "--input", "_cursor=null", "--input", "_page_bytes=null"
    )
    assert nulls.data == ok(run_cli, "view", "--name", "item_catalog").data
    entity = call(run_cli, "view_item_catalog", "--input", f"_entity={j('SH')}")
    assert (entity.code, entity.error["code"]) == (2, "usage.invalid_argument")


def test_a_view_with_a_reference_parameter(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    item = alias_of(run_cli, "inv:Item", condition="good")
    shown = ok(run_cli, "view", "--name", "items_in_location", "--param", f"location={shelf}")
    called = call(run_cli, "view_items_in_location", "--input", f"location={j(shelf)}")
    assert called.code == 0 and called.data == shown.data
    wrong = call(run_cli, "view_items_in_location", "--input", f"location={j(item)}")
    assert (wrong.code, wrong.error["code"]) == (1, "view.parameter_type")
    missing = call(run_cli, "view_items_in_location")
    assert (missing.code, missing.error["code"]) == (2, "view.parameter_missing")
    unknown = call(run_cli, "view_items_in_location", "--input", f"locaton={j(shelf)}")
    assert unknown.error["code"] == "view.parameter_unknown"


# ---- text, audit, access log, the skill ---------------------------------------------------------


def test_the_text_form_is_that_of_the_command(run_cli: CliRunner, root: Path) -> None:
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    pairs = (
        (
            ("run", "items_in_location", "--param", f"location={shelf}"),
            ("call", "run_items_in_location", "--input", f"location={j(shelf)}"),
        ),
        (
            ("view", shelf, "--name", "location_overview"),
            ("call", "view_location_overview", "--input", f"_entity={j(shelf)}"),
        ),
    )
    for plain, called in pairs:
        expected = run_cli("graph", *plain, env=ENV)
        text = run_cli("graph", *called, env=ENV)
        assert text.returncode == 0 and text.stderr == "", text.stderr
        assert text.stdout == expected.stdout
    done = run_cli(
        "graph",
        *("call", "do_move_item", "--input", "_dry_run=true"),
        *("--input", f"item={j(alias_of(run_cli, 'inv:Item', condition='good'))}"),
        *("--input", f"location={j(shelf)}"),
        env=ENV,
    )
    assert done.returncode == 0 and "dry_run: true" in done.stdout


def events(root: Path, run_cli: CliRunner) -> list[dict[str, Any]]:
    graph_id = admin(run_cli, "graph", "show", "example_graph").data["id"]
    stream = graph_stream(graph_id)
    return [
        dict(e) for e in verify_stream(FileAuditLog(root).read_lines(stream), stream=stream).events
    ]


def test_the_audit_names_the_underlying_command_and_that_a_call_made_it(
    run_cli: CliRunner, root: Path
) -> None:
    item = alias_of(run_cli, "inv:Item", condition="good")
    other = alias_of(run_cli, "inv:Item", condition="damaged")
    place = alias_of(run_cli, "inv:Location", code="SH")
    called = call(
        run_cli, "do_move_item", *move_input(run_cli, item, place), "--idempotency-key", "k-1"
    )
    by_do = ok(
        run_cli, *("do", "move_item", "--param", f"item={other}", "--param", f"location={place}")
    )
    assert called.code == 0
    intents = [e for e in events(root, run_cli) if e["request"]["command"] == "do"]
    via_call, via_do = intents[-2:]
    assert via_call["request"]["command"] == "do" and via_call["request"]["via"] == "call"
    assert via_call["request"]["action"] == called.data["action"]
    assert "via" not in via_do["request"] and via_do["request"]["action"] == by_do.data["action"]
    # the record of the transaction carries it too, and the history says "do"
    history = ok(run_cli, "history", item).data["items"]
    assert history[0]["command"] == "do" and history[0]["action"] == called.data["action"]
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0 and verified.data["counts"]["error"] == 0


def last_refusal(root: Path) -> dict[str, Any]:
    lines = list(FileAuditLog(root).read_lines(INSTALLATION_STREAM))
    last: dict[str, Any] = json.loads(lines[-1].data)
    return last


@pytest.mark.doc_claim("security_model.tool_call_is_the_command")
def test_a_refused_call_is_audited_with_the_underlying_command(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    install(run_cli, tmp_path / "policies", NO_EXPLORE)
    item = alias_of(run_cli, "inv:Item", condition="good")
    place = alias_of(run_cli, "inv:Location", code="SH")
    denied = call(run_cli, "do_move_item", *move_input(run_cli, item, place), env=EXPLORE)
    assert denied.error["code"] == "access.denied"
    last = last_refusal(root)
    assert last["request"]["command"] == "do" and last["request"]["via"] == "call"
    read = call(run_cli, "run_items_by_condition", "--input", f"condition={j('good')}", env=EXPLORE)
    assert read.error["code"] == "access.denied"
    last = last_refusal(root)
    assert last["request"]["command"] == "run" and last["request"]["via"] == "call"
    shown = call(run_cli, "view_item_catalog", env=EXPLORE)  # it runs functions: not allowed
    assert shown.error["code"] == "access.denied"
    last = last_refusal(root)
    assert last["request"]["command"] == "view" and last["request"]["via"] == "call"
    plain = app(run_cli, "run", "items_by_condition", "--param", "condition=good", env=EXPLORE)
    assert plain.error["code"] == "access.denied"
    last = last_refusal(root)
    assert last["request"]["command"] == "run" and "via" not in last["request"]


ONLY_OTHER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: [other_graph], level: write}
no_project: {"*": write}
"""


@pytest.mark.doc_claim("security_model.tool_call_is_the_command")
def test_a_graph_the_caller_cannot_see_is_not_found_like_for_the_command(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "other_graph", "inventory")
    install(run_cli, tmp_path / "policies", ONLY_OTHER)
    hidden = call(run_cli, "run_items_by_condition", "--input", f"condition={j('good')}")
    assert (hidden.code, hidden.error["code"]) == (1, "graph.not_found")
    assert last_refusal(root)["request"]["command"] == "call"  # no tool was looked up yet
    by_run = app(run_cli, "run", "items_by_condition", "--param", "condition=good", env=ENV)
    assert by_run.error["code"] == "graph.not_found"
    assert last_refusal(root)["request"]["command"] == "run"


def enable_logs(env: dict[str, str]) -> Path:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    return Path(env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"


MARKER = "call-marker-4b9d"


def test_the_access_log_records_the_command_a_call_ran_without_a_value(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    log = enable_logs(isolated_env)
    item = alias_of(run_cli, "inv:Item", condition="good")
    shelf = alias_of(run_cli, "inv:Location", code="SH")
    shown = call(run_cli, "run_items_by_condition", "--input", f"condition={j('good')}")
    failed = call(run_cli, "run_items_in_location", "--input", f"location={j('0192f3c0')}")
    moved = call(
        run_cli,
        "do_move_item",
        *move_input(run_cli, item, shelf),
        *("--input", f"_reason={j(MARKER)}"),
    )
    seen = call(run_cli, "view_location_overview", "--input", f"_entity={j(shelf)}")
    stale = call(run_cli, "view_item_catalog", "--expect-hash", "sha256:" + "0" * 64)
    control = call(run_cli, "run_items_by_condition", "--input", "_dry_run=true")
    assert (stale.error["code"], control.error["code"]) == (
        "tool.hash_mismatch",
        "usage.invalid_argument",
    )
    absent = call(run_cli, f"run_{MARKER}")
    assert failed.error["code"] == "entity.not_found" and absent.error["code"] == "tool.not_found"
    assert (shown.code, moved.code, seen.code) == (0, 0, 0)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    ran, bad, did, viewed, mismatch, wrong, unknown = lines[-7:]
    assert (mismatch["command"], mismatch["via"]) == ("view", "call")
    assert (wrong["command"], wrong["via"]) == ("run", "call")  # the log says what ran
    assert (ran["command"], ran["via"], ran["result"]) == ("run", "call", {"status": "ok"})
    assert ran["function"]["name"] == "items_by_condition" and ran["function"]["hash"]
    assert (bad["command"], bad["via"], bad["result"]["code"]) == (
        "run",
        "call",
        "entity.not_found",
    )
    assert bad["function"]["name"] == "items_in_location"
    assert (did["command"], did["via"]) == ("do", "call") and did["action"]["name"] == "move_item"
    assert (viewed["command"], viewed["via"]) == ("view", "call")
    assert viewed["view"]["name"] == "location_overview"
    assert (unknown["command"], unknown["result"]["code"]) == ("call", "tool.not_found")
    assert "via" not in unknown
    plain = ok(run_cli, "run", "items_by_condition", "--param", "condition=good")
    assert plain.code == 0 and "via" not in json.loads(log.read_text().splitlines()[-1])
    for value in (item, shelf, MARKER):
        assert value not in log.read_text(), value


# ---- the examples of the company graph ----------------------------------------------------------

COMPANY = {"GRAPH_CLI_GRAPH": "company_graph", "GRAPH_CLI_SPILL_BYTES": "0"}


def apply_seeds(run_cli: CliRunner, version: str) -> None:
    seeds = (EXAMPLES / "company" / "seeds" / f"{version}.jsonl").read_text()
    applied = app(run_cli, "apply", "-", env=COMPANY, input=seeds)
    assert applied.code == 0, applied.envelope


@pytest.fixture
def company_root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """The example graph ``company`` at 1.1.0 with its seeds and the catalogs the tool tests use."""
    root = init(run_cli, isolated_env)
    ontology = EXAMPLES / "company" / "ontology"
    created = admin(
        run_cli,
        *("graph", "create", "company_graph"),
        *("--ontology", str(ontology / "1.0.0"), "--module", "company"),
    )
    assert created.code == 0, created.envelope
    apply_seeds(run_cli, "1.0.0")
    released = admin(
        run_cli,
        *("ontology", "release", "company_graph", str(ontology / "1.1.0")),
        *("--module", "company"),
    )
    assert released.code == 0, released.envelope
    apply_seeds(run_cli, "1.1.0")
    functions = tmp_path / "company_functions"
    functions.mkdir()
    for name, text in function_sources().items():
        (functions / name).write_text(text)
    for kind, folder in (
        ("functions", functions),
        ("actions", EXAMPLES / "company" / "actions"),
        ("views", VIEWS_VALID),
    ):
        published = admin(run_cli, "graph", kind, "publish", "company_graph", str(folder))
        assert published.code == 0, published.envelope
    return root


def plain(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema of a property without the ``null`` of "not given"."""
    options = [o for o in schema.get("anyOf", [schema]) if o.get("type") != "null"]
    return options[0] if len(options) == 1 and "anyOf" in schema else schema


def reference_class(schema: dict[str, Any]) -> str | None:
    """The class of a reference property (or a list of them), ``None`` for any other."""
    schema = plain(schema)
    schema = schema.get("items", schema)
    found = schema.get("x-graph-cli", {}).get("class")
    return str(found) if found else None


def an_entity(run_cli: CliRunner, cls: str) -> str:
    items = app(run_cli, "find", "--type", cls, "--limit", "1", env=COMPANY).data["items"]
    assert items, cls
    return str(items[0]["alias"])


def test_every_example_of_the_company_entries_runs_through_call(
    run_cli: CliRunner, company_root: Path
) -> None:
    """The examples of functions and views as typed input, with the references they leave out
    added: valid against the input schema of their tool, run by ``call``, and the answer valid
    against the output schema of the tool."""
    manifest = {t["name"]: t for t in app(run_cli, "describe", "tools", env=COMPANY).data["tools"]}
    catalog = company()
    ran = 0
    for kind, entries in (("function", catalog.functions), ("view", catalog.views)):
        for entry in entries:
            tool = manifest[tool_name(kind, entry.name)]
            kinds = {p.name: p.type for p in entry.defn.params}
            properties = tool["input_schema"]["properties"]
            for example in entry.defn.examples:
                given = {k: input_value(kinds[k], v) for k, v in dict(example.params).items()}
                for name in tool["input_schema"]["required"]:
                    if name not in given:
                        cls = reference_class(properties[name])
                        assert cls, (tool["name"], name)
                        ref = an_entity(run_cli, cls)
                        many = plain(properties[name]).get("type") == "array"
                        given[name] = [ref] if many else ref
                errors = list(Draft202012Validator(tool["input_schema"]).iter_errors(given))
                assert errors == [], (tool["name"], given, [e.message for e in errors])
                args = [x for k, v in given.items() for x in ("--input", f"{k}={j(v)}")]
                outcome = call(run_cli, tool["name"], *args, env=COMPANY)
                assert outcome.code == 0, (tool["name"], given, outcome.envelope)
                answer = Draft202012Validator(tool["output_schema"])
                found = list(answer.iter_errors(outcome.data))
                assert found == [], (tool["name"], [e.message for e in found])
                ran += 1
    assert ran >= 8


def test_a_small_stored_decimal_is_shown_in_plain_notation_and_validates(
    run_cli: CliRunner, company_root: Path
) -> None:
    """A decimal attribute holds ``0.0000001``; the engine reads it as a ``Decimal``, which
    ``str`` writes as ``1E-7``. The cell is the grammar of a decimal and the column schema accepts
    it, and the entity object next to it shows the stored text."""
    shares = app(run_cli, "find", "--type", "leg:Shareholding", env=COMPANY).data["items"]
    stake = shares[0]
    updated = app(
        run_cli,
        "apply",
        "-",
        env=COMPANY,
        input=json.dumps(
            {"ops": [{"op": "update", "id": stake["alias"], "set": {"percent": "0.0000001"}}]}
        ),
    )
    assert updated.code == 0, updated.envelope
    entities = [
        str(e["alias"])
        for e in app(run_cli, "find", "--type", "leg:LegalEntity", env=COMPANY).data["items"]
    ]
    tool = {t["name"]: t for t in app(run_cli, "describe", "tools", env=COMPANY).data["tools"]}[
        "run_shareholders_of"
    ]
    args = ["--input", f"entity={j(entities)}"]
    called = call(run_cli, "run_shareholders_of", *args, env=COMPANY)
    assert called.code == 0, called.envelope
    percents = [row["percent"] for row in called.data["items"]]
    assert "0.0000001" in percents and not any("E" in p for p in percents)
    assert Draft202012Validator(tool["output_schema"]).is_valid(called.data)


def test_every_action_of_the_company_runs_as_a_dry_run_with_its_references(
    run_cli: CliRunner, company_root: Path
) -> None:
    """Each action with a reference for every reference parameter and a fixed value for the rest:
    valid against the input schema, answered like ``do --dry-run`` (the state may refuse it, with
    the code of ``do``)."""
    manifest = {t["name"]: t for t in app(run_cli, "describe", "tools", env=COMPANY).data["tools"]}
    seen = 0
    for name, tool in manifest.items():
        if tool["kind"] != "action":
            continue
        properties = tool["input_schema"]["properties"]
        given: dict[str, Any] = {"_dry_run": True, "_reason": "check"}
        for param, schema in properties.items():
            if param.startswith("_"):
                continue
            cls = reference_class(schema)
            if cls:
                ref = an_entity(run_cli, cls)
                given[param] = [ref] if plain(schema).get("type") == "array" else ref
            elif plain(schema).get("format") == "date":
                given[param] = "2026-03-01"
            else:
                pytest.fail(f"no value for {name}.{param}: {schema}")
        errors = list(Draft202012Validator(tool["input_schema"]).iter_errors(given))
        assert errors == [], (name, given, [e.message for e in errors])
        args = [x for k, v in given.items() for x in ("--input", f"{k}={j(v)}")]
        called = call(run_cli, name, *args, env=COMPANY)
        pairs = [
            x
            for k, v in given.items()
            if not k.startswith("_")
            for x in ("--param", f"{k}={v}" if not isinstance(v, list) else f"{k}={v[0]}")
        ]
        by_do = app(
            run_cli,
            *("do", name.removeprefix("do_"), *pairs, "--dry-run", "--reason", "check"),
            env=COMPANY,
        )
        assert called.code == by_do.code, (name, called.envelope, by_do.envelope)
        if by_do.code == 0:
            assert called.data == by_do.data
            found = list(Draft202012Validator(tool["output_schema"]).iter_errors(called.data))
            assert found == [], (name, [e.message for e in found])
        else:
            assert called.error["code"] == by_do.error["code"], name
        seen += 1
    assert seen == 3
