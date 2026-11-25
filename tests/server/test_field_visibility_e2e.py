"""C: field visibility (markings) end to end through the server in-process (field visibility plan
section 4, step 8).

The ``staff`` fixture marks the personal data of employees (``salary`` also carries the pay
marking). The roles are installed with the real ``graph-admin``: ``hr`` writes and holds both
markings, ``payroll`` reads and holds the personal marking only, ``reporting`` reads and may
update employees but holds no marking. Token A holds ``reporting``, token B ``hr``, token C
``payroll``. Requests go through ``ServerApp`` without a socket.

A gets every read command (get, find, lookup, search, neighbors, query, run, history, changes,
``--as-of``, ``describe *``, the graph skill) without a trace of a marked attribute: no value,
name, marking, count, rule or skill text. A filter, a sort, a field list or a write that names
one is answered exactly as one that names an attribute the type does not declare, a write of the
current value included, and an update of another attribute keeps what A cannot see. B reads and
writes every attribute, C all but the salary. ``whoami`` says what was observed, the refusals are
audited with the decision that hides the marking and ``audit verify`` stays clean.
"""

import base64
import io
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init, write_documents
from support.fixtures import FIXTURES
from support.markings import (
    ACTION_SOURCES,
    FUNCTION_SOURCES,
    HIDDEN_NAMES,
    SECRETS,
    documents,
    leaks,
    recomputed,
)
from support.policies import write_policy
from support.server import Reply, call, new_token

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal and pay data of colleagues"}
  payroll: {description: "Payroll: personal data, not the pay"}
  reporting: {description: "Reporting: colleagues without personal data"}
grants:
  - entrypoint: server
    principal: {role: hr}
    graphs: [example_graph]
    level: write
    markings: [stf:personal, stf:pay]
  - entrypoint: server
    principal: {role: payroll}
    graphs: [example_graph]
    level: read
    markings: [stf:personal]
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    writes:
      stf:Employee: {create: true, update: true}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
