"""S: the ``lookup`` tool through ``describe tools`` and ``graph call`` (tool manifest plan TM5,
Q5): when it is listed, that a call answers what ``lookup --for`` answers for the slot ``for``
names, the input rules of the tool, and what a caller who sees or may do less is given.

The graph is the ``company`` graph of ``support.lookup_graph``: a department and a team of one
name, functions, actions and views with reference inputs.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from jsonschema import Draft202012Validator
from support.admin import Outcome, admin, init
from support.app import app, create_graph
from support.fixtures import EXAMPLES
from support.functions import VALID as FUNCTION_FIXTURES
from support.lookup_graph import ENV, EXPLORE, NAME, build
from support.output_schemas import closed
from support.read_limits import install

from graph_cli.query.lookup import MAX_LOOKUP_TEXTS


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    return build(run_cli, isolated_env, tmp_path)


def j(value: Any) -> str:
    return json.dumps(value)


def call(run_cli: CliRunner, *inputs: str, env: dict[str, str] | None = None) -> Outcome:
    args = [x for pair in inputs for x in ("--input", pair)]
    return app(run_cli, "call", "lookup", *args, env=env or ENV)


def find(
    run_cli: CliRunner, slot: str, *texts: str, env: dict[str, str] | None = None, **more: Any
) -> Outcome:
    """``call lookup`` for ``texts`` and the slot ``for``."""
    given = [f"for={j(slot)}", f"text={j(list(texts) if len(texts) > 1 else texts[0])}"]
    given += [f"{k}={j(v)}" for k, v in more.items()]
    return call(run_cli, *given, env=env)


def manifest(run_cli: CliRunner, env: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    outcome = app(run_cli, "describe", "tools", env=env or ENV)
    assert outcome.code == 0, outcome.envelope
    return {t["name"]: t for t in outcome.data["tools"]}


def slots_of(run_cli: CliRunner, env: dict[str, str] | None = None) -> list[str]:
    found: list[str] = manifest(run_cli, env)["lookup"]["input_schema"]["properties"]["for"]["enum"]
    return found


def qualified(slot: str) -> str:
    """``lookup --for`` of a slot, from the tool name alone (the entries here have short names)."""
    tool, _, name = slot.partition(".")
    for prefix, kind in (("run_", "function"), ("do_", "action"), ("view_", "view")):
        if tool.startswith(prefix):
            return f"{kind}:{tool.removeprefix(prefix)}.{name}"
    raise AssertionError(slot)


# ---- listing -----------------------------------------------------------------------------


def test_the_manifest_lists_the_lookup_tool_last_with_the_slots_of_the_tools(
    run_cli: CliRunner, root: Path
) -> None:
    tools = manifest(run_cli)
    assert list(tools)[-1] == "lookup" and tools["lookup"]["kind"] == "lookup"
    enum = slots_of(run_cli)
    assert {
        "run_members_of_unit.unit",
        "do_assign_position.person",
        "view_unit_overview._entity",
        "view_members_report.unit",
        "view_employer_of._entity",
    } <= set(enum)
    # the references of the other tools name the tool and their slot
    unit = tools["run_members_of_unit"]["input_schema"]["properties"]["unit"]
    assert "with the lookup tool (for: 'run_members_of_unit.unit')" in unit["description"]
    only = app(run_cli, "describe", "tools", "--kind", "lookup", env=ENV)
    assert [t["name"] for t in only.data["tools"]] == ["lookup"]
    named = app(run_cli, "describe", "tools", "--tool", "lookup", env=ENV)
    assert named.data["tools"][0]["hash"] == tools["lookup"]["hash"]
    text = app(run_cli, "describe", "tools", "--kind", "lookup", env=ENV)
    assert text.code == 0


def test_every_slot_of_the_enum_is_looked_up_like_the_command_does(
    run_cli: CliRunner, root: Path
) -> None:
    """The tool is ``lookup --for`` with the kind of the entry written out: same candidates, same
    counts, the page in ``data``."""
    for slot in slots_of(run_cli):
        tool = find(run_cli, slot, NAME)
        assert tool.code == 0, (slot, tool.envelope)
        command = app(run_cli, "lookup", NAME, "--for", qualified(slot), env=ENV)
        assert command.code == 0, (slot, command.envelope)
        assert tool.data == {**command.data, "page": command.envelope["page"]}, slot
        assert tool.envelope["graph"] == command.envelope["graph"]
        assert "page" not in tool.envelope and tool.data["fit"]["for"] == qualified(slot)


def test_a_slot_of_a_view_fits_the_class_the_view_takes(run_cli: CliRunner, root: Path) -> None:
    def kept(slot: str) -> list[str]:
        outcome = find(run_cli, slot, NAME)
        assert outcome.code == 0, outcome.envelope
        return sorted(i["type"] for i in outcome.data["items"])

    assert kept("view_members_report.unit") == ["org:Department"]
    assert kept("view_legacy_unit_overview._entity") == ["org:Team"]
    assert kept("view_unit_overview._entity") == ["org:Department", "org:Team"]
    assert kept("view_employer_of._entity") == ["org:Department"]  # not the function of the name
    assert kept("run_members_of_unit.unit") == ["org:Department", "org:Team"]
    excluded = find(run_cli, "view_members_report.unit", NAME).data["texts"]
    assert excluded == [{"text": NAME, "total": 1, "exact": 1, "excluded": 1}]


# ---- the answer and its schema -------------------------------------------------------------


def test_the_input_and_the_answer_validate_against_the_schemas_of_the_tool(
    run_cli: CliRunner, root: Path
) -> None:
    tool = manifest(run_cli)["lookup"]
    valid = Draft202012Validator(tool["input_schema"])
    answers = Draft202012Validator(closed(tool["output_schema"]))
    for slot in slots_of(run_cli):
        for given in (
            {"text": NAME, "for": slot},
            {"text": [NAME, "Nothing here"], "for": slot, "limit": 1},
        ):
            assert valid.is_valid(given), given
            outcome = call(run_cli, *(f"{k}={j(v)}" for k, v in given.items()))
            assert outcome.code == 0, outcome.envelope
            found = list(answers.iter_errors(outcome.data))
            assert found == [], [e.message for e in found]


def test_several_names_a_limit_and_null_are_read_like_the_command(
    run_cli: CliRunner, root: Path
) -> None:
    slot = "run_members_of_unit.unit"
    both = find(run_cli, slot, NAME, "Nothing here", "alpha")
    assert both.code == 0, both.envelope
    assert [t["text"] for t in both.data["texts"]] == [NAME, "Nothing here"]  # alpha is the same
    by_command = app(
        run_cli,
        "lookup",
        NAME,
        "Nothing here",
        "alpha",
        "--for",
        "function:members_of_unit.unit",
        env=ENV,
    )
    assert both.data["texts"] == by_command.data["texts"]
    one = find(run_cli, slot, NAME, limit=1)
    assert one.data["page"]["limit"] == 1 and len(one.data["items"]) == 1
    assert one.data["page"]["next_offset"] is None or one.data["page"]["truncated"]
    default = find(run_cli, slot, NAME)
    assert default.data["page"]["limit"] == 50
    nulled = call(run_cli, f"text={j(NAME)}", f"for={j(slot)}", "limit=null")
    assert nulled.data == default.data
    floated = find(run_cli, slot, NAME, limit=2.0)
    assert floated.data["page"]["limit"] == 2


# ---- refusals ------------------------------------------------------------------------------


def code_of(outcome: Outcome) -> tuple[int, str]:
    return outcome.code, outcome.error["code"]


def test_the_input_is_checked(run_cli: CliRunner, root: Path) -> None:
    slot = "run_members_of_unit.unit"
    missing = {
        "no text": [f"for={j(slot)}"],
        "null text": ["text=null", f"for={j(slot)}"],
        "no slot": [f"text={j(NAME)}"],
        "null slot": [f"text={j(NAME)}", "for=null"],
    }
    for label, given in missing.items():
        refused = call(run_cli, *given)
        assert code_of(refused) == (2, "usage.missing_argument"), label
        assert refused.error["details"]["argument"] in ("text", "for"), label
    wrong = {
        "text": [
            "text=5",
            "text=true",
            'text={"a": 1}',
            "text=1.5",
            "text=[]",
            'text=[""]',
            'text=["a", 1]',
            'text=""',
            'text="  "',
            'text=["a", " "]',
            f"text={j(['x'] * (MAX_LOOKUP_TEXTS + 1))}",
        ],
        "limit": ["limit=0", "limit=501", "limit=2.5", 'limit="2"', "limit=true", "limit=[2]"],
    }
    for argument, values in wrong.items():
        for value in values:
            given = [f"for={j(slot)}"] + ([f"text={j(NAME)}"] if argument != "text" else [])
            refused = call(run_cli, value, *given)
            assert code_of(refused) == (2, "usage.invalid_argument"), value
            assert refused.error["details"]["argument"] == argument, value
    unknown = call(run_cli, f"text={j(NAME)}", f"for={j(slot)}", "offset=1")
    assert code_of(unknown) == (2, "usage.invalid_argument")
    assert unknown.error["details"]["expected"] == ["text", "for", "limit"]
    control = call(run_cli, f"text={j(NAME)}", f"for={j(slot)}", "_limit=1")
    assert code_of(control) == (2, "usage.invalid_argument")
    assert code_of(call(run_cli, "text")) == (2, "usage.invalid_argument")  # not NAME=<json>


def test_a_slot_that_is_none_is_refused_with_the_close_ones(run_cli: CliRunner, root: Path) -> None:
    close = find(run_cli, "run_members_of_unt.unit", NAME)
    assert code_of(close) == (2, "usage.invalid_argument")
    assert close.error["details"]["did_you_mean"][0] == "run_members_of_unit.unit"
    for bad in (
        "members_of_unit.unit",  # not a tool
        "function:members_of_unit.unit",  # the form of the command, not of the tool
        "view:members_report.unit",
        "view_members_report._entity",  # a set view has no entity
        "run_members_of_unit",
        "run_members_of_unit._limit",  # a control is no reference
        "do_assign_position.since",  # a date
        "lookup.text",
        "",
    ):
        refused = find(run_cli, bad, NAME)
        assert code_of(refused) == (2, "usage.invalid_argument"), bad
        assert refused.error["details"]["argument"] == "for", bad
    not_a_string = call(run_cli, f"text={j(NAME)}", "for=5")
    assert code_of(not_a_string) == (2, "usage.invalid_argument")


def test_a_call_of_the_lookup_tool_is_a_read(run_cli: CliRunner, root: Path) -> None:
    slot = "run_members_of_unit.unit"
    keyed = app(
        run_cli,
        "call",
        "lookup",
        *("--input", f"text={j(NAME)}", "--input", f"for={j(slot)}"),
        *("--idempotency-key", "k-1"),
        env=ENV,
    )
    assert code_of(keyed) == (2, "usage.invalid_argument")
    assert keyed.error["details"]["argument"] == "--idempotency-key"


def test_the_hash_of_the_manifest_guards_the_call(run_cli: CliRunner, root: Path) -> None:
    slot = "run_members_of_unit.unit"
    tool = manifest(run_cli)["lookup"]
    given = (f"text={j(NAME)}", f"for={j(slot)}")
    args = [x for pair in given for x in ("--input", pair)]
    good = app(run_cli, "call", "lookup", *args, "--expect-hash", tool["hash"], env=ENV)
    assert good.code == 0, good.envelope
    stale = app(run_cli, "call", "lookup", *args, "--expect-hash", "sha256:" + "0" * 64, env=ENV)
    assert code_of(stale) == (4, "tool.hash_mismatch")
    assert stale.error["details"]["actual"] == tool["hash"]


# ---- what a caller who sees or may do less is given -----------------------------------------


NO_FIND = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_find, agent_type: [explore], action: ["read.find"]}
"""
NO_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
"""


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_caller_without_read_find_has_no_lookup_tool_and_is_denied_a_call(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    full = manifest(run_cli, EXPLORE)
    assert "lookup" in full
    install(run_cli, tmp_path / "policies", NO_FIND)
    seen = manifest(run_cli, EXPLORE)
    assert "lookup" not in seen
    # the references stay, and say nothing of a tool the caller has not got
    assert "run_members_of_unit" in seen
    assert "lookup" not in json.dumps(seen["run_members_of_unit"]["input_schema"])
    missing = app(run_cli, "describe", "tools", "--tool", "lookup", env=EXPLORE)
    assert code_of(missing) == (1, "tool.not_found")
    # a name close to it is not suggested to a caller that has none
    close = app(run_cli, "call", "lookp", env=EXPLORE)
    assert code_of(close) == (1, "tool.not_found")
    assert "lookup" not in close.error["details"]["did_you_mean"]
    assert app(run_cli, "call", "lookp", env=ENV).error["details"]["did_you_mean"] == ["lookup"]
    slot = "run_members_of_unit.unit"
    denied = find(run_cli, slot, NAME, env=EXPLORE)
    assert code_of(denied) == (3, "access.denied")
    by_command = app(run_cli, "lookup", NAME, "--for", "members_of_unit.unit", env=EXPLORE)
    assert by_command.error["code"] == "access.denied"
    assert denied.error["details"] == by_command.error["details"]
    hashed = app(
        run_cli,
        "call",
        "lookup",
        *("--input", f"text={j(NAME)}", "--input", f"for={j(slot)}"),
        *("--expect-hash", "sha256:" + "0" * 64),
        env=EXPLORE,
    )
    assert hashed.error["code"] == "access.denied"  # the denial comes before the hash
    # another caller of the same graph still has it
    assert find(run_cli, slot, NAME).code == 0


def test_the_hash_of_a_tool_follows_whether_the_lookup_tool_is_listed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """The reference inputs of a tool name the lookup tool only for a caller that has it, so the
    hash of the tool differs, and a call that checks it builds the tool for its own caller."""
    unit = f"unit={j('0192f3c0')}"
    with_find = manifest(run_cli, EXPLORE)["run_members_of_unit"]["hash"]
    held = app(
        run_cli,
        "call",
        "run_members_of_unit",
        "--input",
        unit,
        "--expect-hash",
        with_find,
        env=EXPLORE,
    )
    assert held.error["code"] == "entity.not_found"  # the hash held, the id does not exist
    install(run_cli, tmp_path / "policies", NO_FIND)
    without = manifest(run_cli, EXPLORE)["run_members_of_unit"]["hash"]
    assert without != with_find
    stale = app(
        run_cli,
        "call",
        "run_members_of_unit",
        "--input",
        unit,
        "--expect-hash",
        with_find,
        env=EXPLORE,
    )
    assert code_of(stale) == (4, "tool.hash_mismatch")
    assert stale.error["details"]["actual"] == without
    fresh = app(
        run_cli,
        "call",
        "run_members_of_unit",
        "--input",
        unit,
        "--expect-hash",
        without,
        env=EXPLORE,
    )
    assert fresh.error["code"] == "entity.not_found"


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_the_slots_are_those_of_the_tools_the_caller_may_use(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    before = slots_of(run_cli, EXPLORE)
    install(run_cli, tmp_path / "policies", NO_FUNCTIONS)
    after = slots_of(run_cli, EXPLORE)
    assert (
        not [s for s in after if s.startswith("run_")] and "view_members_report.unit" not in after
    )
    assert set(after) < set(before) and "view_position_overview._entity" in after
    gone = find(run_cli, "run_members_of_unit.unit", NAME, env=EXPLORE)
    assert code_of(gone) == (2, "usage.invalid_argument")
    assert gone.error["details"]["did_you_mean"] == [] or all(
        not s.startswith("run_") for s in gone.error["details"]["did_you_mean"]
    )
    assert find(run_cli, "run_members_of_unit.unit", NAME).code == 0


READS_NO_TEAMS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [{reads}]
no_project: {{"*": write}}
"""


