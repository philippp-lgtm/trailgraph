"""C: the acceptance of the tool manifest (tool manifest plan, step 6): a client that knows a graph
from ``POST /v1/commands/describe/tools`` alone, the way an agent harness does, uses every tool of
the ``company`` example through ``POST /v1/commands/call``.

The client (``support.tools_client``) checks each input it sends against the ``input_schema`` of the
tool and each answer against its ``output_schema``, published and closed, and each refusal against
the ``errors`` of the tool and the ``errors`` map of the manifest. The graph is the example built
with its seeds (``support.company_server``), served to three callers: ``maintainer`` writes,
``reader`` only reads and ``narrow`` reads a few types.
"""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from jsonschema import Draft202012Validator
from support.admin import admin
from support.company_server import COMPANY, GRAPH, Installation, build
from support.isolation import isolated, runner
from support.server import call
from support.tools_client import Ref, ToolsClient, sections

pytestmark = pytest.mark.slow

CONTRACTOR = {"kind": "contractor"}


def ref(text: str, slot: str) -> Ref:
    return Ref(text, slot)


# Every function of the example with an input that finds rows: the names are looked up with the
# ``lookup`` tool for the slot of the tool, so no id comes from outside the manifest.
FUNCTIONS: dict[str, dict[str, Any]] = {
    "run_employer_of": {"person": ref("Mara Lindqvist", "run_employer_of.person")},
    "run_members_of_unit": {"unit": ref("Engineering", "run_members_of_unit.unit")},
    "run_missing_required_trainings": {
        "unit": ref("Engineering", "run_missing_required_trainings.unit")
    },
    "run_policies_for_unit": {"unit": ref("Infrastructure", "run_policies_for_unit.unit")},
    "run_reporting_chain": {"position": ref("Software Engineer", "run_reporting_chain.position")},
    "run_required_trainings": {"person": [ref("Mara Lindqvist", "run_required_trainings.person")]},
    "run_shareholders_of": {"entity": [ref("Marrowdale Holding AG", "run_shareholders_of.entity")]},
    "run_training_completions": {"titles": ["Security awareness"], "since": "2020-01-01"},
    "run_vacant_positions": {"unit": ref("Management", "run_vacant_positions.unit")},
    "run_externals_by_kind": CONTRACTOR,
}
ENTITY_VIEWS: dict[str, dict[str, Any]] = {
    "view_position_overview": {
        "_entity": ref("Software Engineer", "view_position_overview._entity")
    },
    "view_unit_overview": {
        "_entity": ref("Engineering", "view_unit_overview._entity"),
        "status": "active",
    },
    "view_legacy_unit_overview": {
        "_entity": ref("Applications", "view_legacy_unit_overview._entity")
    },
}
SET_VIEWS: dict[str, dict[str, Any]] = {
    "view_org_catalog": {},
    "view_members_report": {"unit": ref("Engineering", "view_members_report.unit")},
    "view_tasks_by_state": {"status": "open", "before": "2027-01-01"},
}
PAGE_BYTES = 4096  # a set view pages by size, at least 1024 bytes


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Installation:
    """One installation for the tests that only read (a dry run reads): building it costs seconds
    of CPU, and nothing these tests do changes it."""
    base = tmp_path_factory.mktemp("company")
    env = isolated(base)
    return build(runner(env, base), env, base)


@pytest.fixture
def harness(shared: Installation) -> ToolsClient:
    """The client of the caller who writes, over the shared installation, with the manifest
    fetched once. It must not write: the tests that do take ``writer``."""
    return ToolsClient.fetch(shared.server, shared.tokens["maintainer"], GRAPH)


