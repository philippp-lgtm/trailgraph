"""C: the acceptance of catalog callers (catalog callers plan, step 5): a server token whose role
is limited to a few functions, one action and two views of the ``company`` example works through
them and through nothing else, over the server API as an agent harness meets it.

* Its manifest (``describe tools``) holds exactly its tools, plus ``lookup`` when a tool takes a
  reference; each tool runs through ``call`` and through the command form (``run``, ``do``,
  ``view``) with the same answer.
* ``COMMANDS`` is a table of every command the server's inventory lists, with the outcome for
  the catalog caller; the test fails for a command the table does not classify, so a future
  primitive cannot slip through unnoticed. It covers commands only: the two routes that are no
  command (the download ``/v1/graphs/{graph}/files/{ref}``, which delegates to ``file path``, and
  ``/v1/graphs/{graph}/skill``) are not in it, and a new route is not caught by it.
* No way to look names up enumerates entities (id suffixes, ambiguous suffixes, ids of other
  classes, prefix matches, offsets, inactive records, relation slots): each is refused with the
  one answer an unknown name gets.
* The action writes what the token cannot write by hand (``link``, ``apply`` and ``create`` of
  the same ops are refused); a deny rule, a type limit and a project scope of ``read`` refuse it.
* A set view lists entities the token cannot ``find``; the read limits of the role and the
  markings of a graph still filter it.
* The audit names the entry grant of every step and the action of every write; ``audit verify``
  passes; ``graph-admin policy explain`` explains the profile.

The policies without the new keys compile to the same documents and hashes as before: pinned in
step 1 (``tests/goldens/access_control/policy_hashes_before_catalog.json``), not repeated here.
The server runs in-process, as for the other server tests; building the example takes seconds.
"""

import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.company_server import (
    CATALOG_READS,
    GRAPH,
    ORGANIZATION,
    Installation,
    build,
)
from support.files import write_pdf
from support.isolation import isolated, runner
from support.markings import STAFF
from support.policies import write_policy
from support.server import Reply, call, new_token
from support.tools_client import Ref, ToolsClient

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  maintainer: {description: "Reads and writes the graph"}
  reader: {description: "Reads the graph"}
  catalog_agent: {description: "Works through a few entries"}
  catalog_denied: {description: "The same entries, with deny rules on writing and finding"}
  catalog_narrow: {description: "Works through two entries and reads part of the graph"}
grants:
  - {entrypoint: app, graphs: "*", level: write}
  - {entrypoint: server, principal: {role: maintainer}, graphs: [company], level: write}
  - {entrypoint: server, principal: {role: reader}, graphs: [company], level: read}
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [company]
    level: catalog
    catalog:
      functions: [members_of_unit, vacant_positions]
      actions: [assign_position]
      views: [unit_overview, org_catalog]
    reads: [{READS}]
  - entrypoint: server
    principal: {role: catalog_denied}
    graphs: [company]
    level: catalog
    catalog:
      functions: [members_of_unit, vacant_positions]
      actions: [assign_position]
      views: [unit_overview, org_catalog]
    reads: [{READS}]
  - entrypoint: server
    principal: {role: catalog_narrow}
    graphs: [company]
    level: catalog
    catalog:
      functions: [members_of_unit]
      views: [org_catalog]
    reads: [{NARROW_READS}]
scopes:
  company_project: {company: write}
  read_project: {company: read}
no_project: {"*": none}
deny:
  - {id: denied_no_link, role: [catalog_denied], action: [write.link]}
  - {id: denied_no_find, role: [catalog_denied], action: [read.find]}
"""
POLICY = POLICY.replace("{READS}", ", ".join(CATALOG_READS)).replace(
    "{NARROW_READS}", ", ".join((*ORGANIZATION, "ppl:Employee"))
)
TOKENS = {
    "maintainer": ("maintainer", "company_project"),
    "reader": ("reader", "company_project"),
    "agent": ("catalog_agent", "company_project"),
    "denied": ("catalog_denied", "company_project"),
    "scoped": ("catalog_agent", "read_project"),
    "narrow": ("catalog_narrow", "company_project"),
}
# What the grant of ``catalog_agent`` names, and entries of the graph it does not name: visible
# ones (they name types the role reads) and one a type limit hides (``legal`` is not in ``reads``).
GRANTED = {"members_of_unit", "vacant_positions", "assign_position", "unit_overview", "org_catalog"}
OTHERS = {
    "policies_for_unit",
    "required_trainings",
    "hand_over_position",
    "offboard_employee",
    "members_report",
    "position_overview",
    "employer_of",
}
TOOLS = {
    "run_members_of_unit",
    "run_vacant_positions",
    "do_assign_position",
    "view_unit_overview",
    "view_org_catalog",
    "lookup",
}
SINCE = "2026-03-01"
GRAPHLESS = {"graphs", "whoami"}  # the commands that take no graph


@pytest.fixture
def fresh(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Installation:
    """The installation of this test alone: a test that writes starts from the seeds."""
    return build(run_cli, isolated_env, tmp_path, policy=POLICY, tokens=TOKENS)


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Installation:
    """For the tests that only read: building the example costs seconds of CPU."""
    base = tmp_path_factory.mktemp("company")
    env = isolated(base)
    return build(runner(env, base), env, base, policy=POLICY, tokens=TOKENS)


def command(inst: Installation, path: str, who: str, /, **args: Any) -> Reply:
    """``POST /v1/commands/<path>`` for the graph; ``args`` are the arguments of the command."""
    graph = {} if path in GRAPHLESS else {"graph": GRAPH}
    return call(
        inst.server,
        "POST",
        f"/v1/commands/{path}",
        token=inst.tokens[who],
        body={"args": {**graph, **args}},
    )


def core(data: dict[str, Any]) -> dict[str, Any]:
    """An answer without its ``page`` block, which the tool carries in ``data`` and the command
    form in the envelope."""
    return {k: v for k, v in data.items() if k != "page"}


def ident(inst: Installation, text: str) -> str:
    """The id of the one entity named ``text``, as the maintainer finds it."""
    found = command(inst, "lookup", "maintainer", text=[text], **{"--match": "exact"})
    assert found.status == 200, found.json
    (item,) = [i for i in found.json["data"]["items"] if i["text"] == text]
    return str(item["id"])


@dataclass(frozen=True)
class Ids:
    """Entities the table's commands work on, and uploads for the commands that take a file."""

    inst: Installation
    unit: str  # Engineering, a department (it has a default view in the catalog)
    person: str  # Dana Whitfield (no default view)
    position: str  # Data Protection Officer, vacant

    def upload(self, content: dict[str, Any]) -> dict[str, str]:
        """An upload of the agent (uploads are used once and kept per token)."""
        reply = call(
            self.inst.server,
            "PUT",
            "/v1/uploads",
            token=self.inst.tokens["agent"],
            raw=json.dumps(content).encode(),
        )
        assert reply.status == 200, reply.json
        return {"upload": reply.json["upload"], "name": "changes.json"}

    def hold(self) -> dict[str, Any]:
        """The ops of ``assign_position`` as a document, to write by hand."""
        op = {"op": "link", "id": self.person, "rel": "org:holds", "target": self.position}
        return {"ops": [{**op, "qualifiers": {"since": SINCE}}]}