@pytest.mark.doc_claim("security_model.tool_manifest_per_caller")
def test_a_caller_that_does_not_see_a_class_has_no_slot_that_names_it(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    from support.lookup_graph import FACTORY

    reads = sorted(
        name
        for name, found in FACTORY.model.classes.items()
        if not found.abstract and name != "org:Team"
    )
    assert "view_legacy_unit_overview._entity" in slots_of(run_cli)
    install(run_cli, tmp_path / "policies", READS_NO_TEAMS.format(reads=", ".join(reads)))
    tools = manifest(run_cli)
    assert "lookup" in tools
    assert "view_legacy_unit_overview._entity" not in slots_of(run_cli)
    text = json.dumps(tools["lookup"])
    for word in ("Team", "legacy_unit_overview"):
        assert word not in text, word
    hidden = find(run_cli, "view_legacy_unit_overview._entity", NAME)
    assert code_of(hidden) == (2, "usage.invalid_argument")
    assert "legacy_unit_overview" not in json.dumps(hidden.error["details"]["did_you_mean"])
    visible = find(run_cli, "run_members_of_unit.unit", NAME)
    assert visible.code == 0 and {i["type"] for i in visible.data["items"]} == {"org:Department"}


def test_a_graph_whose_tools_take_no_reference_has_no_lookup_tool(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    created = admin(
        run_cli,
        *("graph", "create", "example_graph"),
        *("--ontology", str(EXAMPLES / "company" / "ontology" / "1.1.0")),
        *("--module", "company"),
    )
    assert created.code == 0, created.envelope
    functions = tmp_path / "functions"
    functions.mkdir()
    shutil.copy(FUNCTION_FIXTURES / "training_completions.yaml", functions)  # a list and a date
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions))
    assert published.code == 0, published.envelope
    tools = manifest(run_cli)
    assert list(tools) == ["run_training_completions"]
    missing = call(run_cli, f"text={j(NAME)}", f"for={j('run_training_completions.unit')}")
    assert code_of(missing) == (1, "tool.not_found")
    assert missing.error["hint"] == "graph describe tools"
    assert missing.error["details"]["did_you_mean"] == []
    assert code_of(app(run_cli, "call", "lookup", env=ENV)) == (1, "tool.not_found")