@pytest.fixture
def installation(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Installation:
    """An installation of the test's own, for a test that writes or publishes."""
    return build(run_cli, isolated_env, tmp_path)


@pytest.fixture
def writer(installation: Installation) -> ToolsClient:
    return ToolsClient.fetch(installation.server, installation.tokens["maintainer"], GRAPH)


def assign(client: ToolsClient, person: str, **more: Any) -> dict[str, Any]:
    """The input that gives the position of the data protection officer to ``person``."""
    return {
        "person": ref(person, "do_assign_position.person"),
        "position": ref("Data Protection Officer", "do_assign_position.position"),
        "since": "2026-03-01",
        **more,
    }


def test_the_manifest_lists_every_tool_of_the_example(harness: ToolsClient) -> None:
    listed = set(harness.tools)
    assert listed == {
        *FUNCTIONS,
        *ENTITY_VIEWS,
        *SET_VIEWS,
        "do_assign_position",
        "do_hand_over_position",
        "do_offboard_employee",
        "lookup",
    }
    assert harness.manifest["tools_format"] == 1
    for tool in harness.tools.values():
        Draft202012Validator.check_schema(tool["input_schema"])
        Draft202012Validator.check_schema(tool["output_schema"])
        # the codes of every tool are in the map, each with what to do about it
        assert set(tool["errors"]) <= set(harness.manifest["errors"]), tool["name"]


def test_every_function_answers_rows_that_fit_its_schema(harness: ToolsClient) -> None:
    for tool, given in FUNCTIONS.items():
        answer = harness.call(tool, given)  # the client checks the input and the answer
        assert answer.data["items"], tool
        assert answer.data["page"]["total"] == len(answer.data["items"]), tool
    # a column whose type the manifest gives is typed in the rows: a date and an enum
    rows = harness.call("run_externals_by_kind", CONTRACTOR).data["items"]
    assert rows[0]["kind"] == "contractor" and rows[0]["engaged_until"] == "2027-03-31"


def test_a_function_pages_by_offset(harness: ToolsClient) -> None:
    given = FUNCTIONS["run_members_of_unit"]
    whole = harness.call("run_members_of_unit", given).data["items"]
    assert len(whole) > 3
    assert harness.rows("run_members_of_unit", given, limit=3) == whole
    first = harness.call("run_members_of_unit", {**given, "_limit": 3}).data["page"]
    assert first["truncated"] is True and first["next_offset"] == 3
    # null is "not given" for every argument that is not required, though the schema lets only an
    # argument without a default take it (a default is "not required" and nullable in one):
    nulls = harness.call("run_members_of_unit", {**given, "_limit": None}, valid=False)
    assert nulls.data["items"] == whole
    reasoned = assign(harness, "Dana Whitfield", _reason=None, _dry_run=True)
    assert harness.call("do_assign_position", reasoned).data["dry_run"] is True


def test_the_views_answer_pages_and_sections_that_fit_their_schemas(
    harness: ToolsClient,
) -> None:
    for tool, given in ENTITY_VIEWS.items():
        answer = harness.call(tool, given)
        assert answer.data["sections"], tool
        assert "more" not in answer.data and "next" not in answer.data, tool
    rows = {
        s["id"]: s
        for s in sections(
            harness.call("view_position_overview", ENTITY_VIEWS["view_position_overview"]).data
        )
    }
    assert rows["chain"]["kind"] == "rows" and rows["chain"]["items"]
    for tool, given in SET_VIEWS.items():
        whole = harness.call(tool, given).data
        assert harness.call(tool, given | {"_page_bytes": 262144}).data == whole


def test_a_set_view_pages_by_cursor(harness: ToolsClient) -> None:
    whole = harness.call("view_org_catalog", {"_page_bytes": 262144}).data
    assert "more" not in whole
    pages = harness.pages("view_org_catalog", {}, page_bytes=PAGE_BYTES)
    assert len(pages) > 2 and all(p["more"] for p in pages[:-1]) and "more" not in pages[-1]
    assert "next" not in pages[-1]

    def keys(answer: dict[str, Any]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for section in sections(answer):
            found.setdefault(section["id"], []).extend(i["ref"] for i in section.get("items", []))
        return found

    paged: dict[str, list[str]] = {}
    for page in pages:
        for section, refs in keys(page).items():
            paged.setdefault(section, []).extend(refs)
    assert paged == keys(whole)
    # a set view with a reference and one with defaults page the same way
    report = harness.pages("view_members_report", SET_VIEWS["view_members_report"], page_bytes=1024)
    assert len(report) == 2 and report[0]["more"] is True


def test_a_write_between_two_pages_makes_the_cursor_stale(writer: ToolsClient) -> None:
    first = writer.call("view_org_catalog", {"_page_bytes": PAGE_BYTES}).data
    cursor = first["next"]
    writer.call("do_assign_position", assign(writer, "Dana Whitfield"))
    stale = writer.call("view_org_catalog", {"_page_bytes": PAGE_BYTES, "_cursor": cursor})
    assert (stale.reply.status, stale.code) == (422, "view.cursor_stale")
    assert stale.on_error == "restart"
    again = writer.pages("view_org_catalog", {}, page_bytes=PAGE_BYTES)  # from the first page
    assert again[0]["next"] != cursor and "more" not in again[-1]


def test_an_action_runs_as_a_dry_run_and_for_real(writer: ToolsClient) -> None:
    before = writer.manifest["version"]
    vacant = FUNCTIONS["run_vacant_positions"]
    positions = writer.call("run_vacant_positions", vacant).data["items"]
    dry = writer.call("do_assign_position", assign(writer, "Dana Whitfield", _dry_run=True))
    assert dry.data["dry_run"] is True and dry.data["seq"] is None
    assert writer.call("run_vacant_positions", vacant).data["items"] == positions
    real = writer.call(
        "do_assign_position", assign(writer, "Dana Whitfield", _reason="joins the company")
    )
    assert real.data["dry_run"] is False and real.data["seq"] is not None
    assert real.data["entities"][0]["ops"] == ["link"]
    held = writer.call(
        "run_members_of_unit", {"unit": ref("Management", "run_members_of_unit.unit")}
    )
    assert "Dana Whitfield" in [r["person"] for r in held.data["items"]]
    # the other two actions: a hand over to a new holder, and an exit, which needs a reason
    handed = writer.call(
        "do_hand_over_position",
        {
            "position": ref("Recruiter", "do_hand_over_position.position"),
            "person": ref("Rhea Castellanos", "do_hand_over_position.person"),
            "since": "2026-04-01",
        },
    )
    assert handed.data["seq"] > real.data["seq"]
    exit_input = {"employee": ref("Noah Becker", "do_offboard_employee.employee")}
    writer.call("do_offboard_employee", exit_input | {"_dry_run": True, "_reason": "leaves"})
    left = writer.call("do_offboard_employee", exit_input | {"_reason": "leaves the company"})
    assert left.data["seq"] > handed.data["seq"]
    # data writes do not change the manifest
    assert ToolsClient.fetch(writer.server, writer.token, GRAPH).manifest["version"] == before


def test_a_precondition_failure_tells_the_client_to_check_the_state(
    writer: ToolsClient,
) -> None:
    writer.call("do_assign_position", assign(writer, "Dana Whitfield"))
    for more in ({}, {"_dry_run": True}):  # a dry run raises the same error as the real run
        taken = writer.call("do_assign_position", assign(writer, "Rhea Castellanos", **more))
        assert (taken.reply.status, taken.code) == (422, "action.precondition_failed"), more
        assert taken.on_error == "check_state"
        (failed,) = taken.details["failed"]
        assert failed["precondition"] == "position_vacant" and failed["entity"]
    # the same input again is refused the same way: the state decides, not the input
    again = writer.call("do_assign_position", assign(writer, "Dana Whitfield", _dry_run=True))
    assert again.code == "action.precondition_failed"


def test_an_input_the_schema_refuses_is_refused_by_the_server_as_well(
    harness: ToolsClient,
) -> None:
    unit = ref("Engineering", "run_members_of_unit.unit")
    cases = (
        ("run_members_of_unit", {"unit": unit, "bogus": 1}, "function.parameter_unknown"),
        ("run_externals_by_kind", {"kind": "vendor"}, "function.parameter_invalid"),
        ("run_members_of_unit", {}, "function.parameter_missing"),
        ("run_members_of_unit", {"unit": unit, "_limit": 0}, "usage.invalid_argument"),
        (
            "run_training_completions",
            {"titles": ["x"], "since": "soon"},
            "function.parameter_invalid",
        ),
        ("do_offboard_employee", {"employee": unit}, "action.reason_required"),
        ("view_org_catalog", {"_entity": "x"}, "usage.invalid_argument"),
    )
    for tool, given, code in cases:
        refused = harness.call(tool, given, valid=False)
        assert refused.problems and (refused.reply.status, refused.code) == (400, code), (
            tool,
            given,
        )
        assert refused.on_error == "fix_input", (tool, given)


def test_a_reference_of_the_wrong_class_is_refused(harness: ToolsClient) -> None:
    person = harness.resolve(ref("Mara Lindqvist", "run_employer_of.person"))  # no unit
    wrong = harness.call("run_members_of_unit", {"unit": person})
    assert (wrong.reply.status, wrong.code) == (422, "function.parameter_type")
    assert wrong.on_error == "fix_input"
    shown = harness.call("view_position_overview", {"_entity": person})
    assert shown.code == "view.class_mismatch" and shown.on_error == "fix_input"


def test_a_tool_that_is_hidden_from_the_caller_is_not_found(
    harness: ToolsClient, shared: Installation
) -> None:
    narrow = ToolsClient.fetch(shared.server, shared.tokens["narrow"], GRAPH)
    assert "run_shareholders_of" in harness.tools and "run_shareholders_of" not in narrow.tools
    assert {"lookup", "run_members_of_unit"} <= set(narrow.tools)
    # the harness holds the tools of the caller who sees more; the narrow caller sees none of them
    holding = harness.as_caller(shared.tokens["narrow"])
    holder = harness.resolve(ref("Marrowdale Holding AG", "run_shareholders_of.entity"))
    hidden = holding.call("run_shareholders_of", {"entity": [holder]})
    assert (hidden.reply.status, hidden.code) == (404, "tool.not_found")
    assert hidden.on_error == "refresh"
    assert not any("shareholders" in name for name in hidden.details.get("did_you_mean", []))
    # the entry names a hidden type: the narrow caller cannot see its entity either
    hidden_view = holding.call(
        "view_unit_overview",
        {"_entity": harness.resolve(ref("Engineering", "view_unit_overview._entity"))},
    )
    assert hidden_view.code == "tool.not_found"


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_a_caller_limited_to_its_catalog_finds_names_only_through_the_narrow_lookup(
    harness: ToolsClient, shared: Installation
) -> None:
    """The token of a catalog grant names one function and one entity view: its manifest holds
    those two tools and the lookup tool for their reference inputs, the harness resolves names
    with it and calls the tools with the alias it gets, and every other way to a reference is
    shut."""
    client = ToolsClient.fetch(shared.server, shared.tokens["catalog"], GRAPH)
    assert set(client.tools) == {"run_members_of_unit", "view_unit_overview", "lookup"}
    assert client.tools["lookup"]["input_schema"]["properties"]["for"]["enum"] == [
        "run_members_of_unit.unit",
        "view_unit_overview._entity",
    ]
    unit = FUNCTIONS["run_members_of_unit"]["unit"]
    members = client.call("run_members_of_unit", {"unit": unit})
    assert members.data["items"]
    assert client.call("view_unit_overview", ENTITY_VIEWS["view_unit_overview"]).ok
    # the candidates show what tells homonyms apart and nothing that counts the rest
    found = client.call("lookup", {"text": "Engineering", "for": "run_members_of_unit.unit"}).data
    (item,) = found["items"]
    assert set(item) == {"text", "id", "alias", "type", "record_status", "label", "match", "facts"}
    assert found["texts"] == [{"text": "Engineering", "total": 1, "exact": 1, "excluded": 0}]
    # a name of another class (a person) is no candidate for a unit and is not even counted
    person = "Mara Lindqvist"
    other = client.call("lookup", {"text": person, "for": "run_members_of_unit.unit"}).data
    nothing = client.call(
        "lookup", {"text": "No Such Name", "for": "run_members_of_unit.unit"}
    ).data
    assert other["items"] == nothing["items"] == []
    assert other["texts"] == [{"text": person, "total": 0, "exact": 0, "excluded": 0}]
    assert nothing["texts"] == [{"text": "No Such Name", "total": 0, "exact": 0, "excluded": 0}]
    assert other["page"]["next_offset"] is None
    # an id of another class, and one it cannot see, are not found: neither names a class
    maintainer_ref = harness.resolve(ref(person, "run_employer_of.person"))
    shut = harness.resolve(ref("Marrowdale Holding AG", "run_shareholders_of.entity"))
    nowhere = "DEPT-9999"
    answers = []
    for text in (maintainer_ref, shut, nowhere):
        wrong = client.call("run_members_of_unit", {"unit": text})
        assert (wrong.reply.status, wrong.code) == (404, "entity.not_found"), text
        answers.append((wrong.reply.error["message"].replace(text, "X"), wrong.on_error))
    assert answers[0] == answers[1] == answers[2]
    wrong_view = client.call(
        "view_unit_overview", {**ENTITY_VIEWS["view_unit_overview"], "_entity": maintainer_ref}
    )
    assert wrong_view.code == "entity.not_found"
    # a trailing hex is no reference: the schema forbids it and the server refuses it
    hexed = client.call("run_members_of_unit", {"unit": "0123abcd"}, valid=False)
    assert hexed.code == "function.parameter_invalid"
    assert "candidates" not in hexed.details and "matches" not in hexed.reply.error["message"]


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_every_other_way_to_look_names_up_is_refused_for_a_catalog_caller(
    shared: Installation,
) -> None:
    token = shared.tokens["catalog"]

    def command(path: str, **args: Any) -> Any:
        return call(
            shared.server,
            "POST",
            f"/v1/commands/{path}",
            token=token,
            body={"args": {"graph": GRAPH, **args}},
        )

    narrow = command("lookup", text=["Engineering"], **{"--for": "members_of_unit.unit"})
    assert narrow.status == 200, narrow.json
    # nothing found: the only hint names no other way to look names up
    empty = command("lookup", text=["No Such Name"], **{"--for": "members_of_unit.unit"})
    assert empty.status == 200 and empty.json["data"]["items"] == []
    assert [h["command"] for h in empty.json["hints"]] == [None]
    assert narrow.json["hints"] == [] and narrow.json["page"]["next_offset"] is None
    refused = {
        "free": {"text": ["Engineering"]},
        "relation": {"text": ["Engineering"], "--for": "part_of"},
        "source": {"text": ["Engineering"], "--for-source": "part_of"},
        "prefix": {"text": ["Eng"], "--for": "members_of_unit.unit", "--match": "prefix"},
        "offset": {"text": ["Eng"], "--for": "members_of_unit.unit", "--offset": 1},
        "inactive": {"text": ["Eng"], "--for": "members_of_unit.unit", "--include-inactive": True},
        "typed": {"text": ["Eng"], "--for": "members_of_unit.unit", "--type": ["org:Team"]},
    }
    bodies = []
    for label, args in refused.items():
        reply = command("lookup", **args)
        assert reply.status == 403, (label, reply.json)
        assert (
            reply.error["code"] == "access.denied"
            and reply.error["details"]["action"] == "read.find"
        )
        assert reply.error["details"]["resource"].count("/") == 0, label  # the graph, no entry
        bodies.append(str(reply.error["details"]))
    assert len(set(bodies)) == 1  # one refusal for every shape
    for path, args in (
        ("search", {"text": "Engineering"}),
        ("find", {"--type": ["org:Department"]}),
        ("get", {"ref": "DEPT-0001"}),
    ):
        reply = command(path, **args)
        assert reply.status == 403 and reply.error["code"] == "access.denied", (path, reply.json)
    # a slot of an entry outside the catalog is no slot
    outside = command("lookup", text=["Engineering"], **{"--for": "function:employer_of.person"})
    absent = command("lookup", text=["Engineering"], **{"--for": "function:employer_ofz.person"})
    assert outside.status == absent.status == 404
    assert outside.error["code"] == absent.error["code"] == "function.not_found"
    assert outside.error["message"].replace("employer_of", "X") == absent.error["message"].replace(
        "employer_ofz", "X"
    )


def test_a_tool_the_caller_sees_but_may_not_use_is_denied(
    harness: ToolsClient, shared: Installation
) -> None:
    reader = ToolsClient.fetch(shared.server, shared.tokens["reader"], GRAPH)
    assert "do_assign_position" in harness.tools and "do_assign_position" not in reader.tools
    reading = harness.as_caller(shared.tokens["reader"])
    given = {
        "person": harness.resolve(ref("Dana Whitfield", "do_assign_position.person")),
        "position": harness.resolve(ref("Data Protection Officer", "do_assign_position.position")),
        "since": "2026-03-01",
    }
    for more in ({}, {"_dry_run": True}):
        denied = reading.call("do_assign_position", given | more)
        assert (denied.reply.status, denied.code) == (403, "access.denied"), more
        assert denied.on_error == "stop"
    # nothing was written: the maintainer still finds the position vacant
    assert harness.call("do_assign_position", given | {"_dry_run": True}).ok


def test_a_changed_tool_is_refused_with_the_hash_it_has_now(
    writer: ToolsClient, run_cli: CliRunner, tmp_path: Path
) -> None:
    given = FUNCTIONS["run_members_of_unit"]
    old = writer.tools["run_members_of_unit"]["hash"]
    wrong = writer.call("run_members_of_unit", given, expect_hash="sha256:" + "0" * 64)
    assert (wrong.reply.status, wrong.code) == (409, "tool.hash_mismatch")
    assert wrong.on_error == "refresh" and wrong.details["actual"] == old
    assert writer.call("run_members_of_unit", given, expect_hash=False).ok
    # the maintainer rewords the function: the tool the writer built is not the current one
    folder = tmp_path / "reworded"
    shutil.copytree(COMPANY / "functions", folder)
    path = folder / "members_of_unit.yaml"
    path.write_text(path.read_text().replace("People holding", "Everyone holding", 1))
    published = admin(run_cli, "graph", "functions", "publish", GRAPH, str(folder))
    assert published.code == 0, published.envelope
    stale = writer.call("run_members_of_unit", given)
    assert (stale.reply.status, stale.code) == (409, "tool.hash_mismatch")
    assert stale.on_error == "refresh"
    # refresh: fetch the manifest again and call with the tool as it is now
    fresh = ToolsClient.fetch(writer.server, writer.token, GRAPH)
    assert fresh.tools["run_members_of_unit"]["hash"] == stale.details["actual"] != old
    assert fresh.manifest["version"] != writer.manifest["version"]
    assert fresh.call("run_members_of_unit", given).ok


def test_the_client_needs_no_describe_beyond_the_manifest(
    writer: ToolsClient, isolated_env: dict[str, str]
) -> None:
    """What the server saw, from its access log: for the token of the client one ``describe
    tools`` and then the commands the calls ran, no other ``describe``."""
    for tool, given in {**FUNCTIONS, **ENTITY_VIEWS}.items():
        writer.call(tool, given)
    log = Path(isolated_env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    ours = [line for line in lines if line["actor"]["principal"]["id"] == "token:app_maintainer"]
    commands = [line["command"] for line in ours]
    assert [c for c in commands if c.startswith("describe")] == ["describe tools"]
    assert set(commands) <= {"describe tools", "lookup", "run", "view"}
    assert len(commands) == len(writer.requests)  # one line per request the client made
    # every code a tool lists has what to do about it; the map lists no other code
    listed = {code for tool in writer.tools.values() for code in tool["errors"]}
    assert listed == set(writer.manifest["errors"])


def test_the_client_refuses_an_answer_that_does_not_fit_its_schema(harness: ToolsClient) -> None:
    """The checks of the client mean something: a schema that disagrees with the answer, in a
    type or by leaving out a key (which only the closed form notices), fails the call."""
    given = FUNCTIONS["run_members_of_unit"]

    def with_schema(change: Any) -> ToolsClient:
        manifest = copy.deepcopy(dict(harness.manifest))
        tool = next(t for t in manifest["tools"] if t["name"] == "run_members_of_unit")
        change(tool["output_schema"]["properties"]["items"]["items"])
        return ToolsClient(harness.server, harness.token, GRAPH, manifest)

    def retype(row: dict[str, Any]) -> None:
        row["properties"]["person"] = {"type": "integer"}

    def forget(row: dict[str, Any]) -> None:
        del row["properties"]["position"]
        row["required"].remove("position")

    with pytest.raises(AssertionError, match="published schema"):
        with_schema(retype).call("run_members_of_unit", given)
    with pytest.raises(AssertionError, match="closed schema"):
        with_schema(forget).call("run_members_of_unit", given)
    # and one that lists less than the server may return
    narrower = copy.deepcopy(dict(harness.manifest))
    for tool in narrower["tools"]:
        tool["errors"] = [c for c in tool["errors"] if c != "function.parameter_unknown"]
    refusing = ToolsClient(harness.server, harness.token, GRAPH, narrower)
    with pytest.raises(AssertionError, match=r"does not list function\.parameter_unknown"):
        refusing.call("run_members_of_unit", {**given, "bogus": 1}, valid=False)


def test_the_format_checker_refuses_what_is_no_date_or_timestamp() -> None:
    """The formats of the schemas are checked only where the checker has the library for them
    (``date-time`` needs ``rfc3339-validator``, which ``jsonschema`` does not require): a date-time
    without an offset, and a date that is no day, are errors, so the checks above mean something."""
    checker = Draft202012Validator.FORMAT_CHECKER
    stamp = Draft202012Validator({"type": "string", "format": "date-time"}, format_checker=checker)
    assert stamp.is_valid("2026-01-31T10:00:00Z") and stamp.is_valid("2026-01-31T10:00:00+01:00")
    for bad in ("2026-01-31T10:00:00", "2026-01-31", "yesterday", "2026-13-01T10:00:00Z"):
        assert not stamp.is_valid(bad), bad
    day = Draft202012Validator({"type": "string", "format": "date"}, format_checker=checker)
    assert day.is_valid("2026-02-28") and not day.is_valid("2026-02-30")