def ids(inst: Installation) -> Ids:
    return Ids(
        inst,
        ident(inst, "Engineering"),
        ident(inst, "Dana Whitfield"),
        ident(inst, "Data Protection Officer"),
    )


@dataclass(frozen=True)
class Outcome:
    """What the catalog caller gets: the HTTP status, the error code (none: success) and, for
    ``access.denied``, the action it names."""

    status: int
    code: str | None = None
    action: str | None = None


ALLOWED = Outcome(200)


def denied(action: str) -> Outcome:
    return Outcome(403, "access.denied", action)


def unknown(code: str) -> Outcome:
    return Outcome(404, code)


@dataclass(frozen=True)
class Row:
    """One command of the inventory, how it is asked for and what the catalog caller gets."""

    path: str
    args: Callable[[Ids], dict[str, Any]]
    expect: Outcome
    why: str


def row(path: str, args: Callable[[Ids], dict[str, Any]], expect: Outcome, why: str) -> Row:
    return Row(path, args, expect, why)


def param(i: Ids) -> list[str]:
    return [f"unit={i.unit}"]


ASSIGN = ["person={0}", "position={1}", f"since={SINCE}"]

# Every command the server serves has at least one row; the commands that depend on the entry
# named (describe, run, do, view) have a row per outcome. The reasons are the rules of the plan.
COMMANDS: tuple[Row, ...] = (
    # the entries of the grant
    row("run", lambda i: {"name": "members_of_unit", "--param": param(i)}, ALLOWED, "granted"),
    row(
        "run",
        lambda i: {"name": "policies_for_unit", "--param": param(i)},
        unknown("function.not_found"),
        "visible, not granted: as if it did not exist",
    ),
    row(
        "do",
        lambda i: {
            "name": "assign_position",
            "--dry-run": True,
            "--param": [f"person={i.person}", f"position={i.position}", f"since={SINCE}"],
        },
        ALLOWED,
        "granted",
    ),
    row(
        "do",
        lambda i: {
            "name": "hand_over_position",
            "--dry-run": True,
            "--param": [f"person={i.person}", f"position={i.position}", f"since={SINCE}"],
        },
        unknown("action.not_found"),
        "visible, not granted",
    ),
    row("view", lambda i: {"name": "unit_overview", "ref": i.unit}, ALLOWED, "granted"),
    row("view", lambda i: {"name": "org_catalog"}, ALLOWED, "granted set view"),
    row(
        "view",
        lambda i: {"name": "members_report", "--param": param(i)},
        unknown("view.not_found"),
        "visible, not granted",
    ),
    row("view", lambda i: {"ref": i.unit}, ALLOWED, "the default view of a unit is in the catalog"),
    row("view", lambda i: {"ref": i.person}, denied("read.get"), "the card needs read.get"),
    row(
        "call",
        lambda i: {"tool": "run_members_of_unit", "input": {"unit": i.unit}},
        ALLOWED,
        "the tool of a granted function",
    ),
    row(
        "call",
        lambda i: {"tool": "run_policies_for_unit", "input": {"unit": i.unit}},
        unknown("tool.not_found"),
        "the tool of a function outside the catalog does not exist",
    ),
    row(
        "lookup",
        lambda i: {"text": ["Engineering"], "--for": "members_of_unit.unit"},
        ALLOWED,
        "the narrow lookup for the reference slot of a granted entry",
    ),
    # the catalog and the model
    row("describe/function", lambda i: {"name": "members_of_unit"}, ALLOWED, "granted"),
    row(
        "describe/function",
        lambda i: {"name": "policies_for_unit"},
        unknown("function.not_found"),
        "not granted",
    ),
    row("describe/functions", lambda i: {}, ALLOWED, "lists the granted ones"),
    row("describe/action", lambda i: {"name": "assign_position"}, ALLOWED, "granted"),
    row(
        "describe/action",
        lambda i: {"name": "hand_over_position"},
        unknown("action.not_found"),
        "not granted",
    ),
    row("describe/actions", lambda i: {}, ALLOWED, "lists the granted ones"),
    row("describe/view", lambda i: {"name": "unit_overview"}, ALLOWED, "granted"),
    row(
        "describe/view",
        lambda i: {"name": "members_report"},
        unknown("view.not_found"),
        "not granted",
    ),
    row("describe/views", lambda i: {}, ALLOWED, "lists the granted ones"),
    row("describe/search", lambda i: {"text": "members"}, ALLOWED, "finds granted entries only"),
    row("describe/tools", lambda i: {}, ALLOWED, "the manifest of the granted tools"),
    row("describe/types", lambda i: {}, ALLOWED, "the model: read.describe is part of the grant"),
    row("describe/type", lambda i: {"type": "org:Department"}, ALLOWED, "the model"),
    row("describe/fields", lambda i: {"type": "ppl:Employee"}, ALLOWED, "the model"),
    row("describe/lifecycle", lambda i: {"type": "org:Position"}, ALLOWED, "the model"),
    row("describe/relations", lambda i: {}, ALLOWED, "the model"),
    row("describe/modules", lambda i: {}, ALLOWED, "the model"),
    row("describe/module", lambda i: {"module": "organization"}, ALLOWED, "the model"),
    row(
        "describe/module",
        lambda i: {"module": "legal"},
        unknown("term.unknown"),
        "a module the role does not read is no module",
    ),
    row("describe/schema", lambda i: {"--compact": True}, ALLOWED, "the model"),
    row("graphs", lambda i: {}, ALLOWED, "read.list"),
    row("start", lambda i: {}, ALLOWED, "the orientation of the caller"),
    row("whoami", lambda i: {}, ALLOWED, "the caller's own rights"),
    # the primitives: nothing of the grant opens them
    row("get", lambda i: {"ref": i.unit}, denied("read.get"), "read.get is not granted"),
    row("find", lambda i: {"--type": ["org:Department"]}, denied("read.find"), "not granted"),
    row("search", lambda i: {"text": "Engineering"}, denied("read.find"), "not granted"),
    row("lookup", lambda i: {"text": ["Engineering"]}, denied("read.find"), "the free lookup"),
    row("neighbors", lambda i: {"ref": i.unit}, denied("read.traverse"), "not granted"),
    row(
        "query",
        lambda i: {"text": "MATCH (n) RETURN n LIMIT 1"},
        denied("read.query"),
        "not granted",
    ),
    row("history", lambda i: {"ref": i.unit}, denied("read.history"), "not granted"),
    row("changes", lambda i: {"--since": "tx:0"}, denied("read.history"), "not granted"),
    row("file/check", lambda i: {"refs": [i.unit]}, denied("read.get"), "files need read.get"),
    row("file/path", lambda i: {"ref": i.unit}, denied("read.get"), "files need read.get"),
    # the writes by hand: the action is the only way to write
    row(
        "apply",
        lambda i: {"file": i.upload(i.hold())},
        denied("write.link"),
        "the ops of assign_position, written by hand",
    ),
    row(
        "apply",
        lambda i: {"file": i.upload(i.hold()), "--dry-run": True},
        denied("write.link"),
        "the same as a dry run",
    ),
    row(
        "create",
        lambda i: {"type": "ppl:External", "--set": ["name=X"], "--reason": "t"},
        denied("write.create"),
        "not granted",
    ),
    row(
        "update",
        lambda i: {"ref": i.unit, "--set": ["name=Y"], "--reason": "t"},
        denied("write.update"),
        "not granted",
    ),
    row(
        "link",
        lambda i: {
            "ref": i.person,
            "rel": "org:holds",
            "target": i.position,
            "--qualifier": [f"since={SINCE}"],
            "--reason": "t",
        },
        denied("write.link"),
        "what the action writes, by hand",
    ),
    row(
        "unlink",
        lambda i: {"ref": i.person, "rel": "org:holds", "target": i.position, "--reason": "t"},
        denied("write.unlink"),
        "not granted",
    ),
    row(
        "supersede",
        lambda i: {"by": i.person, "targets": [i.unit], "--reason": "t"},
        denied("write.supersede"),
        "not granted",
    ),
    row(
        "retract",
        lambda i: {"ref": i.person, "--reason": "t"},
        denied("write.retract"),
        "not granted",
    ),
    row(
        "transition",
        lambda i: {"ref": i.position, "state": "active", "--reason": "t"},
        denied("write.transition"),
        "not granted",
    ),
    row(
        "file/add",
        lambda i: {"path": i.upload({"x": 1})},
        Outcome(422, "file.module_missing"),
        "company has no files module: the command stops before the question of rights",
    ),
)


