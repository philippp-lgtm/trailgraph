"""S: the publish warnings of catalog grants through the admin API (graph-admin in remote mode):
the server runs the same checks, so a publish that removes or changes an entry a grant names
answers with the same warnings, and a dry run shows them without publishing (catalog callers
plan, steps 4 and 5), and of an ontology release that compiles a granted entry again."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.admin_api import command, served, upload_tree
from support.app import create_graph
from support.fixtures import FIXTURES
from support.moved_supplier import BARE_FUNCTION, INVENTORY, moved_source, write_folder
from support.policies import write_policy
from support.server import call

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  catalog_agent: {description: "Runs entries of example_graph"}
grants:
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [example_graph]
    level: catalog
    catalog:
      functions: [items_by_condition]
      actions: [move_item]
    reads: [inv:Item, inv:Location]
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    for kind, folder in (("functions", FUNCTIONS), ("actions", ACTIONS)):
        assert admin(run_cli, "graph", kind, "publish", "example_graph", str(folder)).code == 0
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "catalog caller").code == 0
    token = admin(run_cli, "token", "create", "root", "--scope", "admin").data["token"]
    return {"root": root, "env": isolated_env, "tokens": {"root": token}}


def codes(reply: Any) -> list[str]:
    assert reply.status == 200, reply.json
    return [w["code"] for w in reply.json["warnings"] if w["code"].startswith("catalog.")]


def test_a_remote_publish_warns_about_granted_entries(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    folder = tmp_path / "actions"
    shutil.copytree(ACTIONS, folder)
    path = "graph/actions/publish"

    def publish(**more: Any) -> Any:
        args = {"ref": "example_graph", "dir": upload_tree(app, root, folder), **more}
        return command(app, path, root, args=args)

    assert codes(publish()) == []  # the same files: nothing changes, nothing to warn about
    text = (folder / "retire_item.yaml").read_text()
    (folder / "retire_item.yaml").write_text(text.replace("summary: ", "summary: Please ", 1))
    assert codes(publish()) == []  # an entry no grant names
    moved = (folder / "move_item.yaml").read_text()
    (folder / "move_item.yaml").write_text(moved.replace("Store an item", "Put an item", 1))
    dry = publish(**{"--dry-run": True})
    assert codes(dry) == ["catalog.granted_entry_changed"]
    (changed,) = [w for w in dry.json["warnings"] if w["code"].startswith("catalog.")]
    assert changed["details"]["name"] == "move_item"
    assert changed["details"]["grants"] == [
        {"grant": "grant.1", "principal": {"role": ["catalog_agent"]}}
    ]
    assert dry.json["data"]["published"] is False
    (folder / "move_item.yaml").unlink()
    gone = publish()
    assert codes(gone) == ["catalog.granted_entry_removed"]
    assert gone.json["data"]["published"] is True


def test_a_remote_push_warns_about_granted_entries(
    run_cli: CliRunner, installation: dict[str, Any], tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    assert admin(run_cli, "graph", "pull", "example_graph", str(workspace)).code == 0
    (workspace / "functions" / "items_by_condition.yaml").unlink()
    app = served(installation, tmp_path)
    root = installation["tokens"]["root"]
    pushed = command(
        app,
        "graph/push",
        root,
        args={
            "ref": "example_graph",
            "dir": upload_tree(app, root, workspace),
            "--dry-run": True,
        },
    )
    assert codes(pushed) == ["catalog.granted_entry_removed"]


CLAIM_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  catalog_agent: {description: "Runs one function of example_graph"}
grants:
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [items_by_condition]}
    reads: [inv:Item, inv:Location]
scopes:
  p1: {example_graph: maintain}
no_project: {"*": none}
"""
WIDER = {
    "i.condition AS condition\n": "i.condition AS condition, i.quantity AS quantity\n",
    "examples:\n": "  - column: quantity\n    summary: Units.\nexamples:\n",
}