SEED = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [stf:personal, stf:pay]}
no_project: {"*": write}
"""
GRAPH = {"--graph": "example_graph"}
SKILL = FIXTURES / "skills" / "blocks" / "graph-example-graph"
E1, E2, E3, E4 = "EMP-0001", "EMP-0002", "EMP-0003", "EMP-0004"
# What the caller wrote comes back in the answer (a name it asked for, a text it looked up).
ECHOED = {"lookup_hidden_label": "Ada Lovelace"}


class World:
    """The prepared installation: tokens, the data root, the sequence numbers of the data."""

    def __init__(self, server: ServerApp, installation: dict[str, Any]) -> None:
        self.server = server
        self.root: Path = installation["root"]
        self.tokens: dict[str, str] = installation["tokens"]
        self.graph_id: str = installation["graph_id"]
        self.first_tx: int = installation["first_tx"]

    def ask(self, who: str, path: str, **args: Any) -> Reply:
        body = {"args": {**GRAPH, **args}}
        return call(self.server, "POST", f"/v1/commands/{path}", token=self.tokens[who], body=body)

    def ok(self, who: str, path: str, **args: Any) -> Reply:
        reply = self.ask(who, path, **args)
        assert reply.status == 200, (path, args, reply.json)
        return reply

    def data(self, who: str, path: str, **args: Any) -> Any:
        return self.ok(who, path, **args).json["data"]

    def attrs(self, who: str, ref: str) -> dict[str, Any]:
        attrs: dict[str, Any] = self.data(who, "get", ref=ref)["entity"]["attrs"]
        return attrs


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF_V1))
    assert created.code == 0, created.envelope
    for kind, sources in (("functions", FUNCTION_SOURCES), ("actions", ACTION_SOURCES)):
        folder = tmp_path / kind
        folder.mkdir()
        for file, text in sources.items():
            (folder / file).write_text(text)
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    skill = tmp_path / "skill" / "graph-example-graph"
    shutil.copytree(SKILL, skill)
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(skill))
    assert published.code == 0, published.envelope
    # the data are written by the local app caller under a policy that grants it the markings
    # (the built-in policy grants none), so that ``audit verify`` finds every decision made
    # under the policy then in effect
    seed = write_policy(tmp_path / "policies", "seed.yaml", SEED)
    applied = admin(run_cli, "policy", "apply", str(seed), "--reason", "seed the data")
    assert applied.code == 0, applied.envelope
    seqs = write_documents(root, "example_graph", documents(), installed=True)
    policy = write_policy(tmp_path / "policies", "installation.yaml", INSTALLATION)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "field visibility")
    assert applied.code == 0, applied.envelope
    # reporting may create and update employees without holding a marking: dead rights
    assert [(w["code"], w["details"]["verb"]) for w in applied.warnings] == [
        ("admin.policy_unusable_write", "create"),
        ("admin.policy_unusable_write", "update"),
    ], applied.envelope
    tokens = {
        "a": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reporting"),
        "b": new_token(run_cli, "app_b", "--project", "example_project", "--role", "hr"),
        "c": new_token(run_cli, "app_c", "--project", "example_project", "--role", "payroll"),
    }
    return {
        "root": root,
        "tokens": tokens,
        "graph_id": created.data["id"],
        "first_tx": seqs[0],
    }


STAFF_V1 = FIXTURES / "ontologies" / "staff" / "1.0.0"


@pytest.fixture
def world(installation: dict[str, Any], isolated_env: dict[str, str], tmp_path: Path) -> World:
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO(), enroll=Enrollment)
    return World(server, installation)


def plain(body: Any) -> Any:
    """``body`` without the suggestions (``did_you_mean``, ``expected``): they follow the
    spelling of the name asked, so two different names get different ones."""
    if isinstance(body, dict):
        return {k: plain(v) for k, v in body.items() if k not in ("did_you_mean", "expected")}
    if isinstance(body, list):
        return [plain(v) for v in body]
    return body


def same_but_for(
    world: World,
    who: str,
    path: str,
    asked: dict[str, Any],
    absent: dict[str, Any],
    swap: tuple[str, str],
) -> Reply:
    """The answers to a request that names a hidden thing and to one that names a missing one are
    the same but for the name (and the suggestions, which follow the spelling)."""
    hidden, missing = world.ask(who, path, **asked), world.ask(who, path, **absent)
    assert hidden.status == missing.status, (path, asked, hidden.json, missing.json)
    assert json.dumps(plain(hidden.json)).replace(*swap) == json.dumps(plain(missing.json)), (
        path,
        asked,
    )
    return hidden


def skill_text(world: World, who: str) -> tuple[str, dict[str, Any]]:
    reply = call(
        world.server,
        "POST",
        "/v1/graphs/example_graph/skill",
        token=world.tokens[who],
        body={},
    )
    assert reply.status == 200, reply.json
    skill = reply.json["data"]["skill"]
    text = b"".join(base64.b64decode(c) for c in skill["files"].values()).decode()
    return text, skill["source"]


def graph_of(world: World, who: str) -> dict[str, Any]:
    """What ``whoami`` says about ``example_graph``."""
    reply = call(
        world.server,
        "POST",
        "/v1/commands/whoami",
        token=world.tokens[who],
        body={"args": {}},
    )
    assert reply.status == 200, reply.json
    (graph,) = reply.json["data"]["permissions"]["graphs"]
    assert isinstance(graph, dict)
    return graph


def events_of(world: World, principal: str) -> list[dict[str, Any]]:
    lines = FileAuditLog(world.root).read_lines(INSTALLATION_STREAM)
    report = verify_stream(lines, stream=INSTALLATION_STREAM)
    assert report.findings == ()
    return [dict(e) for e in report.events if e["actor"]["principal"]["id"] == principal]


def read_everything(world: World, who: str) -> dict[str, Reply]:
    """Every read command once, about entities with marked attributes, and the schema in full."""
    tx = f"tx:{world.first_tx}"
    cypher = "MATCH (e:Employee)-[r]->(x) RETURN e.name AS name, type(r) AS rel, x.name AS target"
    asked: dict[str, tuple[str, dict[str, Any]]] = {
        "get": ("get", {"ref": E1}),
        "get_full": ("get", {"ref": E1, "--full": True}),
        "get_rev": ("get", {"ref": E1, "--rev": 1, "--full": True}),
        "get_fields": ("get", {"ref": E1, "--fields": ["name,phone,shift"]}),
        "get_affordances": ("get", {"ref": E2, "--affordances": True}),
        "get_as_of": ("get", {"ref": E1, "--as-of": tx}),
        "get_badge": ("get", {"ref": "BDG-0001"}),
        "get_contractor": ("get", {"ref": "CON-0001"}),
        "find": ("find", {"--limit": 100}),
        "find_employee": ("find", {"--type": ["stf:Employee"], "--full": True}),
        "find_colleagues": ("find", {"--type": ["stf:Colleague"], "--sort": ["name"]}),
        "find_state": ("find", {"--type": ["stf:Employee"], "--where": ["stage=started"]}),
        "find_badges": ("find", {"--type": ["stf:Badge"], "--sort": ["number"]}),
        "find_as_of": ("find", {"--limit": 100, "--as-of": tx}),
        "lookup": ("lookup", {"text": ["Ada"]}),
        "lookup_hidden_label": ("lookup", {"text": ["Ada Lovelace"]}),
        "search": ("search", {"text": "ada"}),
        "search_hidden": ("search", {"text": "lovelace"}),
        "neighbors": ("neighbors", {"ref": E1, "--depth": 2, "--direction": "both"}),
        "neighbors_full": ("neighbors", {"ref": "CREW-0001", "--depth": 2, "--full": True}),
        "history": ("history", {"ref": E1}),
        "history_e2": ("history", {"ref": E2}),
        "changes": ("changes", {"--since": "tx:0", "--limit": 500}),
        "changes_page": ("changes", {"--since": "tx:0", "--limit": 3}),
        "query_count": ("query", {"text": "MATCH (e:Employee) RETURN count(e) AS c"}),
        "query_edges": ("query", {"text": cypher}),
        "query_nodes": ("query", {"text": "MATCH (e:Employee) RETURN e"}),
        "query_properties": ("query", {"text": "MATCH (e:Colleague) RETURN properties(e) AS p"}),
        "query_unlabelled": ("query", {"text": "MATCH (n) RETURN n.name AS name ORDER BY name"}),
        "query_as_of": ("query", {"text": "MATCH (n) RETURN count(n) AS c", "--as-of": tx}),
        "run": ("run", {"name": "colleague_names"}),
        "describe_types": ("describe/types", {}),
        "describe_type": ("describe/type", {"type": "stf:Employee"}),
        "describe_fields": ("describe/fields", {"type": "stf:Employee"}),
        "describe_lifecycle": ("describe/lifecycle", {"type": "stf:Employee"}),
        "describe_relations": ("describe/relations", {}),
        "describe_modules": ("describe/modules", {}),
        "describe_module": ("describe/module", {"module": "staff"}),
        "describe_schema": ("describe/schema", {}),
        "describe_schema_compact": ("describe/schema", {"--compact": True}),
        "describe_search": ("describe/search", {"text": "pay"}),
        "describe_functions": ("describe/functions", {}),
        "describe_actions": ("describe/actions", {}),
        "describe_action": ("describe/action", {"name": "set_phone"}),
    }
    return {name: world.ask(who, path, **args) for name, (path, args) in asked.items()}


def traces(name: str, reply: Reply) -> list[str]:
    """The hidden values and terms in an answer, not counting the text the caller asked for."""
    text = reply.body.decode().replace(ECHOED.get(name, "\0"), "")
    return leaks(text)


@pytest.mark.doc_claim("security_model.markings", "security_model.field_visibility_complete")
def test_field_visibility_end_to_end(run_cli: CliRunner, world: World) -> None:
    # 1. A reads every command without a trace of a marked attribute
    seen_by_a = read_everything(world, "a")
    for name, reply in seen_by_a.items():
        assert reply.status == 200, (name, reply.json)
        assert traces(name, reply) == [], name
    text, source = skill_text(world, "a")
    assert leaks(text) == [] and source["visible"].startswith("sha256:")
    # what is left of an employee: the visible fields, its edges, rev and tx as stored
    ada = seen_by_a["get_full"].json["data"]["entity"]
    assert sorted(ada["attrs"]) == ["name", "phone", "shift", "stage"]
    assert ada["content_hash"] == recomputed(ada)
    assert len(ada["edges"]) == 2 and (ada["rev"], ada["tx_seq"]) == (2, 2)
    assert (
        recomputed(seen_by_a["get_rev"].json["data"]["entity"])
        == (seen_by_a["get_rev"].json["data"]["entity"]["content_hash"])
    )
    assert seen_by_a["find"].json["page"]["total"] == 11
    assert rows(seen_by_a["query_count"]) == [{"c": 4}]
    # the only label of the badge is hidden: the badge shows its number, a lookup finds nothing
    assert seen_by_a["get_badge"].json["data"]["entity"]["attrs"] == {"number": 1}
    assert seen_by_a["lookup_hidden_label"].json["page"]["total"] == 0
    assert seen_by_a["search_hidden"].json["page"]["total"] == 0
    # the e-mail of a contractor is not marked: it is there for everyone
    assert seen_by_a["get_contractor"].json["data"]["entity"]["attrs"]["email"] == (
        "dee@agency.example"
    )
    # a transition guarded by a marked attribute is listed (accepted residual); the rules,
    # the enum and the datatype that only marked attributes use are not in the schema
    schema = seen_by_a["describe_schema"].json["data"]
    assert "stf:Employee" in json.dumps(schema)
    fields = seen_by_a["describe_fields"].json["data"]["fields"]
    assert sorted(f["name"] for f in fields) == ["name", "phone", "shift", "stage"]
    assert "markings" not in json.dumps(seen_by_a["describe_type"].json)
    assert [a["name"] for a in seen_by_a["describe_actions"].json["data"]["actions"]] == [
        "set_phone"
    ]
    assert [f["name"] for f in seen_by_a["describe_functions"].json["data"]["functions"]] == [
        "colleague_names"
    ]
    # revision 3 of the first employee changed hidden values and the phone; the one of the
    # second only a hidden attribute and a visible one; none names what changed under a mark
    for item in seen_by_a["history"].json["data"]["items"]:
        assert not set((item["changed"] or {"attrs": []})["attrs"]) & set(HIDDEN_NAMES)

    # the feed names the same changes and never a marked attribute; a page resumes through
    # its cursor to the same whole
    feed = seen_by_a["changes"].json["data"]
    assert feed["view"] is not None and feed["more"] is False
    names = {
        n
        for e in feed["items"]
        for i in e["entities"]
        for n in (i["changed"] or {"attrs": []})["attrs"]
    }
    assert "phone" in names and not names & set(HIDDEN_NAMES), names
    page, resumed = seen_by_a["changes_page"].json["data"], []
    while True:
        resumed += [i["id"] for e in page["items"] for i in e["entities"]]
        if not page["more"]:
            break
        reply = world.ok("a", "changes", **{"--since": page["next"], "--limit": 3})
        assert traces("changes_page", reply) == []
        page = reply.json["data"]
    assert resumed == [i["id"] for e in feed["items"] for i in e["entities"]]

    # 2. what A asks about a marked attribute is answered as about one the type lacks
    for hidden in ("weekly_hours", "salary", "badge_code", "pay_grade", "birth_date"):
        for path, make in (
            ("find", lambda x: {"--type": ["stf:Employee"], "--where": [f"{x}=1"]}),
            ("find", lambda x: {"--type": ["stf:Employee"], "--sort": [x]}),
            ("find", lambda x: {"--type": ["stf:Employee"], "--fields": [x]}),
            ("get", lambda x: {"ref": E1, "--fields": [x]}),
            ("query", lambda x: {"text": f"MATCH (e:Employee) WHERE e.{x} > 1 RETURN e.name"}),
            ("query", lambda x: {"text": f"MATCH (e:Employee) RETURN e.name ORDER BY e.{x}"}),
            ("search", lambda x: {"text": "ada", "--field": x}),
            ("describe/search", lambda x: {"text": x}),
        ):
            same_but_for(world, "a", path, make(hidden), make("gone_field"), (hidden, "gone_field"))
    assert world.ask("a", "describe/search", text="weekly_hours").json["data"]["items"] == []

    # 3. B holds both markings and sees everything; C all but the salary
    seen_by_b = read_everything(world, "b")
    for name, reply in seen_by_b.items():
        assert reply.status == 200, (name, reply.json)
    whole = seen_by_b["get_full"].json["data"]["entity"]
    assert whole["attrs"]["email"] == "ada@new.example"
    assert whole["attrs"]["salary"] == "91234.50"
    assert whole["attrs"]["weekly_hours"] == 35
    assert whole["content_hash"] == recomputed(whole) != ada["content_hash"]
    assert seen_by_b["lookup_hidden_label"].json["page"]["total"] == 1
    assert rows(seen_by_b["query_count"]) == [{"c": 4}]
    assert {"employee_mail", "colleague_names", "by_grade"} <= {
        f["name"] for f in seen_by_b["describe_functions"].json["data"]["functions"]
    }
    employee = seen_by_b["describe_type"].json["data"]["type"]
    marked = {f["name"]: f["markings"] for f in employee["fields"] if "markings" in f}
    assert marked["salary"] == ["stf:pay", "stf:personal"]
    assert marked["email"] == ["stf:personal"]
    text_b, source_b = skill_text(world, "b")
    assert "weekly_hours" in text_b and "marked stf:pay+stf:personal" in text_b
    assert "visible" not in source_b  # no limit, no digest
    # the feed of a holder names the hidden attributes, under another view digest
    feed_b = seen_by_b["changes"].json["data"]
    names_b = {
        n
        for e in feed_b["items"]
        for i in e["entities"]
        for n in (i["changed"] or {"attrs": []})["attrs"]
    }
    assert {"weekly_hours", "email", "pay_grade"} <= names_b and feed_b["view"] is None
    held_by_c = world.attrs("c", E1)
    assert held_by_c["email"] == "ada@new.example" and "salary" not in held_by_c
    assert held_by_c["weekly_hours"] == 35
    for field_ in ("salary", "gone_field"):  # the pay is as unknown to C as a field that is not
        asked = {"--type": ["stf:Employee"], "--where": [f"{field_}:exists=true"]}
        assert world.ask("c", "find", **asked).status == 404
    same_but_for(
        world,
        "c",
        "find",
        {"--type": ["stf:Employee"], "--where": ["salary:exists=true"]},
        {"--type": ["stf:Employee"], "--where": ["gone_field:exists=true"]},
        ("salary", "gone_field"),
    )
    holder = {"--type": ["stf:Employee"], "--where": ["salary:exists=true"]}
    assert world.ask("b", "find", **holder).json["page"]["total"] == 1
    text_c, source_c = skill_text(world, "c")
    assert "salary" not in text_c and "weekly_hours" in text_c
    assert len({source["visible"], source_c["visible"]}) == 2

    # 4. whoami says what was observed
    graph_a, graph_b, graph_c = (graph_of(world, who) for who in ("a", "b", "c"))
    assert graph_a["markings"] == []
    assert graph_c["markings"] == ["stf:personal"]
    assert "markings" not in graph_b
    assert graph_a["writes"] == {"stf:Employee": {"create": True, "update": True}}
    assert leaks(graph_a) == [] and "stf:" not in json.dumps(graph_a["markings"])
    assert sorted(world.attrs("a", E1)) == ["name", "phone", "shift", "stage"]
    assert "salary" in world.attrs("b", E1) and "pay_grade" in world.attrs("c", E1)

    # 5. A's writes: a marked attribute is unknown, a guess of its value too, and an update of
    #    another attribute keeps what A cannot see
    before = world.data("b", "get", ref=E1, **{"--full": True})["entity"]
    for field_, value in (
        ("weekly_hours", "35"),  # the current value
        ("weekly_hours", "36"),
        ("salary", "91234.50"),  # the current value
        ("salary", "1"),
        ("email", "ada@new.example"),
    ):
        for verb, flag in (("update", "--set"), ("update", "--unset")):
            args: dict[str, Any] = {"ref": E1}
            if flag == "--set":
                args[flag] = [f"{field_}={value}"]
                other = {**args, flag: [f"gone_field={value}"]}
            else:
                args[flag] = [field_]
                other = {**args, flag: ["gone_field"]}
            hidden_reply = world.ask("a", verb, **args)
            missing_reply = world.ask("a", verb, **other)
            assert hidden_reply.status == missing_reply.status == 422, hidden_reply.json
            assert hidden_reply.error["code"] == "write.invalid"
            first, second = plain(hidden_reply.json), plain(missing_reply.json)
            assert json.dumps(first).replace(field_, "gone_field") == json.dumps(second)
            assert leaks(first, SECRETS) == []
    # a create that sets one is refused the same way
    created = world.ask("a", "create", type="stf:Employee", **{"--set": ["name=Fay", "salary=1"]})
    absent = world.ask("a", "create", type="stf:Employee", **{"--set": ["name=Fay", "gone=1"]})
    assert created.status == absent.status == 422
    assert json.dumps(plain(created.json)).replace("salary", "gone") == json.dumps(
        plain(absent.json)
    )
    assert world.ask("b", "find", **{"--where": ["name=Fay"]}).json["page"]["total"] == 0
    # the visible attribute of an update goes through; the hidden ones stay
    world.ok("a", "update", ref=E1, **{"--set": ["phone=+1 555 0100"]})
    after = world.data("b", "get", ref=E1, **{"--full": True})["entity"]
    assert after["attrs"]["phone"] == "+1 555 0100"
    kept = {k: v for k, v in after["attrs"].items() if k != "phone"}
    assert kept == {k: v for k, v in before["attrs"].items() if k != "phone"}
    assert after["rev"] == before["rev"] + 1
    assert world.attrs("a", E1)["phone"] == "+1 555 0100"
    # B writes the marked attributes, C writes what it holds and meets the salary as unknown
    world.ok("b", "update", ref=E1, **{"--set": ["weekly_hours=36", "salary=95000.00"]})
    assert world.attrs("b", E1)["weekly_hours"] == 36
    assert world.attrs("b", E1)["salary"] == "95000.00"
    # the feed of A lists both writes: the phone, and the hidden-only update with nothing
    # changed (a documented residual) and no trace of what it set
    after_feed = world.ok("a", "changes", **{"--since": "tx:0", "--limit": 500})
    assert traces("changes", after_feed) == []
    last_two = after_feed.json["data"]["items"][-2:]
    assert [e["entities"][0]["changed"]["attrs"] for e in last_two] == [["phone"], []]
    assert [e["entities"][0]["id"] for e in last_two] == [last_two[0]["entities"][0]["id"]] * 2
    assert world.ask("c", "update", ref=E1, **{"--set": ["weekly_hours=37"]}).status == 403
    # an action that sets a marked attribute is not found for A and runs for B
    for name in ("set_email", "set_salary", "set_grade", "set_phone_badged"):
        hidden_action = world.ask("a", "do", name=name)
        missing_action = world.ask("a", "do", name="gone_action")
        assert hidden_action.status == missing_action.status
        assert json.dumps(plain(hidden_action.json)).replace(name, "gone_action") == json.dumps(
            plain(missing_action.json)
        )
    done = world.ok(
        "b", "do", name="set_email", **{"--param": [f"who={E2}", "value=bo@new.example"]}
    )
    assert done.json["data"]["entities"]
    assert world.attrs("b", E2)["email"] == "bo@new.example"
    # the second employee was never touched by A: what A reads of it has no trace either
    assert leaks(world.ask("a", "get", ref=E2).json, SECRETS) == []

    # 6. the audit trail verifies and records the decision that hides the marking
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0 and verified.data["findings"] == []
    denied = [e for e in events_of(world, "token:app_a") if e["result"]["status"] == "denied"]
    assert denied, "the refused writes of a marked attribute are audited"
    hiding = {
        (d["action"], d["effect"])
        for e in denied
        for d in e["authz"]["decisions"]
        if d["resource"].startswith(f"marking:{world.graph_id}/")
    }
    assert ("read.get", "deny") in hiding
    # the other callers' events carry no marking decision of a refusal
    assert not [
        d
        for e in events_of(world, "token:app_b")
        for d in e["authz"]["decisions"]
        if d["effect"] == "deny"
    ]


def rows(reply: Reply) -> list[Any]:
    return list(reply.json["data"]["items"])