# ---- the manifest and the tools -----------------------------------------------------------


def test_the_manifest_holds_exactly_the_tools_of_the_grant(shared: Installation) -> None:
    client = ToolsClient.fetch(shared.server, shared.tokens["agent"], GRAPH)
    assert set(client.tools) == TOOLS
    # the lookup tool serves the reference slots of the tools it has and no other
    slots = client.tools["lookup"]["input_schema"]["properties"]["for"]["enum"]
    assert sorted(slots) == [
        "do_assign_position.person",
        "do_assign_position.position",
        "run_members_of_unit.unit",
        "run_vacant_positions.unit",
        "view_unit_overview._entity",
    ]
    # another role of the same graph has other tools; a role with a ref-less grant has no lookup
    full = ToolsClient.fetch(shared.server, shared.tokens["maintainer"], GRAPH)
    assert set(full.tools) > TOOLS and "run_policies_for_unit" in full.tools
    narrow = ToolsClient.fetch(shared.server, shared.tokens["narrow"], GRAPH)
    assert set(narrow.tools) == {"run_members_of_unit", "view_org_catalog", "lookup"}
    # no other entry is a tool, however its name is spelled (the text of a granted entry may
    # mention one: that is what its author wrote)
    assert not {n.split("_", 1)[1] for n in client.tools if n != "lookup"} & OTHERS