def test_a_graph_without_labels_answers_like_the_command(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """A reference input that no name can fill (the ontology has no label role) is still a slot:
    the answer is the one of ``lookup``."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    from support.fixtures import FIXTURES

    for kind in ("functions", "actions", "views"):
        folder = FIXTURES / kind / "inventory" / "valid"
        assert admin(run_cli, "graph", kind, "publish", "example_graph", str(folder)).code == 0
    tool = find(run_cli, "run_items_in_location.location", "x")
    command = app(run_cli, "lookup", "x", "--for", "function:items_in_location.location", env=ENV)
    assert code_of(tool) == code_of(command) == (1, "term.no_labels")
    assert tool.error["details"] == command.error["details"]


MARKER = "lookup-marker-7c1e"


def test_the_access_log_says_a_call_ran_lookup_and_keeps_the_names_out(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    log = Path(isolated_env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    found = find(run_cli, "run_members_of_unit.unit", MARKER)
    assert found.code == 0 and found.data["texts"][0]["total"] == 0
    bad = find(run_cli, "run_nothing.unit", MARKER)
    assert bad.error["code"] == "usage.invalid_argument"
    by_command = app(run_cli, "lookup", MARKER, env=ENV)
    assert by_command.code == 0
    ran, refused, plain = [json.loads(line) for line in log.read_text().splitlines()[-3:]]
    assert (ran["command"], ran["via"], ran["result"]) == ("lookup", "call", {"status": "ok"})
    assert (refused["command"], refused["via"]) == ("lookup", "call")
    assert refused["result"]["code"] == "usage.invalid_argument"
    assert plain["command"] == "lookup" and "via" not in plain
    assert MARKER not in log.read_text()
