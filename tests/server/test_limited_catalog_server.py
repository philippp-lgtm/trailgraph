"""C: functions, actions and the graph skill of a limited token through the server in-process
(type read limits plan V7).

The role ``reporting`` reads items of ``example_graph`` and nothing else, ``auditor`` reads
everything. Token A holds the first, token B both. A function or action that names a hidden term
is as unknown to A as one that was never published; the skill the server renders for A names
nothing hidden and carries the digest of what A sees, which the server compares when A's client
claims an installed copy.
"""

import base64
import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import create_graph
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
  reporting: {description: "Reporting: items only"}
  auditor: {description: "Auditors: everything"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [inv:Item]
  - entrypoint: server
    principal: {role: auditor}
    graphs: [example_graph]
    level: read
scopes:
  example_project: {example_graph: read}
no_project: {"*": none}
"""
GRAPH = {"--graph": "example_graph"}
POINTER = {"graphs": ["example_graph"], "default": "example_graph"}
HIDDEN = ("Location", "located_in", "Category", "in_category", "items_in_location", "move_item")
SKILL = FIXTURES / "skills" / "inventory_limited" / "graph-example-graph"
NOT_THERE = "items_in_nowhere"


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    published = admin(
        run_cli,
        *("graph", "functions", "publish", "example_graph"),
        str(FIXTURES / "functions" / "inventory" / "valid"),
    )
    assert published.code == 0, published.envelope
    actions = FIXTURES / "actions" / "inventory" / "valid"
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(actions))
    assert published.code == 0, published.envelope
    folder = tmp_path / "skill" / "graph-example-graph"
    shutil.copytree(SKILL, folder)
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "read limits")
    assert applied.code == 0, applied.envelope
    tokens = {
        "a": new_token(run_cli, "app_a", "--project", "example_project", "--role", "reporting"),
        "b": new_token(
            *(run_cli, "app_b", "--project", "example_project"),
            *("--role", "reporting", "--role", "auditor"),
        ),
    }
    return {"env": isolated_env, "tokens": tokens}


@pytest.fixture
def served(installation: dict[str, Any], tmp_path: Path) -> ServerApp:
    return ServerApp(
        Settings.load(installation["env"], tmp_path), log=io.StringIO(), enroll=Enrollment
    )


def command(server: ServerApp, path: str, token: str, **args: Any) -> Any:
    return call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})


def skill(server: ServerApp, token: str) -> dict[str, Any]:
    reply = call(server, "POST", "/v1/graphs/example_graph/skill", token=token, body={})
    assert reply.status == 200, reply.json
    data: dict[str, Any] = reply.json["data"]["skill"]
    return data


def listed(reply: Any, key: str) -> list[str]:
    return [i["name"] for i in reply.json["data"][key]]


def same_but_for(hidden: Any, missing: Any, swap: tuple[str, str]) -> None:
    """Two replies are the same but for the name asked (and the suggestions that follow it)."""
    assert hidden.status == missing.status
    assert hidden.error["code"] == missing.error["code"]
    first, second = hidden.json, missing.json
    for body in (first, second):
        body["error"]["details"].pop("did_you_mean", None)
    assert str(first).replace(*swap) == str(second)


def test_a_limited_token_lists_and_runs_the_functions_that_fit_and_no_others(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    a, b = installation["tokens"]["a"], installation["tokens"]["b"]
    assert listed(command(served, "describe/functions", b, **GRAPH), "functions") == [
        "items_by_condition",
        "items_in_location",
    ]
    assert listed(command(served, "describe/functions", a, **GRAPH), "functions") == [
        "items_by_condition"
    ]
    modules = command(served, "describe/modules", a, **GRAPH).json["data"]["modules"]
    assert [m["functions"] for m in modules if m["id"] == "inventory"] == [1]
    hidden = command(served, "describe/function", a, name="items_in_location", **GRAPH)
    missing = command(served, "describe/function", a, name=NOT_THERE, **GRAPH)
    assert hidden.error["code"] == "function.not_found"
    same_but_for(hidden, missing, ("items_in_location", NOT_THERE))
    assert command(served, "describe/function", b, name="items_in_location", **GRAPH).status == 200
    run = command(
        served, "run", a, name="items_in_location", **{"--param": ["location=x"]}, **GRAPH
    )
    absent = command(served, "run", a, name=NOT_THERE, **{"--param": ["location=x"]}, **GRAPH)
    same_but_for(run, absent, ("items_in_location", NOT_THERE))
    ran = command(
        served, "run", a, name="items_by_condition", **{"--param": ["condition=worn"]}, **GRAPH
    )
    assert ran.status == 200, ran.json


def test_the_skill_is_rendered_per_token_and_carries_the_digest_of_the_visible_set(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    a, b = installation["tokens"]["a"], installation["tokens"]["b"]
    limited, full = skill(served, a), skill(served, b)
    text_a = b"".join(base64.b64decode(c) for c in limited["files"].values()).decode()
    text_b = b"".join(base64.b64decode(c) for c in full["files"].values()).decode()
    assert [name for name in HIDDEN if name in text_a] == []
    assert "items_in_location" in text_b  # (neither token writes: no action is listed)
    assert "Named functions:" in text_a and "items_by_condition" not in text_a  # not featured
    assert limited["source"]["visible"].startswith("sha256:")
    assert "visible" not in full["source"]
    # what the render shows of the catalogs: the whole catalog's digest for B (it sees all), the
    # one of what A sees for A (hidden entries never enter it)
    assert limited["source"]["featured"] != full["source"]["featured"]


def read_with(server: ServerApp, token: str, claim: str) -> Any:
    return call(
        server,
        "POST",
        "/v1/commands/find",
        token=token,
        body={"args": {"--type": ["inv:Item"]}, "pointer": POINTER},
        headers={"Graph-Cli-Skills": claim},
    )


def test_a_claim_of_another_visible_set_is_stale(
    served: ServerApp, installation: dict[str, Any]
) -> None:
    a, b = installation["tokens"]["a"], installation["tokens"]["b"]
    listing = command(served, "graphs", a).json["data"]["items"]
    (graph,) = [g["id"] for g in listing if g["name"] == "example_graph"]
    source = skill(served, a)["source"]
    base = f"graph={graph};hash={source['hash']};bundle=inventory;version=1.0.0"
    mine = f"{base};visible={source['visible']};scope=user"
    assert read_with(served, a, mine).json["warnings"] == []
    # a copy rendered for another set, or for a reader without a limit, is stale for A
    other = f"{base};visible=sha256:{'0' * 64};scope=user"
    assert [w["code"] for w in read_with(served, a, other).json["warnings"]] == ["skills.stale"]
    unlimited = f"{base};scope=user"
    assert [w["code"] for w in read_with(served, a, unlimited).json["warnings"]] == ["skills.stale"]
    # B sees everything: its copy has no digest, and A's copy is stale for it
    assert read_with(served, b, unlimited).json["warnings"] == []
    assert [w["code"] for w in read_with(served, b, mine).json["warnings"]] == ["skills.stale"]


# ---- what the render shows of the catalogs, for a limited token (graph start plan S6) ----------


def republish(run_cli: CliRunner, tmp_path: Path, name: str, **featured: bool) -> None:
    """Publish the functions with ``featured`` set per function name (others as they are)."""
    folder = tmp_path / f"functions_{name}"
    shutil.copytree(FIXTURES / "functions" / "inventory" / "valid", folder)
    for function, value in featured.items():
        path = folder / f"{function}.yaml"
        text = path.read_text()
        text = text.replace("featured: true\n", "").replace("featured: false\n", "")
        path.write_text(text.replace("\nparams:", f"\nfeatured: {str(value).lower()}\nparams:", 1))
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


def test_the_featured_claim_of_a_limited_token_covers_only_what_it_sees(
    run_cli: CliRunner, served: ServerApp, installation: dict[str, Any], tmp_path: Path
) -> None:
    """A featured entry the token cannot see changes nothing for it: its copy stays fresh and no
    digest of hidden content reaches it; a featured entry it sees makes the copy stale. The
    digest is the one the skill carries when the server renders it."""
    a, b = installation["tokens"]["a"], installation["tokens"]["b"]
    listing = command(served, "graphs", a).json["data"]["items"]
    (graph,) = [g["id"] for g in listing if g["name"] == "example_graph"]

    def claim(token: str) -> str:
        source = skill(served, token)["source"]
        base = f"graph={graph};hash={source['hash']};bundle=inventory;version=1.0.0"
        seen = f";visible={source['visible']}" if "visible" in source else ""
        return f"{base}{seen};featured={source['featured']};scope=user"

    mine = claim(a)
    whole_before = skill(served, b)["source"]["featured"]
    assert read_with(served, a, mine).json["warnings"] == []  # fresh right after install
    # the function A cannot see (it names a location) is unfeatured: nothing changes for A...
    republish(run_cli, tmp_path, "hidden", items_in_location=False)
    assert skill(served, b)["source"]["featured"] != whole_before  # the whole catalog moved
    assert read_with(served, a, mine).json["warnings"] == []
    assert claim(a) == mine
    # ...and one A can see is featured: A's copy is stale, B's too
    republish(run_cli, tmp_path, "seen", items_in_location=False, items_by_condition=True)
    moved = read_with(served, a, mine)
    assert [w["code"] for w in moved.json["warnings"]] == ["skills.stale"]
    (entry,) = moved.json["warnings"][0]["details"]["skills"]
    whole_now = skill(served, b)["source"]["featured"]
    shown = {entry["installed"]["featured"], entry["current"]["featured"]}
    assert shown.isdisjoint({whole_before, whole_now})  # per-reader values, not the whole catalog's
    assert read_with(served, a, claim(a)).json["warnings"] == []  # fresh again after a refresh