def test_every_tool_runs_through_call_and_through_the_command_form(
    fresh: Installation,
) -> None:
    """The harness calls the tools with the manifest's schemas (input and output checked); the
    command form gives the same answers. The action is a dry run here: ``test_the_action_...``
    writes."""
    i = ids(fresh)
    client = ToolsClient.fetch(fresh.server, fresh.tokens["agent"], GRAPH)
    unit = Ref("Engineering", "run_members_of_unit.unit")
    run = client.call("run_members_of_unit", {"unit": unit}).data
    assert run["items"] and run["function"]["name"] == "members_of_unit"
    vacant = client.call(
        "run_vacant_positions", {"unit": Ref("Management", "run_vacant_positions.unit")}
    )
    assert vacant.ok, vacant.body
    entity_view = client.call(
        "view_unit_overview", {"_entity": Ref("Engineering", "view_unit_overview._entity")}
    ).data
    assert entity_view["view"]["name"] == "unit_overview"
    set_view = client.call("view_org_catalog", {}).data
    assert [s["id"] for s in set_view["sections"]] == ["units", "positions", "people"]
    action_given = {
        "person": Ref("Dana Whitfield", "do_assign_position.person"),
        "position": Ref("Data Protection Officer", "do_assign_position.position"),
        "since": SINCE,
        "_dry_run": True,
    }
    done = client.call("do_assign_position", action_given).data
    assert done["action"]["name"] == "assign_position" and done["dry_run"] is True
    found = client.call("lookup", {"text": "Engineering", "for": "run_members_of_unit.unit"}).data
    assert [item["id"] for item in found["items"]] == [i.unit]
    # the command form answers the same
    by_run = command(fresh, "run", "agent", name="members_of_unit", **{"--param": param(i)})
    assert by_run.status == 200 and core(by_run.json["data"]) == core(run)
    by_view = command(fresh, "view", "agent", name="unit_overview", ref=i.unit)
    assert by_view.status == 200 and core(by_view.json["data"]) == core(entity_view)
    by_set = command(fresh, "view", "agent", name="org_catalog")
    assert by_set.status == 200 and core(by_set.json["data"]) == core(set_view)
    by_do = command(
        fresh, "do", "agent", name="assign_position", **{"--dry-run": True, "--param": [
            f"person={i.person}", f"position={i.position}", f"since={SINCE}"]},
    )  # fmt: skip
    assert by_do.status == 200 and core(by_do.json["data"]) == core(done)


# ---- the table of every command ---------------------------------------------------------------


def served_paths(inst: Installation) -> set[str]:
    """The commands the server's inventory lists, as the paths of ``POST /v1/commands/<path>``."""
    inventory = call(inst.server, "GET", "/v1/inventory", token=inst.tokens["agent"])
    assert inventory.status == 200, inventory.json
    return {"/".join(c["path"]) for c in inventory.json["commands"] if c.get("remote") != "client"}


def test_the_table_classifies_every_command_the_server_serves(shared: Installation) -> None:
    """A command that is new, or gone, fails here until its row says what a catalog caller gets:
    a primitive added later cannot become reachable without someone deciding so."""
    listed = {r.path for r in COMMANDS}
    served = served_paths(shared)
    assert served - listed == set(), f"commands without a row: {sorted(served - listed)}"
    assert listed - served == set(), (
        f"rows of commands the server does not serve: {sorted(listed - served)}"
    )


