"""C: write allow lists end to end through the server in-process (write allow lists plan W1-W11).

The roles ``importer`` and ``tagger`` carry ``writes`` on the inventory toy ontology; token A
holds the first, token B both. Requests go through ``ServerApp`` without a socket, the
installation is prepared with the real ``graph-admin``. What the lists hold goes through, what
they leave out is refused with the attribute or relation it names, a graph policy term deny
refuses an attribute to both tokens, ``graph do`` lists and runs only actions within the
rights, ``whoami`` says what was observed, the fine decisions are recorded in the audit events
and ``audit verify`` stays clean.
"""

import io
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.policies import write_policy
from support.server import call, new_token

from graph_cli_server.app import ServerApp, Settings
from graph_cli_server.enroll import Enrollment

pytestmark = pytest.mark.slow

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: items and locations in example_graph"}
  tagger: {description: "Taggers: the quantity of items in example_graph"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    writes:
      inv:Item:
        create: [name, quantity, condition]
        update: [condition]
        link: [inv:located_in, inv:part_of]
        transition: true
      inv:Location:
        create: true
        link: true
  - entrypoint: server
    principal: {role: tagger}
    graphs: [example_graph]
    level: read
    writes:
      inv:Item: {update: [quantity]}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
GRAPH = """\
schema: graph_cli.policy/1
id: graph
deny:
  - id: no_condition_updates
    role: [importer]
    action: [write.update]
    type: [inv:Item]
    attribute: [condition]
"""
MARK_DAMAGED = """\
schema: graph_cli.action/1
name: mark_damaged
summary: Record that an item is damaged.
use_when: An inspection finds an item damaged.
params:
  item:
    type: ref
    class: inv:Item
    summary: The item.
ops:
  - {op: update, id: $item, set: {condition: damaged}}
examples:
  - request: The chair came out of the inspection damaged.
"""
ITEM = "type:inv:Item"
GRAPH_ARGS = {"--graph": "example_graph"}
# What the lists grant (W2) and what whoami shows of them: the importer alone, then both roles.
IMPORTER_WRITES: dict[str, dict[str, Any]] = {
    "inv:Item": {
        "create": ["condition", "name", "quantity"],
        "update": ["condition"],
        "link": ["inv:located_in", "inv:part_of"],
        "transition": True,
    },
    "inv:Location": {"create": True, "link": True},
}
BOTH_WRITES: dict[str, dict[str, Any]] = {
    "inv:Item": {**IMPORTER_WRITES["inv:Item"], "update": ["condition", "quantity"]},
    "inv:Location": IMPORTER_WRITES["inv:Location"],
}


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    graph_id = create_graph(run_cli, "example_graph")
    # the actions and the data an importer links to are prepared under the built-in policy
    actions = tmp_path / "actions"
    shutil.copytree(FIXTURES / "actions" / "inventory" / "valid", actions)
    (actions / "mark_damaged.yaml").write_text(MARK_DAMAGED)
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(actions))
    assert published.code == 0, published.envelope
    env = {"GRAPH_CLI_GRAPH": "example_graph"}
    seeded: dict[str, str] = {}
    for name, cls, sets in (
        ("shelf", "inv:Location", ("name=Shelf", "code=S1")),
        ("rack", "inv:Location", ("name=Rack", "code=R1")),
        ("books", "inv:Category", ("name=Books",)),
    ):
        made = app(run_cli, "create", cls, *(f"--set={s}" for s in sets), env=env)
        assert made.code == 0, made.envelope
        seeded[name] = made.data["entities"][0]["id"]
    work = tmp_path / "policies"
    applied = admin(
        run_cli,
        *("policy", "apply", str(write_policy(work, "installation.yaml", INSTALLATION))),
        *("--reason", "write allow lists"),
    )
    assert applied.code == 0 and applied.warnings == [], applied.envelope
    tokens = {
        "a": new_token(run_cli, "app_a", "--project", "example_project", "--role", "importer"),
        "b": new_token(
            *(run_cli, "app_b", "--project", "example_project"),
            *("--role", "importer", "--role", "tagger"),
        ),
    }
    return {
        "root": root,
        "env": isolated_env,
        "tokens": tokens,
        "graph_id": graph_id,
        "seeded": seeded,
        "graph_policy": write_policy(work, "graph.yaml", GRAPH),  # applied in step 7
    }


