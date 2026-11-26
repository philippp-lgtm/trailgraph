"""C: a server token limited to the catalog (catalog callers plan, steps 1 and 2): the commands
that list, describe and count entries show exactly the entries its grant names and nothing else;
the answers for an entry outside the catalog are those for an entry that does not exist; a granted
function or view runs (``run``, ``view --name``, ``call``) although the token holds neither
``read.function`` nor ``read.get`` on the graph, and a granted action writes (``do``, ``call``)
although the token could not write by hand; ``whoami`` shows the level ``catalog`` and the names it
sees; token creation and enrollment count the grant as granted.

The role ``catalog_agent`` is granted ``items_by_condition``, ``move_item`` and ``item_catalog`` of
``example_graph`` (inventory), ``reader`` reads everything, ``idle`` is granted nothing. The server
runs in-process, as for the other token tests.
"""

import io
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.catalog_leaks import assert_hidden_absent
from support.fixtures import FIXTURES
from support.policies import write_policy
from support.server import call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  catalog_agent: {description: "Runs three entries of example_graph"}
  reader: {description: "Reads example_graph"}
  mover: {description: "Reads example_graph and moves items through one action"}
  idle: {description: "Granted nothing"}
grants:
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [example_graph]
    level: catalog
    catalog:
      functions: [items_by_condition]
      actions: [move_item]
      views: [item_catalog]
    reads: [inv:Item, inv:Location, inv:Category]
  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}
  - entrypoint: server
    principal: {role: mover}
    graphs: [example_graph]
    level: read
    catalog: {actions: [move_item]}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
GRAPH = {"--graph": "example_graph"}
GRANTED = {"items_by_condition", "move_item", "item_catalog"}
OTHERS = {"items_in_location", "retire_item", "location_overview"}  # published, not granted
EVERYTHING = GRANTED | OTHERS | {"unit"}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    for kind in ("functions", "actions", "views"):
        folder = FIXTURES / kind / "inventory" / "valid"
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    # the data the actions work on is written under the built-in policy, before the installation's
    env = {"GRAPH_CLI_GRAPH": "example_graph"}
    ids: dict[str, str] = {}
    for name, cls, sets in (
        ("shelf", "inv:Location", ("name=Shelf", "code=S1")),
        ("rack", "inv:Location", ("name=Rack", "code=R1")),
    ):
        made = app(run_cli, "create", cls, *(f"--set={s}" for s in sets), env=env)
        assert made.code == 0, made.envelope
        ids[name] = made.data["entities"][0]["id"]
    made = app(
        run_cli, "create", "inv:Item", "--set=name=Chair", f"--edge=inv:located_in={ids['shelf']}",
        env=env,
    )  # fmt: skip
    assert made.code == 0, made.envelope
    ids["chair"] = made.data["entities"][0]["id"]
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "catalog caller")
    assert applied.code == 0, applied.envelope
    tokens = {
        name: new_token(run_cli, f"app_{name}", "--project", "example_project", "--role", name)
        for name in ("catalog_agent", "reader", "mover")
    }
    return {"env": isolated_env, "tokens": tokens, "root": root, "ids": ids}


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> ServerApp:
    return ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )


def command(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


@pytest.fixture
def agent(installation: dict[str, Any]) -> str:
    return str(installation["tokens"]["catalog_agent"])


@pytest.fixture
def reader(installation: dict[str, Any]) -> str:
    return str(installation["tokens"]["reader"])


@pytest.fixture
def mover(installation: dict[str, Any]) -> str:
    return str(installation["tokens"]["mover"])


def names(reply: Any, key: str) -> list[str]:
    assert reply.status == 200, reply.json
    return [i["name"] for i in reply.json["data"][key]]


# ---- whoami, start ------------------------------------------------------------------------


def test_whoami_shows_the_level_catalog_and_the_names_the_token_sees(
    served: ServerApp, agent: str, reader: str
) -> None:
    seen = command(served, "whoami", agent)
    assert seen.status == 200, seen.json
    (graph,) = seen.json["data"]["permissions"]["graphs"]
    assert graph["level"] == "catalog" and graph["withheld"] == []
    assert graph["catalog"] == {
        "functions": ["items_by_condition"],
        "actions": ["move_item"],
        "views": ["item_catalog"],
        "catalog_only": True,
        "reader_rules": [],
    }
    assert "writes" not in graph
    allowed = seen.json["data"]["permissions"]["allowed"]
    assert "write.action" not in allowed and "read.view" not in allowed
    other = command(served, "whoami", reader).json["data"]["permissions"]["graphs"][0]
    assert other["level"] == "read" and "catalog" not in other


def test_whoami_shows_the_obligations_the_granted_action_holds_its_writes_to(
    served: ServerApp, agent: str, reader: str, mover: str
) -> None:
    """The token has no write by hand, so the obligations of its own writes are empty; the ones
    ``do`` will hold the writes of its action to (the built-in reason for a supersede or a
    retract, the entry's own) are what whoami shows (step 3 review N4)."""

    def obligations(token: str) -> dict[str, Any]:
        (graph,) = command(served, "whoami", token).json["data"]["permissions"]["graphs"]
        shown: dict[str, Any] = graph["obligations"]
        return shown

    assert obligations(agent) == {
        "write.retract": {"require_reason": True},
        "write.supersede": {"require_reason": True},
    }
    # a reader with an action gets the same; a plain reader writes nothing, so nothing holds it
    assert obligations(mover) == obligations(agent)
    assert obligations(reader) == {}


def test_start_counts_the_catalog_only_and_says_level_catalog(
    served: ServerApp, agent: str, reader: str
) -> None:
    started = command(served, "start", agent)
    assert started.status == 200, started.json
    (graph,) = started.json["data"]["graphs"]
    assert graph["level"] == "catalog" and graph["withheld"] == []
    assert graph["counts"]["functions"] == 1 and graph["counts"]["views"] == 1
    assert graph["counts"]["actions"] == 1 and "not_allowed" not in graph
    full = command(served, "start", reader).json["data"]["graphs"][0]
    assert full["counts"]["functions"] == 2 and full["level"] == "read"


# ---- the listings -------------------------------------------------------------------------


def test_every_listing_names_the_granted_entries_only(
    served: ServerApp, agent: str, reader: str
) -> None:
    every = {"--include-not-allowed": True, **GRAPH}
    assert names(command(served, "describe/functions", agent, **every), "functions") == [
        "items_by_condition"
    ]
    assert names(command(served, "describe/actions", agent, **every), "actions") == ["move_item"]
    assert names(command(served, "describe/views", agent, **every), "views") == ["item_catalog"]
    # what the reader sees of the same catalogs is more
    assert len(names(command(served, "describe/functions", reader, **GRAPH), "functions")) == 2
    answers: list[Any] = []
    for path, args in (
        ("describe/functions", every),
        ("describe/actions", every),
        ("describe/views", every),
        ("describe/modules", GRAPH),
        ("describe/tools", GRAPH),
        ("describe/types", GRAPH),
    ):
        reply = command(served, path, agent, **args)
        assert reply.status == 200, (path, reply.json)
        answers.append(reply.json)
    for text in ("items", "retire", "location", "move", "catalog", "condition"):
        reply = command(served, "describe/search", agent, text=text, **GRAPH)
        assert reply.status == 200, reply.json
        answers.append(reply.json)
        hits = [h["curie"] for h in reply.json["data"]["items"] if h["kind"] in KINDS]
        assert set(hits) <= GRANTED, (text, hits)
    for answer in answers:
        assert_hidden_absent(answer["data"], OTHERS)
    modules = command(served, "describe/modules", agent, **GRAPH).json["data"]["modules"]
    assert [m["not_allowed"] for m in modules if "not_allowed" in m] == []
    # the manifest is exactly the tools the token may use: its function, its action and its view
    tools = command(served, "describe/tools", agent, **GRAPH).json["data"]["tools"]
    assert sorted(t["name"] for t in tools) == [
        "do_move_item",
        "run_items_by_condition",
        "view_item_catalog",
    ]


KINDS = ("function", "action", "view")
GRANTED_OF = {"function": "items_by_condition", "action": "move_item", "view": "item_catalog"}


def test_a_granted_entry_is_described_as_allowed_and_another_is_unknown(
    served: ServerApp, agent: str
) -> None:
    shown = command(served, "describe/function", agent, name="items_by_condition", **GRAPH)
    assert shown.status == 200, shown.json
    assert "allowed" not in shown.json["data"]["function"]  # allowed entries carry no mark
    action = command(served, "describe/action", agent, name="move_item", **GRAPH)
    assert "allowed" not in action.json["data"]["action"]  # allowed through its grant
    for path, hidden, missing, swap in (
        ("describe/function", "items_in_location", "items_in_nowhere", "function"),
        ("describe/action", "retire_item", "retire_itemm", "action"),
        ("describe/view", "location_overview", "location_overviewz", "view"),
    ):
        unnamed = command(served, path, agent, name=hidden, **GRAPH)
        absent = command(served, path, agent, name=missing, **GRAPH)
        assert unnamed.status == absent.status == 404, (unnamed.json, absent.json)
        assert unnamed.error["code"] == absent.error["code"] == f"{swap}.not_found"
        first, second = unnamed.json, absent.json
        for body in (first, second):
            assert body["error"]["details"].pop("did_you_mean") in ([], [GRANTED_OF[swap]])
        assert str(first).replace(hidden, "X") == str(second).replace(missing, "X")
        assert_hidden_absent(second, OTHERS)


# ---- using an entry: not yet, and unknown stays unknown --------------------------------


def moved(served: ServerApp, token: str, item: str) -> str:
    """The location the item is in, as the token that reads it sees it."""
    shown = command(served, "get", token, ref=item, **GRAPH)
    assert shown.status == 200, shown.json
    (edge,) = [e for e in shown.json["data"]["entity"]["edges"] if e["rel"] == "inv:located_in"]
    return str(edge["target"])


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_a_granted_action_writes_and_another_is_not_found(
    served: ServerApp, agent: str, reader: str, installation: dict[str, Any]
) -> None:
    ids = installation["ids"]
    chair, rack = ids["chair"], ids["rack"]
    args = {**GRAPH, "--param": [f"item={chair}", f"location={rack}"], "--reason": "test"}
    # a reader that is granted no action may not write; the token that is granted it does
    assert command(served, "do", reader, name="move_item", **args).status == 403
    done = command(served, "do", agent, name="move_item", **args)
    assert done.status == 200, done.json
    assert done.json["data"]["action"]["name"] == "move_item"
    assert done.json["data"]["entity_count"] == 1
    assert moved(served, reader, chair) == rack
    # the primitives stay refused: the token cannot write the item by hand
    by_hand = command(
        served, "update", agent, ref=chair, **GRAPH, **{"--set": ["name=Other"], "--reason": "x"}
    )
    assert by_hand.status == 403 and by_hand.error["code"] == "access.denied", by_hand.json
    # a dry run is decided the same way and writes nothing
    back = {**args, "--param": [f"item={chair}", f"location={ids['shelf']}"]}
    dry = command(served, "do", agent, name="move_item", **back, **{"--dry-run": True})
    assert dry.status == 200 and dry.json["data"]["dry_run"] is True, dry.json
    assert moved(served, reader, chair) == rack
    # an action the grant does not name does not exist for the token
    unnamed = command(served, "do", agent, name="retire_item", **args)
    absent = command(served, "do", agent, name="retire_itemm", **args)
    assert unnamed.status == absent.status == 404
    assert unnamed.error["code"] == absent.error["code"] == "action.not_found"
    for reply in (unnamed, absent):
        assert reply.error["details"]["did_you_mean"] in ([], ["move_item"])
    assert_hidden_absent(unnamed.json["error"]["details"]["did_you_mean"], OTHERS)


@pytest.mark.doc_claim("security_model.catalog_actions")
def test_a_reader_with_catalog_actions_writes_through_these_actions_only(
    served: ServerApp, mover: str, installation: dict[str, Any]
) -> None:
    ids = installation["ids"]
    chair, rack = ids["chair"], ids["rack"]
    args = {**GRAPH, "--param": [f"item={chair}", f"location={rack}"], "--reason": "test"}
    done = command(served, "do", mover, name="move_item", **args)
    assert done.status == 200, done.json
    assert moved(served, mover, chair) == rack  # it reads what it wrote
    # the primitives are what a reader's grant says: no write by hand
    by_hand = command(
        served, "update", mover, ref=chair, **GRAPH, **{"--set": ["name=Other"], "--reason": "x"}
    )
    assert by_hand.status == 403 and by_hand.error["code"] == "access.denied", by_hand.json
    # an action it is not granted is what it was for a reader: seen, not allowed, refused
    other = command(
        served, "do", mover, name="retire_item", **GRAPH, **{"--param": [f"item={chair}"]}
    )
    assert other.status == 403 and other.error["code"] == "access.denied", other.json
    shown = command(served, "describe/action", mover, name="retire_item", **GRAPH)
    assert shown.json["data"]["action"]["allowed"] is False
    assert (
        "allowed"
        not in command(served, "describe/action", mover, name="move_item", **GRAPH).json["data"][
            "action"
        ]
    )


GRAPH_POLICIES = {
    "type_limit": 'types:\n  "inv:Item": read\n',
    "term_rule": (
        "deny:\n  - {id: no_relocation, action: [write.link], type: [inv:Item],"
        " relation: [inv:located_in]}\n"
    ),
    "obligation": "obligations:\n  - {action: [write.*], max_batch: 1}\n",
}


@pytest.mark.doc_claim("security_model.catalog_actions")
@pytest.mark.parametrize("kind", sorted(GRAPH_POLICIES))
def test_a_deny_rule_of_the_graph_refuses_a_granted_action(
    run_cli: CliRunner,
    served: ServerApp,
    agent: str,
    reader: str,
    installation: dict[str, Any],
    tmp_path: Path,
    kind: str,
) -> None:
    """A policy applied after the grant is decided again for the next call: whatever denies a
    write of the action (a type limit, a rule on a relation, an obligation the two ops of
    ``move_item`` break) refuses the whole action, and the item stays where it was."""
    ids = installation["ids"]
    text = "schema: graph_cli.policy/1\nid: graph\n" + GRAPH_POLICIES[kind]
    policy = write_policy(tmp_path / "graph_policies", f"{kind}.yaml", text)
    applied = admin(
        run_cli, "policy", "apply", str(policy), "--graph", "example_graph", "--reason", kind
    )
    assert applied.code == 0, applied.envelope
    args = {
        **GRAPH,
        "--param": [f"item={ids['chair']}", f"location={ids['rack']}"],
        "--reason": "t",
    }
    refused = command(served, "do", agent, name="move_item", **args)
    assert refused.status == 403 and refused.error["code"] in (
        "access.denied",
        "access.batch_too_large",
    ), refused.json
    assert moved(served, reader, ids["chair"]) == ids["shelf"]
    # a dry run is refused the same way and the call of the tool too
    dry = command(served, "do", agent, name="move_item", **args, **{"--dry-run": True})
    assert dry.status == 403, dry.json
    called = command(
        served, "call", agent, tool="do_move_item",
        input={"item": ids["chair"], "location": ids["rack"], "_reason": "t"}, **GRAPH,
    )  # fmt: skip
    assert called.status == 403, called.json
    assert moved(served, reader, ids["chair"]) == ids["shelf"]


@pytest.mark.doc_claim("security_model.catalog_grants")
def test_a_granted_function_and_view_run_and_an_unnamed_one_is_not_found(
    served: ServerApp, agent: str, reader: str
) -> None:
    ran = command(served, "run", agent, name="items_by_condition", **GRAPH,
                  **{"--param": ["condition=damaged"]})  # fmt: skip
    assert ran.status == 200, ran.json
    assert ran.json["data"]["function"]["name"] == "items_by_condition"
    assert ran.json["data"]["columns"] == ["item", "condition"]
    shown = command(served, "view", agent, name="item_catalog", **GRAPH)
    assert shown.status == 200, shown.json
    assert shown.json["data"]["view"]["name"] == "item_catalog"
    # what the reader gets of the same call is the same
    again = command(served, "run", reader, name="items_by_condition", **GRAPH,
                    **{"--param": ["condition=damaged"]})  # fmt: skip
    assert again.json["data"] == ran.json["data"]
    for path, granted, unnamed, missing, code in (
        ("run", "items_by_condition", "items_in_location", "items_in_nowhere", "function"),
        ("view", "item_catalog", "location_overview", "location_overviewz", "view"),
    ):
        first = command(served, path, agent, name=unnamed, **GRAPH)
        second = command(served, path, agent, name=missing, **GRAPH)
        assert first.status == second.status == 404, (first.json, second.json)
        assert first.error["code"] == second.error["code"] == f"{code}.not_found"
        one, two = first.json, second.json
        for body in (one, two):
            assert body["error"]["details"].pop("did_you_mean") in ([], [granted])
        assert str(one).replace(unnamed, "X") == str(two).replace(missing, "X")
        assert_hidden_absent(two, OTHERS)


@pytest.mark.doc_claim("security_model.catalog_grants")
def test_the_primitives_and_the_card_stay_refused_whatever_is_asked(
    served: ServerApp, agent: str
) -> None:
    """Granting entries opens no primitive: ``get``, ``find``, ``query``, the card of ``view``,
    and ``view --name card`` are the same ``access.denied`` as before, for an id that exists or
    not, with a default view or not."""
    missing = "0192f0aa-0000-7000-8000-000000000000"
    for path, args in (
        ("get", {"ref": missing}),
        ("find", {"--type": ["inv:Item"]}),
        ("query", {"text": "MATCH (i:Item) RETURN i.name"}),
        ("view", {"ref": missing}),
        ("view", {"ref": missing, "name": "card"}),
        ("view", {"ref": "abcdef12"}),
        ("neighbors", {"ref": missing}),
        ("lookup", {"text": ["x"]}),
        ("lookup", {"text": ["x"], "--for": "part_of"}),
        ("lookup", {"text": ["x"], "--match": "prefix", "--for": "items_by_condition.condition"}),
        ("search", {"text": "x"}),
    ):
        reply = command(served, path, agent, **args, **GRAPH)
        assert reply.status == 403 and reply.error["code"] == "access.denied", (path, reply.json)
        assert reply.error["details"]["action"].startswith("read.")
        assert reply.error["details"]["resource"].count("/") == 0  # the graph, never an entry
    first = command(served, "view", agent, ref=missing, **GRAPH)
    second = command(served, "view", agent, ref="abcdef12", **GRAPH)
    assert first.json["error"] == second.json["error"]


def test_call_of_an_unnamed_tool_is_tool_not_found(served: ServerApp, agent: str) -> None:
    for tool in ("run_items_in_location", "do_retire_item", "view_location_overview"):
        reply = command(served, "call", agent, tool=tool, input={}, **GRAPH)
        assert reply.status == 404 and reply.error["code"] == "tool.not_found", reply.json
        assert_hidden_absent(reply.json["error"]["details"].get("did_you_mean", []), OTHERS)


def test_call_runs_the_granted_function_view_and_action_through_the_manifest(
    served: ServerApp, agent: str, installation: dict[str, Any]
) -> None:
    ran = command(served, "call", agent, tool="run_items_by_condition",
                  input={"condition": "damaged"}, **GRAPH)  # fmt: skip
    assert ran.status == 200, ran.json
    assert ran.json["data"]["function"]["name"] == "items_by_condition"
    assert ran.json["data"]["page"]["total"] == 0  # the graph holds no item
    shown = command(served, "call", agent, tool="view_item_catalog", input={}, **GRAPH)
    assert shown.status == 200, shown.json
    assert shown.json["data"]["view"]["name"] == "item_catalog"
    # a tool of the manifest runs through its own hash too
    tools = {
        t["name"]: t
        for t in command(served, "describe/tools", agent, **GRAPH).json["data"]["tools"]
    }
    stamped = command(
        served, "call", agent, tool="run_items_by_condition", input={"condition": "damaged"},
        **{"--expect-hash": tools["run_items_by_condition"]["hash"]}, **GRAPH,
    )  # fmt: skip
    assert stamped.status == 200, stamped.json
    # the action is a tool of the manifest and runs through ``call`` with its grant
    assert "do_move_item" in tools
    ids = installation["ids"]
    wanted = {"item": ids["chair"], "location": ids["rack"], "_reason": "through call"}
    written = command(served, "call", agent, tool="do_move_item", input=wanted, **GRAPH)
    assert written.status == 200, written.json
    assert written.json["data"]["action"]["name"] == "move_item"
    again = command(
        served, "call", agent, tool="do_move_item", input={**wanted, "_dry_run": True}, **GRAPH
    )
    assert again.status == 200 and again.json["data"]["dry_run"] is True, again.json
    # a tool the grant does not name is unknown
    refused = command(served, "call", agent, tool="do_retire_item", input={}, **GRAPH)
    assert refused.status == 404 and refused.error["code"] == "tool.not_found"


def test_the_lookup_tool_is_listed_only_when_a_tool_has_a_reference_input(
    served: ServerApp, agent: str
) -> None:
    """The entries of this role take no reference, so the manifest has no lookup tool and a call
    of it is an unknown tool, whatever it asks for."""
    tools = command(served, "describe/tools", agent, **GRAPH).json["data"]["tools"]
    assert "lookup" not in {t["name"] for t in tools}
    given = {"text": "x", "for": "run_items_by_condition.condition"}
    reply = command(served, "call", agent, tool="lookup", input=given, **GRAPH)
    assert reply.status == 404 and reply.error["code"] == "tool.not_found", reply.json
    assert reply.error["details"]["did_you_mean"] in ([], ["run_items_by_condition"])
    assert_hidden_absent(reply.json["error"]["details"], OTHERS)
    # the way a name is looked up for a parameter that is no reference: a mistake of the call
    asked = command(
        served, "lookup", agent, text=["x"], **{"--for": "items_by_condition.condition"}, **GRAPH
    )
    assert asked.status == 400, asked.json
    assert asked.error["code"] == "usage.invalid_argument"


# ---- tokens and enrollment ----------------------------------------------------------------


def test_token_creation_and_enrollment_count_a_catalog_grant_as_granted(
    run_cli: CliRunner, served: ServerApp, installation: dict[str, Any]
) -> None:
    created = admin(
        run_cli, "token", "create", "agent_two", "--project", "example_project",
        "--role", "catalog_agent",
    )  # fmt: skip
    assert created.code == 0, created.envelope
    assert created.warning_codes == []
    idle = admin(
        run_cli, "token", "create", "idle_one", "--project", "example_project", "--role", "idle"
    )
    assert idle.warning_codes == ["admin.token_not_granted"]
    added = admin(run_cli, "token", "role", "add", "idle_one", "catalog_agent")
    assert added.code == 0 and added.warning_codes == []  # the grant makes the token granted
    code = admin(
        run_cli, "enroll", "create", "--project", "example_project", "--role", "catalog_agent"
    )
    assert code.code == 0, code.envelope
    assert code.warning_codes == []
    checked = call(served, "POST", "/v1/enroll", body={"code": code.data["code"], "check": True})
    assert checked.status == 200, checked.json
    assert checked.json["data"]["enrollment"]["graphs"] == ["example_graph"]
    # a scope that takes the graph away (no project: none) leaves the grant dead, as for a reader
    dead = admin(run_cli, "token", "create", "dead_one", "--role", "catalog_agent")
    assert dead.warning_codes == ["admin.token_not_granted"]
    elsewhere = admin(
        run_cli, "enroll", "create", "--project", "nowhere", "--role", "catalog_agent"
    )
    assert elsewhere.warning_codes == ["admin.token_not_granted"]
    nothing = admin(run_cli, "enroll", "create", "--project", "example_project", "--role", "idle")
    assert nothing.warning_codes == ["admin.token_not_granted"]


def test_policy_explain_shows_the_level_the_names_and_the_decision_on_an_entry(
    run_cli: CliRunner, installation: dict[str, Any]
) -> None:
    def explain(*args: str) -> Any:
        found = admin(
            run_cli, "policy", "explain", "--graph", "example_graph",
            "--project", "example_project", *args,
        )  # fmt: skip
        assert found.code == 0, found.envelope
        return found

    shown = explain("--role", "catalog_agent")
    assert shown.data["level"] == "catalog" and shown.data["withheld"] == []
    listed = shown.data["catalog"]
    assert {k: [e["name"] for e in listed[k]] for k in ("functions", "actions", "views")} == {
        "functions": ["items_by_condition"],
        "actions": ["move_item"],
        "views": ["item_catalog"],
    }
    assert all(
        e["published"] and e["hash"] for k in ("functions", "actions", "views") for e in listed[k]
    )
    assert (listed["catalog_only"], listed["reader_rules"]) == (True, [])
    for flag, name, rule in (
        ("--function", "items_by_condition", "grant.1.functions"),
        ("--view", "item_catalog", "grant.1.views"),
        ("--entry-action", "move_item", "grant.1.actions"),
    ):
        yes = explain("--role", "catalog_agent", flag, name).data
        (decision,) = yes["decisions"]
        assert (decision["effect"], decision["rule"]) == ("allow", rule), (flag, yes)
        assert yes["resource"].endswith(f"/{flag.lstrip('-').replace('entry-', '')}:{name}")
        no = explain(
            "--role", "catalog_agent", flag, "retire_item" if flag != "--view" else "no_such_view"
        )
        (refused,) = no.data["decisions"]
        assert refused["effect"] == "deny" and refused["rule"] == "default.deny"
    # a reader's explanation says it is no catalog caller
    reading = explain("--role", "reader").data
    assert reading["level"] == "read" and "catalog" not in reading
    # the three filters exclude each other and the other questions
    bad = admin(
        run_cli, "policy", "explain", "--graph", "example_graph", "--role", "catalog_agent",
        "--function", "a_b", "--view", "c_d",
    )  # fmt: skip
    assert bad.code == 2 and bad.error["code"] == "usage.invalid_argument"
    clash = admin(
        run_cli, "policy", "explain", "--graph", "example_graph", "--role", "catalog_agent",
        "--function", "a_b", "--type", "inv:Item",
    )  # fmt: skip
    assert clash.code == 2 and clash.error["details"]["argument"] == "type"
    wrong = admin(
        run_cli, "policy", "explain", "--graph", "example_graph", "--role", "catalog_agent",
        "--function", "Not A Name",
    )  # fmt: skip
    assert wrong.code == 2 and wrong.error["details"]["argument"] == "function"


def audit_text(root: Path) -> str:
    return "".join(
        segment.read_text() for segment in sorted((root / "audit").rglob("*.jsonl"))
    ) + "".join(segment.read_text() for segment in sorted((root / "graphs").rglob("audit/*.jsonl")))


def test_the_audit_names_no_entry_the_catalog_lacks(
    run_cli: CliRunner, served: ServerApp, agent: str, installation: dict[str, Any]
) -> None:
    """An unnamed entry is unknown, so asking for it records nothing; what a named one does is
    audited, a write with the decisions of its grant. Neither names an entry outside the catalog."""
    ids = installation["ids"]
    given = {"--param": [f"item={ids['chair']}", f"location={ids['rack']}"], "--reason": "test"}
    command(served, "do", agent, name="retire_item", **GRAPH, **given)
    done = command(served, "do", agent, name="move_item", **GRAPH, **given)
    assert done.status == 200, done.json
    text = audit_text(installation["root"])
    assert "write.action" in text and "grant.1.actions" in text
    for name in OTHERS:
        assert name not in text
    # the events with the entry decisions verify
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0, verified.data