def whole_strings(value: Any) -> set[str]:
    """Every string of an answer that is a value or a key of its own (free text is one string, so a
    sentence that mentions an entry is not one)."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set(value) | {t for v in value.values() for t in whole_strings(v)}
    if isinstance(value, list):
        return {t for v in value for t in whole_strings(v)}
    return set()


def named_others() -> set[str]:
    """The entries outside the catalog by name, and as the tools of the manifest are named."""
    return OTHERS | {f"{kind}_{name}" for name in OTHERS for kind in ("run", "do", "view")}


@pytest.mark.parametrize("index", range(len(COMMANDS)), ids=lambda i: f"{COMMANDS[i].path}-{i}")
def test_each_command_gives_the_catalog_caller_the_outcome_of_its_row(
    shared: Installation, index: int
) -> None:
    spec = COMMANDS[index]
    reply = command(shared, spec.path, "agent", **spec.args(ids(shared)))
    error = reply.json.get("error")
    got = Outcome(
        reply.status,
        error["code"] if error else None,
        error["details"].get("action") if error and error["code"] == "access.denied" else None,
    )
    assert got == spec.expect, (spec.path, spec.why, reply.json)
    if got.code == "access.denied":  # a refusal names no entry outside the catalog
        assert not [name for name in OTHERS if name in json.dumps(error)], error
    if got.status == 200:  # an answer names no entry outside the catalog as a name of its own
        assert not whole_strings(reply.json["data"]) & named_others(), (spec.path, reply.json)
    listed = {"describe/functions": "functions", "describe/actions": "actions"}.get(spec.path)
    if listed or spec.path == "describe/views":
        key = listed or "views"
        assert {e["name"] for e in reply.json["data"][key]} <= GRANTED, (spec.path, reply.json)


# ---- no way to enumerate entities -------------------------------------------------------------


def tail(entity_id: str) -> str:
    """The 8 trailing hex digits of an id: a reference the full read path resolves."""
    return entity_id.replace("-", "")[-8:]


def asked(inst: Installation, who: str, unit: str) -> Reply:
    return command(inst, "run", who, name="members_of_unit", **{"--param": [f"unit={unit}"]})


def test_an_id_suffix_resolves_for_a_reader_and_for_no_catalog_caller(shared: Installation) -> None:
    """The oracle: a reader gives 8 trailing hex digits and gets the entity (or the candidates);
    a catalog caller gets the same answer for a suffix that exists, one of another class, a
    suffix that matches nothing and an id that does not exist, so none of them says whether
    anything is there."""
    i = ids(shared)
    assert asked(shared, "maintainer", tail(i.unit)).status == 200  # the suffix is real
    missing = "0123abcd"
    forms = {
        "suffix of a unit": tail(i.unit),
        "suffix of a person": tail(i.person),
        "suffix that matches nothing": missing,
        "id of a person": i.person,
        "id that does not exist": "0192f0aa-0000-7000-8000-000000000000",
        "alias that does not exist": "DEPT-9999",
    }
    answers = {}
    for label, text in forms.items():
        reply = asked(shared, "agent", text)
        assert reply.status in (400, 404), (label, reply.json)
        error = reply.json["error"]
        answers[label] = (
            reply.status,
            error["code"],
            error["message"].replace(text, "X"),
            json.dumps(error["details"]).replace(text, "X"),
        )
    suffixes = {k: v for k, v in answers.items() if "suffix" in k}
    assert len(set(suffixes.values())) == 1, suffixes
    ids_ = {k: v for k, v in answers.items() if k.startswith(("id ", "alias"))}
    assert len({v[:3] for v in ids_.values()}) == 1, ids_
    # the reference of a view takes no suffix either, named or by the default view of the class
    for args in ({"name": "unit_overview", "ref": "{}"}, {"ref": "{}"}):
        assert (
            command(shared, "view", "maintainer", **{**args, "ref": tail(i.unit)}).status == 200
        ), args
        seen = []
        for text in (tail(i.unit), missing):
            reply = command(shared, "view", "agent", **{**args, "ref": text})
            assert reply.status in (400, 403, 404), (args, reply.json)
            seen.append((reply.status, json.dumps(reply.json["error"]).replace(text, "X")))
        assert seen[0] == seen[1], (args, seen)
    # the same holds through the tool: the schema does not take a suffix at all
    client = ToolsClient.fetch(shared.server, shared.tokens["agent"], GRAPH)
    pattern = client.tools["run_members_of_unit"]["input_schema"]["properties"]["unit"]["pattern"]
    assert "{8,32}" not in pattern
    for text in (tail(i.unit), missing):
        hexed = client.call("run_members_of_unit", {"unit": text}, valid=False)
        assert hexed.code == "function.parameter_invalid", text
        assert "candidates" not in hexed.details and "matches" not in hexed.reply.error["message"]


def test_every_other_lookup_is_the_one_refusal_of_the_free_lookup(shared: Installation) -> None:
    ok = command(
        shared, "lookup", "agent", text=["Engineering"], **{"--for": "members_of_unit.unit"}
    )
    assert ok.status == 200 and ok.json["page"]["next_offset"] is None, ok.json
    assert [item["id"] for item in ok.json["data"]["items"]] == [ids(shared).unit]
    slot = "members_of_unit.unit"
    refused = {
        "free": {"text": ["Engineering"]},
        "prefix match": {"text": ["Eng"], "--for": slot, "--match": "prefix"},
        "offset": {"text": ["Engineering"], "--for": slot, "--offset": 1},
        "include inactive": {"text": ["Engineering"], "--for": slot, "--include-inactive": True},
        "relation slot": {"text": ["Engineering"], "--for": "part_of"},
        "source slot": {"text": ["Engineering"], "--for-source": "part_of"},
        "type filter": {"text": ["Engineering"], "--for": slot, "--type": ["org:Team"]},
        "ignore accents": {"text": ["Engineering"], "--for": slot, "--ignore-accents": True},
        "language": {"text": ["Engineering"], "--for": slot, "--lang": "de"},
    }
    bodies = {}
    for label, args in refused.items():
        reply = command(shared, "lookup", "agent", **args)
        bodies[label] = (reply.status, str(reply.json.get("error")), reply.json.get("data"))
    # every shape the narrow lookup does not serve is refused alike; the plain one is the plan's
    # ``access.denied`` of read.find on the graph and never an entry
    assert {v[0] for v in bodies.values()} == {403}, bodies
    assert len({v[1] for v in bodies.values()}) == 1, bodies
    assert all(v[2] is None for v in bodies.values())
    assert "read.find" in next(iter(bodies.values()))[1]
    # history is a right of its own, and not granted either
    past = command(
        shared, "lookup", "agent", text=["Engineering"],
        **{"--for": slot, "--as-of": "2020-01-01T00:00:00Z"},
    )  # fmt: skip
    assert (past.status, past.error["details"]["action"]) == (403, "read.history")
    # a slot of an entry outside the catalog is no slot, whether the entry exists or not
    other = command(
        shared, "lookup", "agent", text=["Engineering"], **{"--for": "policies_for_unit.unit"}
    )
    absent = command(
        shared, "lookup", "agent", text=["Engineering"], **{"--for": "policies_for_unitz.unit"}
    )
    assert (other.status, other.error["code"]) == (absent.status, absent.error["code"])
    assert other.error["message"].replace("policies_for_unit", "X") == absent.error[
        "message"
    ].replace("policies_for_unitz", "X")


# ---- the action writes through its grant -------------------------------------------------------


def holders(inst: Installation, position: str) -> list[str]:
    """Who holds the position, as the maintainer reads it."""
    found = command(
        inst,
        "neighbors",
        "maintainer",
        ref=position,
        **{"--rel": ["org:holds"], "--direction": "in"},
    )
    assert found.status == 200, found.json
    return [str(n["entity"]["id"]) for n in found.json["data"]["items"]]


def audit_events(inst: Installation) -> list[dict[str, Any]]:
    lines = []
    for segment in sorted(inst.root.rglob("*.jsonl")):
        if "audit" in segment.parts:
            lines += [json.loads(line) for line in segment.read_text().splitlines() if line]
    return lines


def assign(inst: Installation, who: str, i: Ids, *more: str, **options: Any) -> Reply:
    given = [f"person={i.person}", f"position={i.position}", f"since={SINCE}", *more]
    return command(inst, "do", who, name="assign_position", **{"--param": given}, **options)


def assign_other(inst: Installation, who: str, i: Ids) -> Reply:
    """``hand_over_position``, an action of the graph outside the catalog, with its parameters."""
    given = [f"person={i.person}", f"position={i.position}", f"since={SINCE}"]
    return command(inst, "do", who, name="hand_over_position", **{"--param": given})


AGENT_GRANT = "grant.4"  # the grant of ``catalog_agent`` in ``POLICY`` (the fourth)


@pytest.mark.parametrize("form", ["command", "call"])
def test_the_action_writes_what_the_token_cannot_write_by_hand(
    fresh: Installation, form: str
) -> None:
    i = ids(fresh)
    assert holders(fresh, i.position) == []
    # the ops of the action, by hand, are refused: ``link``, ``apply`` and ``create``
    by_hand = command(
        fresh, "link", "agent", ref=i.person, rel="org:holds", target=i.position,
        **{"--qualifier": [f"since={SINCE}"], "--reason": "by hand"},
    )  # fmt: skip
    assert (by_hand.status, by_hand.error["details"]["action"]) == (403, "write.link")
    document = command(fresh, "apply", "agent", file=i.upload(i.hold()))
    assert (document.status, document.error["details"]["action"]) == (403, "write.link")
    made = command(fresh, "create", "agent", type="ppl:External", **{"--set": ["name=X"]})
    assert (made.status, made.error["details"]["action"]) == (403, "write.create")
    assert holders(fresh, i.position) == []
    # the action writes them: through ``do`` or through the tool, with the grant of the entry
    if form == "command":
        done = assign(fresh, "agent", i)
        assert done.status == 200, done.json
        assert done.json["data"]["dry_run"] is False
    else:
        client = ToolsClient.fetch(fresh.server, fresh.tokens["agent"], GRAPH)
        given = {
            "person": i.person,
            "position": i.position,
            "since": SINCE,
        }
        answer = client.call("do_assign_position", given)
        assert answer.ok, answer.body
    assert holders(fresh, i.position) == [i.person]
    # the position is no longer vacant: the precondition of the action reads the new state
    again = assign(fresh, "agent", i)
    assert again.status in (409, 422), again.json


def refused_everywhere(inst: Installation, who: str, i: Ids) -> None:
    """``do`` (also as a dry run) and ``call`` of the action are ``access.denied`` and nothing is
    written."""
    for options in ({}, {"--dry-run": True}):
        reply = assign(inst, who, i, **options)
        assert (reply.status, reply.error["code"]) == (403, "access.denied"), (options, reply.json)
    called = command(
        inst, "call", who, tool="do_assign_position",
        input={"person": i.person, "position": i.position, "since": SINCE},
    )  # fmt: skip
    assert (called.status, called.error["code"]) == (403, "access.denied"), called.json
    assert holders(inst, i.position) == []


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_a_deny_rule_and_a_read_scope_refuse_the_action_but_not_the_reads(
    fresh: Installation,
) -> None:
    i = ids(fresh)
    for who in ("denied", "scoped"):
        refused_everywhere(fresh, who, i)
        # the same token still runs its function: only the write was taken away
        ran = command(fresh, "run", who, name="members_of_unit", **{"--param": param(i)})
        assert ran.status == 200, (who, ran.json)
    assert assign(fresh, "agent", i, **{"--dry-run": True}).status == 200  # control


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_a_type_limit_of_the_graph_refuses_the_action(
    run_cli: CliRunner, fresh: Installation, tmp_path: Path
) -> None:
    i = ids(fresh)
    assert assign(fresh, "agent", i, **{"--dry-run": True}).status == 200
    policy = write_policy(
        tmp_path / "graph_policies",
        "limit.yaml",
        'schema: graph_cli.policy/1\nid: graph\ntypes:\n  "ppl:External": read\n',
    )
    applied = admin(run_cli, "policy", "apply", str(policy), "--graph", GRAPH, "--reason", "limit")
    assert applied.code == 0, applied.envelope
    refused_everywhere(fresh, "agent", i)
    # by hand nothing is more allowed than before, and the reads go on
    ran = command(fresh, "run", "agent", name="members_of_unit", **{"--param": param(i)})
    assert ran.status == 200, ran.json


# ---- a set view lists what the token cannot find ----------------------------------------------


def sections_of(reply: Reply) -> dict[str, list[dict[str, Any]]]:
    assert reply.status == 200, reply.json
    return {s["id"]: s["items"] for s in reply.json["data"]["sections"]}


def test_a_set_view_lists_entities_the_token_cannot_find(shared: Installation) -> None:
    found = command(shared, "find", "agent", **{"--type": ["ppl:External"]})
    assert (found.status, found.error["code"]) == (403, "access.denied")
    everyone = command(shared, "find", "maintainer", **{"--type": ["ppl:External"]})
    externals = {str(r["attrs"]["name"]) for r in everyone.json["data"]["items"]}
    assert externals
    listed = sections_of(command(shared, "view", "agent", name="org_catalog"))
    assert set(listed) == {"units", "positions", "people"}
    names = {str(item["label"]) for item in listed["people"]}
    assert externals <= names  # what the role reads, the view lists
    assert {item["type"] for item in listed["people"]} == {"ppl:Employee", "ppl:External"}


def test_the_read_limits_of_the_role_still_filter_the_view(shared: Installation) -> None:
    """``catalog_narrow`` reads the organization and the employees: the people of the view are
    employees, and nothing of an external (name, id, an edge to one) is in the answer."""
    full = command(shared, "view", "agent", name="org_catalog")
    narrow = command(shared, "view", "narrow", name="org_catalog")
    hidden = [item for item in sections_of(full)["people"] if item["type"] == "ppl:External"]
    assert hidden
    shown = sections_of(narrow)
    assert shown["people"]
    assert {str(item["ref"]).split("-")[0] for item in shown["people"]} == {"EMP"}
    text = json.dumps(narrow.json)
    for item in hidden:
        for secret in (item["label"], item["ref"]):
            assert str(secret) not in text, secret
    assert "ppl:External" not in text


# ---- audit and explain ------------------------------------------------------------------------


def decisions(event: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(d["action"], d["effect"]): d["rule"] for d in event["authz"]["decisions"]}


def test_the_audit_names_the_grant_of_every_entry_step(
    run_cli: CliRunner, fresh: Installation
) -> None:
    i = ids(fresh)
    assert assign(fresh, "agent", i).status == 200
    assert assign(fresh, "denied", i).status == 403
    assert assign(fresh, "scoped", i).status == 403
    # a view the entry allows but a deny rule of the role keeps from finding: both decisions
    blind = command(fresh, "view", "denied", name="org_catalog")
    assert (blind.status, blind.error["code"]) == (403, "access.denied")
    # what is not in the catalog is unknown (checked: a reply of another kind would audit nothing
    # and the check of the trail below would hold for nothing), and the trail does not name it
    unknown_run = command(fresh, "run", "agent", name="policies_for_unit", **{"--param": param(i)})
    unknown_do = assign_other(fresh, "agent", i)
    unknown_view = command(fresh, "view", "agent", name="members_report")
    for reply, code in (
        (unknown_run, "function.not_found"),
        (unknown_do, "action.not_found"),
        (unknown_view, "view.not_found"),
    ):
        assert (reply.status, reply.error["code"]) == (404, code), reply.json
    assert command(fresh, "get", "agent", ref=i.unit).status == 403
    events = audit_events(fresh)

    def of(token: str, command_name: str) -> list[dict[str, Any]]:
        return [
            e
            for e in events
            if e["actor"]["principal"].get("id") == f"token:app_{token}"
            and e["request"]["command"] == command_name
        ]

    # a name outside the catalog is the denial of the graph's own right: one event per ask, no
    # entry in its resource (an unknown action records nothing, as for every caller)
    for command_name, read in (("run", "read.function"), ("view", "read.get")):
        (refusal,) = of("agent", command_name)
        assert decisions(refusal) == {(read, "deny"): "default.deny"}, refusal["authz"]
        (resource,) = {d["resource"] for d in refusal["authz"]["decisions"]}
        assert "/" not in resource, resource
        assert refusal["result"] == {"code": "access.denied", "status": "denied"}
    assert [e["result"]["status"] for e in of("agent", "do")] == ["ok"]
    (wrote,) = [e for e in of("agent", "do") if e["result"]["status"] == "ok"]
    assert wrote["request"]["action"]["name"] == "assign_position"
    assert wrote["request"]["action"]["hash"].startswith("sha256:")
    rules = decisions(wrote)
    assert rules[("write.action", "allow")] == f"{AGENT_GRANT}.actions"
    assert rules[("write.link", "allow")] == f"{AGENT_GRANT}.actions"  # the op, through the entry
    assert wrote["actions"] == ["read.get", "write.action", "write.link"]
    # the refusals name the action asked for and the rule that refused it
    for who, rule in (("denied", "deny.denied_no_link"), ("scoped", "scope.read_project.company")):
        (event,) = of(who, "do")
        assert event["result"] == {"code": "access.denied", "status": "denied"}
        assert event["request"]["action"]["name"] == "assign_position"
        assert rule in decisions(event).values(), (who, event["authz"])
    (view,) = of("denied", "view")
    assert decisions(view)[("read.view", "allow")] == "grant.5.views"
    assert "deny.denied_no_find" in decisions(view).values(), view["authz"]
    # what the caller asked of a name outside its catalog is the refusal of the graph's own
    # right, which names no entry
    text = json.dumps(events)
    for name in ("policies_for_unit", "hand_over_position", "members_report"):
        assert name not in text, name
    # the whole trail verifies
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0, verified.data


def test_policy_explain_explains_the_profile(run_cli: CliRunner, fresh: Installation) -> None:
    def explain(*args: str, project: str = "company_project", role: str = "catalog_agent") -> Any:
        found = admin(
            run_cli,
            "policy",
            "explain",
            "--graph",
            GRAPH,
            "--project",
            project,
            "--role",
            role,
            *args,
        )
        assert found.code == 0, found.envelope
        return found.data

    shown = explain()
    assert shown["level"] == "catalog" and shown["withheld"] == []
    catalog = shown["catalog"]
    assert catalog["catalog_only"] is True and catalog["reader_rules"] == []
    assert {
        k: sorted(e["name"] for e in catalog[k]) for k in ("functions", "actions", "views")
    } == {
        "functions": ["members_of_unit", "vacant_positions"],
        "actions": ["assign_position"],
        "views": ["org_catalog", "unit_overview"],
    }
    for kind in ("functions", "actions", "views"):
        published = {
            r["name"]: r["hash"] for r in admin(run_cli, "graph", kind, "show", GRAPH).data[kind]
        }
        for entry in catalog[kind]:
            assert entry["published"] is True and entry["hash"] == published[entry["name"]]
    for flag, name, rule in (
        ("--function", "members_of_unit", f"{AGENT_GRANT}.functions"),
        ("--view", "org_catalog", f"{AGENT_GRANT}.views"),
        ("--entry-action", "assign_position", f"{AGENT_GRANT}.actions"),
    ):
        (decision,) = explain(flag, name)["decisions"]
        assert (decision["effect"], decision["rule"]) == ("allow", rule), flag
    (not_granted,) = explain("--function", "policies_for_unit")["decisions"]
    assert (not_granted["effect"], not_granted["rule"]) == ("deny", "default.deny")
    # a project whose scope is read takes the action away, and the explanation says by which rule
    (scoped,) = explain("--entry-action", "assign_position", project="read_project")["decisions"]
    assert (scoped["effect"], scoped["rule"]) == ("deny", "scope.read_project.company")
    # a reader is no catalog caller
    assert "catalog" not in explain(role="reader")


# ---- markings still filter a view -------------------------------------------------------------

STAFF_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  plain: {description: "Lists the employees, holds no marking"}
  personal: {description: "Lists the employees, holds the personal marking"}
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [stf:personal, stf:pay]}
  - entrypoint: server
    principal: {role: plain}
    graphs: [example_graph]
    level: catalog
    catalog: {views: [employee_list]}
    reads: [stf:Employee]
  - entrypoint: server
    principal: {role: personal}
    graphs: [example_graph]
    level: catalog
    catalog: {views: [employee_list]}
    reads: [stf:Employee]
    markings: [stf:personal]
scopes:
  example_project: {example_graph: read}
no_project: {"*": write}
"""
EMPLOYEE_LIST = """\
schema: graph_cli.view/1
name: employee_list
module: staff
summary: Every employee with all the fields the caller sees.
use_when: List the employees with everything recorded about them.
sections:
  - kind: list
    id: employees
    type: stf:Employee
    fields: all
"""