def command(server: ServerApp, path: str, token: str, **body: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body=body)


def write(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return command(server, path, token, args={**GRAPH_ARGS, **args})


def refused(reply: Any, action: str, resource: str, rule: str | None = None) -> None:
    """``access.denied`` that names the action and the (fine) resource."""
    assert reply.status == 403 and reply.error["code"] == "access.denied", reply.json
    details = reply.error["details"]
    assert (details["action"], details["resource"]) == (action, resource), details
    if rule is not None:
        assert details["rule"] == rule, details


def entity(reply: Any) -> str:
    assert reply.status == 200, reply.json
    return str(reply.json["data"]["entities"][0]["id"])


def listed(server: ServerApp, token: str) -> list[str]:
    shown = command(server, "describe/actions", token, args=GRAPH_ARGS)
    assert shown.status == 200, shown.json
    return sorted(a["name"] for a in shown.json["data"]["actions"])


def whoami_writes(server: ServerApp, token: str) -> dict[str, Any]:
    seen = command(server, "whoami", token)
    assert seen.status == 200, seen.json
    (graph,) = seen.json["data"]["permissions"]["graphs"]
    assert graph["level"] == "read"
    writes = graph["writes"]
    assert isinstance(writes, dict)
    return writes


def events_of(root: Path, principal: str) -> list[dict[str, Any]]:
    """The audit events that one token's requests wrote: write intents in the graph's stream,
    refusals in the installation stream."""
    found: list[dict[str, Any]] = []
    for segment in sorted(p for p in root.rglob("*.jsonl") if "audit" in p.parts):
        for line in segment.read_text().splitlines():
            event = json.loads(line)
            if event["actor"]["principal"]["id"] == principal:
                found.append(event)
    return found


def decided(event: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(d["action"], d["resource"], d["effect"]) for d in event["authz"]["decisions"]}


@pytest.mark.doc_claim("security_model.write_allow_lists")
def test_write_allow_lists_decide_what_each_token_may_write(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    server = ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )
    root, graph_id = installation["root"], installation["graph_id"]
    shelf, rack, books = (installation["seeded"][n] for n in ("shelf", "rack", "books"))
    token_a, token_b = installation["tokens"]["a"], installation["tokens"]["b"]

    def resource(below: str) -> str:
        return f"graph:{graph_id}/{below}"

    # 1. A creates an item with listed attributes and an inline edge on a relation it may link
    created = write(
        server,
        "create",
        token_a,
        type="inv:Item",
        **{"--set": ["name=Chair", "quantity=2"], "--edge": [f"inv:located_in={shelf}"]},
    )
    chair = entity(created)
    parent = entity(
        write(
            server,
            "create",
            token_a,
            type="inv:Item",
            **{"--set": ["name=Crate", "condition=good"], "--edge": [f"inv:located_in={shelf}"]},
        )
    )
    # the create right on a type with a "true" list needs no attribute names
    assert write(
        server, "create", token_a, type="inv:Location", **{"--set": ["name=Hall", "code=H1"]}
    )

    # 2. an attribute the list leaves out is refused, naming the attribute
    unlisted = write(
        server,
        "create",
        token_a,
        type="inv:Item",
        **{
            "--set": ["name=Desk", "acquired_on=2026-01-01"],
            "--edge": [f"inv:located_in={shelf}"],
        },
    )
    refused(unlisted, "write.create", resource(f"{ITEM}/attr:acquired_on"))

    # 3. an inline edge needs the link right on its relation: the create right is not enough
    edge = write(
        server,
        "create",
        token_a,
        type="inv:Item",
        **{
            "--set": ["name=Lamp"],
            "--edge": [f"inv:located_in={shelf}", f"inv:in_category={books}"],
        },
    )
    refused(edge, "write.create", resource(f"{ITEM}/rel:inv:in_category"))

    # 4. A links the relation its list names; 5. unlink is not in the list
    linked = write(server, "link", token_a, ref=chair, rel="inv:part_of", target=parent)
    assert linked.status == 200, linked.json
    unlink = write(server, "unlink", token_a, ref=chair, rel="inv:part_of", target=parent)
    refused(unlink, "write.unlink", resource(ITEM), "default.deny")  # no unlink right at all
    not_linked = write(server, "link", token_a, ref=chair, rel="inv:in_category", target=books)
    refused(not_linked, "write.link", resource(f"{ITEM}/rel:inv:in_category"))

    # a transition and an update of the listed attribute go through
    moved = write(server, "transition", token_a, ref=chair, state="in_stock")
    assert moved.status == 200, moved.json
    assert (
        write(server, "update", token_a, ref=chair, **{"--set": ["condition=good"]}).status == 200
    )

    # 6. the tagger's attribute: B may update it, A may not, whatever else A holds
    quantity = {"--set": ["quantity=3"]}
    refused(
        write(server, "update", token_a, ref=chair, **quantity),
        "write.update",
        resource(f"{ITEM}/attr:quantity"),
    )
    assert write(server, "update", token_b, ref=chair, **quantity).status == 200
    # B has the rights of both roles, no more: the tagger's role grants no link
    refused(
        write(server, "link", token_b, ref=chair, rel="inv:in_category", target=books),
        "write.link",
        resource(f"{ITEM}/rel:inv:in_category"),
    )
    assert (
        write(server, "update", token_b, ref=chair, **{"--set": ["condition=worn"]}).status == 200
    )

    # the rights seen so far, before any term deny
    assert whoami_writes(server, token_a) == IMPORTER_WRITES
    assert whoami_writes(server, token_b) == BOTH_WRITES
    assert listed(server, token_a) == ["mark_damaged", "retire_item"]  # move_item unlinks
    assert listed(server, token_b) == ["mark_damaged", "retire_item"]
    marked = command(
        server,
        "do",
        token_a,
        args={**GRAPH_ARGS, "name": "mark_damaged", "--param": [f"item={chair}"]},
    )
    assert marked.status == 200, marked.json

    # 7. a graph policy denies one attribute: both tokens are refused it, the rest stays
    applied = admin(
        run_cli,
        *("policy", "apply", str(installation["graph_policy"])),
        *("--graph", "example_graph", "--reason", "no condition updates"),
    )
    assert applied.code == 0 and applied.warnings == [], applied.envelope
    condition = resource(f"{ITEM}/attr:condition")
    for token in (token_a, token_b):
        denied = write(server, "update", token, ref=chair, **{"--set": ["condition=good"]})
        refused(denied, "write.update", condition, "deny.no_condition_updates")
    assert write(server, "update", token_b, ref=chair, **{"--set": ["quantity=4"]}).status == 200
    # the deny names updates only: a create may still give the attribute
    assert entity(
        write(
            server,
            "create",
            token_a,
            type="inv:Item",
            **{"--set": ["name=Shelf unit", "condition=new"], "--edge": [f"inv:located_in={rack}"]},
        )
    )

    # 8. graph do lists and runs only what the rights allow; the others are refused, not hidden
    for token in (token_a, token_b):
        assert listed(server, token) == ["retire_item"]
        for hidden in ("mark_damaged", "move_item"):
            gone = command(
                server,
                "do",
                token,
                args={**GRAPH_ARGS, "name": hidden, "--param": [f"item={chair}"]},
            )
            assert gone.status == 403 and gone.error["code"] == "access.denied", gone.json
    retire = {"name": "retire_item", "--param": [f"item={chair}"], "--reason": "scrapped"}
    retired = command(server, "do", token_a, args={**GRAPH_ARGS, **retire})
    assert retired.status == 200, retired.json
    again = command(server, "get", token_a, args={"ref": chair, **GRAPH_ARGS})
    assert again.json["data"]["entity"]["attrs"]["status"] == "retired", again.json

    # 9. whoami says what was observed: the importer may update nothing now, B only the quantity
    after_a, after_b = whoami_writes(server, token_a), whoami_writes(server, token_b)
    assert after_a["inv:Item"]["update"] == []  # the deny empties the list, the verb stays shown
    assert after_b["inv:Item"]["update"] == ["quantity"]
    for shown in (after_a, after_b):
        assert {k: v for k, v in shown["inv:Item"].items() if k != "update"} == {
            k: v for k, v in IMPORTER_WRITES["inv:Item"].items() if k != "update"
        }
        assert shown["inv:Location"] == IMPORTER_WRITES["inv:Location"]
    explained = admin(
        run_cli,
        *("policy", "explain", "--graph", "example_graph", "--project", "example_project"),
        *("--role", "importer", "--role", "tagger", "--action", "write.update"),
        *("--type", "inv:Item", "--attribute", "condition"),
    )
    assert explained.code == 0, explained.envelope
    (decision,) = explained.data["decisions"]
    assert (decision["effect"], decision["rule"]) == ("deny", "deny.no_condition_updates")
    assert explained.data["type_decision"]["effect"] == "allow"
    allowed = admin(
        run_cli,
        *("policy", "explain", "--graph", "example_graph", "--project", "example_project"),
        *("--role", "importer", "--role", "tagger", "--action", "write.update"),
        *("--type", "inv:Item", "--attribute", "quantity"),
    )
    assert allowed.data["decisions"][0]["effect"] == "allow"

    # 10. the audit trail verifies
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0 and verified.data["findings"] == []

    # 11. the events of the writes carry the decisions on attributes and relations
    events = events_of(root, "token:app_a")
    denied_events = [e for e in events if e["result"]["status"] == "denied"]
    allowed_create = [
        e
        for e in events
        if e["result"]["status"] == "ok"
        and ("write.create", resource(f"{ITEM}/rel:inv:located_in"), "allow") in decided(e)
    ]
    assert allowed_create, "the intent of the first create records the relation it adds"
    first = decided(allowed_create[0])
    assert {
        ("write.create", resource(ITEM), "allow"),
        ("write.create", resource(f"{ITEM}/attr:name"), "allow"),
        ("write.create", resource(f"{ITEM}/attr:quantity"), "allow"),
    } <= first
    denied_events = [e for e in events if e["result"]["status"] == "denied"]
    denied_resources = {
        d["resource"]
        for e in denied_events
        for d in e["authz"]["decisions"]
        if d["effect"] == "deny"
    }
    assert denied_resources == {
        resource(f"{ITEM}/attr:acquired_on"),
        resource(f"{ITEM}/rel:inv:in_category"),
        resource(ITEM),  # unlink: no right on the type, and none on its relation
        resource(f"{ITEM}/rel:inv:part_of"),
        resource(f"{ITEM}/attr:quantity"),
        condition,
    }
    assert {r for r in denied_resources if "/attr:" not in r and "/rel:" not in r} == {
        resource(ITEM)
    }
    # the deny rule of the graph policy is the rule of the recorded denial
    by_rule = {
        d["rule"]
        for e in denied_events
        for d in e["authz"]["decisions"]
        if d["resource"] == condition and d["effect"] == "deny"
    }
    assert by_rule == {"deny.no_condition_updates"}
    link = [
        e
        for e in events
        if ("write.link", resource(f"{ITEM}/rel:inv:part_of"), "allow") in decided(e)
    ]
    assert link and all(
        ("write.link", resource(ITEM), "allow") in decided(e) for e in link
    )  # the decision on the type stays next to the one on the relation
    assert admin(run_cli, "audit", "verify").code == 0