@pytest.mark.doc_claim("security_model.project_admin_ceiling")
def test_whoever_publishes_the_catalog_defines_what_a_granted_name_reads(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """A project admin that may maintain the graph republishes a granted function: the token of
    the grant then runs the new definition under the same name (and the publish warned), and
    loses the entry when it is removed."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    assert (
        admin(run_cli, "graph", "functions", "publish", "example_graph", str(FUNCTIONS)).code == 0
    )
    path = write_policy(tmp_path / "policies", "installation.yaml", CLAIM_POLICY)
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "claim").code == 0
    pa = admin(run_cli, "token", "create", "pa", "--scope", "admin", "--project", "p1")
    assert pa.code == 0, pa.envelope
    agent = admin(run_cli, "token", "create", "agent", "--project", "p1", "--role", "catalog_agent")
    assert agent.code == 0 and agent.warning_codes == [], agent.envelope
    app = served({"root": None, "env": isolated_env}, tmp_path)
    admin_token, agent_token = pa.data["token"], agent.data["token"]

    def columns() -> Any:
        ran = call(
            app, "POST", "/v1/commands/run", token=agent_token,
            body={"args": {"name": "items_by_condition", "--graph": "example_graph",
                           "--param": ["condition=damaged"]}},
        )  # fmt: skip
        return ran.json["data"]["columns"] if ran.status == 200 else ran.error["code"]

    assert columns() == ["item", "condition"]
    folder = tmp_path / "functions"
    shutil.copytree(FUNCTIONS, folder)
    text = (folder / "items_by_condition.yaml").read_text()
    for old, new in WIDER.items():
        assert old in text
        text = text.replace(old, new, 1)
    (folder / "items_by_condition.yaml").write_text(text)

    def publish() -> Any:
        args = {"ref": "example_graph", "dir": upload_tree(app, admin_token, folder)}
        return command(app, "graph/functions/publish", admin_token, args=args)

    changed = publish()
    assert codes(changed) == ["catalog.granted_entry_changed"]
    assert columns() == ["item", "condition", "quantity"]  # the grant names the entry, not its text
    (folder / "items_by_condition.yaml").unlink()
    assert codes(publish()) == ["catalog.granted_entry_removed"]
    assert columns() == "function.not_found"


RELEASE_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  catalog_agent: {description: "Runs one function of example_graph"}
grants:
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [suppliers_by_name]}
    reads: [inv:Item]
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""


def test_a_remote_release_warns_about_the_granted_entries_it_compiles_again(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """The ontology release through the admin API: the catalogs it republishes for a renamed
    term give a granted function another hash, and the answer says so, on a dry run as well."""
    init(run_cli, isolated_env)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")
    )
    assert created.code == 0, created.envelope
    functions = write_folder(tmp_path, "functions", {"suppliers_by_name": BARE_FUNCTION})
    assert (
        admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions)).code == 0
    )
    path = write_policy(tmp_path / "policies", "installation.yaml", RELEASE_POLICY)
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "release").code == 0
    root = admin(run_cli, "token", "create", "root", "--scope", "admin").data["token"]
    app = served({"env": isolated_env}, tmp_path)
    source = moved_source(tmp_path)

    def release(**more: Any) -> Any:
        args = {
            "graph": "example_graph",
            "source": upload_tree(app, root, source),
            "--module": "inventory",
            **more,
        }
        return command(app, "ontology/release", root, args=args)

    dry = release(**{"--dry-run": True})
    assert codes(dry) == ["catalog.granted_entry_changed"]
    (changed,) = [w for w in dry.json["warnings"] if w["code"].startswith("catalog.")]
    assert changed["details"]["name"] == "suppliers_by_name"
    assert changed["details"]["grants"] == [
        {"grant": "grant.1", "principal": {"role": ["catalog_agent"]}}
    ]
    assert dry.json["data"]["pinned"] is False
    done = release()
    assert codes(done) == ["catalog.granted_entry_changed"]
    assert done.json["data"]["pinned"] is True


PROJECT_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  any_agent: {description: "Any project"}
  own_agent: {description: "Of the project that publishes"}
  other_agent: {description: "Of another project"}
grants:
  - entrypoint: server
    principal: {role: any_agent}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [items_by_condition]}
    reads: [inv:Item]
  - entrypoint: server
    principal: {role: own_agent, project: p1}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [items_by_condition]}
    reads: [inv:Item]
  - entrypoint: server
    principal: {role: other_agent, project: other_project}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [items_by_condition]}
    reads: [inv:Item]
scopes:
  p1: {example_graph: maintain}
no_project: {"*": none}
"""


@pytest.mark.doc_claim("security_model.project_admin_ceiling")
def test_a_project_admin_is_told_which_grants_but_not_whose(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """The project admin may publish the catalog, not read the installation policy: the warning
    names the grants that name the entry (``grant.<n>``), and the principal (role, project) only
    of a grant bound to its own project. A maintainer sees every principal."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    assert (
        admin(run_cli, "graph", "functions", "publish", "example_graph", str(FUNCTIONS)).code == 0
    )
    path = write_policy(tmp_path / "policies", "installation.yaml", PROJECT_POLICY)
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "project admin").code == 0
    pa = admin(run_cli, "token", "create", "pa", "--scope", "admin", "--project", "p1")
    root = admin(run_cli, "token", "create", "root", "--scope", "admin")
    app = served({"root": None, "env": isolated_env}, tmp_path)
    folder = tmp_path / "functions"
    shutil.copytree(FUNCTIONS, folder)
    (folder / "items_by_condition.yaml").unlink()

    def warned(token: str) -> Any:
        args = {"ref": "example_graph", "dir": upload_tree(app, token, folder), "--dry-run": True}
        reply = command(app, "graph/functions/publish", token, args=args)
        assert codes(reply) == ["catalog.granted_entry_removed"], reply.json
        return next(w for w in reply.json["warnings"] if w["code"].startswith("catalog."))

    seen = warned(pa.data["token"])
    assert seen["details"]["grants"] == [
        {"grant": "grant.1"},
        {"grant": "grant.2", "principal": {"project": ["p1"], "role": ["own_agent"]}},
        {"grant": "grant.3"},
    ]
    for text in ("other_agent", "other_project", "any_agent"):
        assert text not in str(seen), text
    assert "own_agent" in seen["message"]
    # a push says no more than a publish
    workspace = tmp_path / "workspace"
    assert admin(run_cli, "graph", "pull", "example_graph", str(workspace)).code == 0
    (workspace / "functions" / "items_by_condition.yaml").unlink()
    pushed = command(
        app,
        "graph/push",
        pa.data["token"],
        args={
            "ref": "example_graph",
            "dir": upload_tree(app, pa.data["token"], workspace),
            "--dry-run": True,
        },
    )
    assert codes(pushed) == ["catalog.granted_entry_removed"], pushed.json
    (push_warning,) = [w for w in pushed.json["warnings"] if w["code"].startswith("catalog.")]
    assert push_warning["details"]["grants"] == seen["details"]["grants"]
    full = warned(root.data["token"])
    assert full["details"]["grants"] == [
        {"grant": "grant.1", "principal": {"role": ["any_agent"]}},
        {"grant": "grant.2", "principal": {"project": ["p1"], "role": ["own_agent"]}},
        {"grant": "grant.3", "principal": {"project": ["other_project"], "role": ["other_agent"]}},
    ]


RELEASE_PROJECT_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  own_agent: {description: "Of the project that releases"}
  other_agent: {description: "Of another project"}
grants:
  - entrypoint: server
    principal: {role: own_agent, project: p1}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [suppliers_by_name]}
    reads: [inv:Item]
  - entrypoint: server
    principal: {role: other_agent, project: other_project}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [suppliers_by_name]}
    reads: [inv:Item]
scopes:
  p1: {example_graph: maintain}
no_project: {"*": none}
"""


@pytest.mark.doc_claim("security_model.project_admin_ceiling")
def test_a_project_admin_cannot_make_a_release_that_compiles_a_granted_entry_again(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """A release that compiles the catalogs again (it has renames or a migration) needs
    ``maint.migrate``, which a project admin never holds: the warnings of such a release are only
    ever shown to a maintainer, so they carry the principals of every grant."""
    init(run_cli, isolated_env)
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")
    )
    assert created.code == 0, created.envelope
    functions = write_folder(tmp_path, "functions", {"suppliers_by_name": BARE_FUNCTION})
    assert (
        admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions)).code == 0
    )
    path = write_policy(tmp_path / "policies", "installation.yaml", RELEASE_PROJECT_POLICY)
    assert admin(run_cli, "policy", "apply", str(path), "--reason", "release").code == 0
    pa = admin(run_cli, "token", "create", "pa", "--scope", "admin", "--project", "p1")
    root = admin(run_cli, "token", "create", "root", "--scope", "admin")
    app = served({"env": isolated_env}, tmp_path)
    source = moved_source(tmp_path)

    def release(token: str) -> Any:
        args = {
            "graph": "example_graph",
            "source": upload_tree(app, token, source),
            "--module": "inventory",
            "--dry-run": True,
        }
        return command(app, "ontology/release", token, args=args)

    refused = release(pa.data["token"])
    assert refused.status == 403, refused.json
    assert "maint.migrate" in refused.error["message"]
    assert not [w for w in refused.json["warnings"] if w["code"].startswith("catalog.")]
    seen = release(root.data["token"])
    (warning,) = [w for w in seen.json["warnings"] if w["code"].startswith("catalog.")]
    assert [g["principal"]["role"] for g in warning["details"]["grants"]] == [
        ["own_agent"],
        ["other_agent"],
    ]