@pytest.mark.doc_claim("security_model.catalog_grants")
def test_the_markings_of_the_caller_still_filter_a_set_view(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """The company example declares no marking, so this one runs on the ``staff`` fixture: the
    personal data of an employee is in the view for the token that holds the marking and for no
    other, though both are granted the same view and neither can ``find`` an employee."""
    init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    views = tmp_path / "views"
    views.mkdir()
    (views / "employee_list.yaml").write_text(EMPLOYEE_LIST)
    published = admin(run_cli, "graph", "views", "publish", "example_graph", str(views))
    assert published.code == 0, published.envelope
    policy = write_policy(tmp_path / "policies", "installation.yaml", STAFF_POLICY)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "markings")
    assert applied.code == 0, applied.envelope
    made = app(
        run_cli, "create", "stf:Employee", "--set=name=Ada Example", "--set=phone=555-0100",
        "--set=email=ada@example.org", "--set=salary=1234.50", "--set=weekly_hours=38",
        env={"GRAPH_CLI_GRAPH": "example_graph"},
    )  # fmt: skip
    assert made.code == 0, made.envelope
    tokens = {
        role: new_token(run_cli, f"app_{role}", "--project", "example_project", "--role", role)
        for role in ("plain", "personal")
    }
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)
    inst = Installation(server, tokens, tmp_path)

    def shown(role: str) -> dict[str, Any]:
        reply = call(
            server, "POST", "/v1/commands/view", token=tokens[role],
            body={"args": {"graph": "example_graph", "name": "employee_list"}},
        )  # fmt: skip
        assert reply.status == 200, reply.json
        (item,) = reply.json["data"]["sections"][0]["items"]
        fields: dict[str, Any] = item["fields"]
        return fields

    plain, personal = shown("plain"), shown("personal")
    assert plain["name"] == "Ada Example" and plain["phone"] == "555-0100"
    assert not {"email", "salary", "weekly_hours"} & set(plain), plain
    assert personal["email"] == "ada@example.org" and personal["weekly_hours"] == 38
    assert "salary" not in personal  # stf:pay is held by neither
    for role in ("plain", "personal"):
        found = call(
            server, "POST", "/v1/commands/find", token=tokens[role],
            body={"args": {"graph": "example_graph", "--type": ["stf:Employee"]}},
        )  # fmt: skip
        assert (found.status, found.error["code"]) == (403, "access.denied")
    assert inst.tokens is tokens


# ---- files: the commands the company example cannot show --------------------------------------

FILES_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  scanner: {description: "Catalog caller on a graph that stores files"}
grants:
  - {entrypoint: app, graphs: "*", level: write}
  - entrypoint: server
    principal: {role: scanner}
    graphs: [files_graph]
    level: catalog
    catalog: {functions: [scan_list]}
    reads: [nas:Scan]
scopes:
  files_project: {files_graph: write}
no_project: {"*": none}
"""


def test_a_catalog_caller_can_neither_add_nor_read_files(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """The ``company`` row of ``file add`` stops at the missing files module, so the rights of the
    file commands are pinned here on a graph that stores files: adding one is ``write.create``,
    checking, locating and downloading one is ``read.get``, and a catalog grant holds neither (the
    name of the entry is not published: the caller still reads ``nas:Scan`` and nothing else)."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "files_graph", "notes_and_sources", version="1.1.0")
    # the file is stored before the policy is applied, as the seeds of the company example are
    stored = app(
        run_cli, "file", "add", str(write_pdf(tmp_path / "scan.pdf")), "--type", "nas:Scan",
        "--no-origin", env={"GRAPH_CLI_GRAPH": "files_graph"},
    )  # fmt: skip
    assert stored.code == 0, stored.envelope
    policy = write_policy(tmp_path / "policies", "installation.yaml", FILES_POLICY)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "files")
    assert applied.code == 0, applied.envelope
    entity = stored.data["entity"]["id"]
    token = new_token(run_cli, "app_scanner", "--project", "files_project", "--role", "scanner")
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)

    def ask(command_path: str, /, **args: Any) -> Reply:
        return call(
            server, "POST", f"/v1/commands/{command_path}", token=token,
            body={"args": {"graph": "files_graph", **args}},
        )  # fmt: skip

    content = write_pdf(tmp_path / "other.pdf", "other").read_bytes()
    uploaded = call(server, "PUT", "/v1/uploads", token=token, raw=content)
    assert uploaded.status == 200, uploaded.json
    reference = {"upload": uploaded.json["upload"], "name": "other.pdf"}
    refused = {
        "file/add": ask("file/add", path=reference, **{"--type": "nas:Scan"}),
        "file/check": ask("file/check", refs=[entity]),
        "file/path": ask("file/path", ref=entity),
    }
    wanted = {"file/add": "write.create", "file/check": "read.get", "file/path": "read.get"}
    for path, reply in refused.items():
        assert (reply.status, reply.error["code"]) == (403, "access.denied"), (path, reply.json)
        assert reply.error["details"]["action"] == wanted[path], (path, reply.json)
    # the download route delegates to ``file path``: the same refusal, and no byte of the file
    download = call(server, "GET", f"/v1/graphs/files_graph/files/{entity}", token=token)
    assert (download.status, download.error["code"]) == (403, "access.denied"), download.body
    assert content not in download.body and b"%PDF" not in download.body
