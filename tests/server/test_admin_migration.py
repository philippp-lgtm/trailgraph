"""S: a release with a data migration through the admin API and the remote mode of graph-admin
(migration plan M3, M10).

In-process: the release through uploads (the version directory carries ``migration.yaml``), the
dry run asked twice, a release whose answer was lost and is run again (a clean no-op, never a
second migration), an interrupted release that the app API shows as incomplete and the admin API
finishes. As processes: the same over HTTP, with a proxy that loses the answer of a request the
server has applied; the client says ``remote.outcome_unknown`` and running the release again is
safe, and a dry run is retried once.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, CliRunner
from support import launcher
from support.admin import data_root, init, write_documents
from support.admin_api import command, served, upload_tree
from support.admin_context import admin_context
from support.app import create_graph
from support.equivalence import home_env
from support.fixtures import ONTOLOGIES
from support.lossy_proxy import lossy_proxy
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY, call, grant_server, new_token
from support.stores import FaultSwitch

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.common.faults import CrashPoint
from graph_cli_admin.services_admin.release import release
from graph_cli_server.app import ServerApp

pytestmark = pytest.mark.slow

INVENTORY = ONTOLOGIES / "inventory"
MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""
SEED: list[dict[str, Any]] = [
    {"op": "create", "type": "inv:Location", "local": "room", "attrs": {"name": "R", "code": "R1"}},
    {"op": "create", "type": "inv:Category", "local": "tools", "attrs": {"name": "Tools"}},
    {
        "op": "create",
        "type": "inv:Item",
        "local": "hammer",
        "attrs": {"name": "Hammer", "condition": "worn"},
        "edges": [
            {"rel": "inv:located_in", "target": "@room"},
            {"rel": "inv:in_category", "target": "@tools"},
        ],
    },
]
RELEASE = "POST /v1/admin/commands/ontology/release HTTP/1.1"


def major(tmp_path: Path, name: str = "v2") -> Path:
    """The 2.0.0 version directory of the fixture with its migration file."""
    target = tmp_path / name
    shutil.copytree(INVENTORY / "2.0.0", target)
    (target / "migration.yaml").write_text(MIGRATION)
    return target


@pytest.fixture
def installation(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> dict[str, Any]:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", version="1.1.0")
    # before the server policy: a decision is verified against the policy then in effect
    write_documents(root, "example_graph", [{"ops": SEED}])
    grant_server(run_cli, tmp_path)
    tokens = {
        "ci": new_token(run_cli, "ci"),
        "root": new_token(run_cli, "root", "--scope", "admin"),
    }
    return {"root": root, "env": isolated_env, "tokens": tokens}


def release_args(app: ServerApp, token: str, source: Path, **options: Any) -> dict[str, Any]:
    return {"graph": "example_graph", "source": upload_tree(app, token, source), **options}


def head_seq(root: Path) -> int:
    catalog = FileCatalog(root)
    return catalog.file_store(catalog.resolve("example_graph").id).head().seq


def audit_text(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*.jsonl")) if "audit" in p.parts)


def migrate_events(root: Path) -> int:
    return sum(b'"maint.migrate"' in line for line in audit_text(root).splitlines())


def test_a_release_with_a_migration_through_the_admin_api(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    root_token, ci = installation["tokens"]["root"], installation["tokens"]["ci"]
    source = major(tmp_path)
    released = command(
        app,
        "ontology/release",
        root_token,
        args=release_args(app, root_token, source, **{"--reason": "rename name to label"}),
    )
    assert released.status == 200, released.json
    data = released.json["data"]
    assert (data["released"], data["pinned"]) == (True, True)
    block = data["migration"]
    assert block["file"] is True and block["completing"] is False
    assert block["migrated"]["entities"] == 1 and block["tx_seq"] > 0 and block["duration_s"] >= 0
    # the answer names no path of the server
    assert str(tmp_path) not in json.dumps(released.json)
    shown = command(app, "graph/show", root_token, args={"ref": "example_graph"})
    assert shown.json["data"]["ontology"]["version"] == "2.0.0"
    assert command(app, "audit/verify", root_token).status == 200
    # the app API reads the migrated data under the new names
    found = call(
        app,
        "POST",
        "/v1/commands/find",
        token=ci,
        body={"args": {"graph": "example_graph", "type": ["inv:Item"]}},
    )
    assert found.status == 200, found.json
    (item,) = found.json["data"]["items"]
    assert item["attrs"]["label"] == "Hammer" and item["attrs"]["condition"] == "good"
    assert not list(installation["root"].rglob("migration.json"))


def test_a_dry_run_asked_twice_answers_alike_and_writes_nothing(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    app = served(installation, tmp_path)
    token, root = installation["tokens"]["root"], installation["root"]
    source = major(tmp_path)
    before = (audit_text(root), head_seq(root))
    answers = []
    for _ in range(2):
        dry = command(
            app,
            "ontology/release",
            token,
            args=release_args(app, token, source, **{"--dry-run": True}),
        )
        assert dry.status == 200, dry.json
        answers.append(dry.json["data"])
    assert answers[0] == answers[1]
    assert answers[0]["dry_run"] is True and answers[0]["migration"]["entities"] == 1
    assert (audit_text(root), head_seq(root)) == before
    assert not list(root.rglob("migration.json"))
    shown = command(app, "graph/show", token, args={"ref": "example_graph"})
    assert shown.json["data"]["ontology"]["version"] == "1.1.0"


def test_a_release_whose_answer_was_lost_is_a_no_op_the_second_time(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """The release committed and the client never saw the answer: running the same release
    again changes nothing, in particular it migrates nothing a second time."""
    app = served(installation, tmp_path)
    token, root = installation["tokens"]["root"], installation["root"]
    source = major(tmp_path)
    first = command(app, "ontology/release", token, args=release_args(app, token, source))
    assert first.status == 200, first.json  # the answer is dropped here
    sealed = (head_seq(root), migrate_events(root))
    again = command(app, "ontology/release", token, args=release_args(app, token, source))
    assert again.status == 200, again.json
    data = again.json["data"]
    assert (data["released"], data["pinned"]) == (False, False)
    assert "migration" not in data or "tx_seq" not in data["migration"]
    assert (head_seq(root), migrate_events(root)) == sealed == (head_seq(root), 1)
    verified = command(app, "audit/verify", token)
    assert verified.status == 200, verified.json["error"]["details"]


def test_an_interrupted_release_is_incomplete_for_the_app_and_finished_by_the_admin_api(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    """A crash between the migration tx and the pin switch: the app API reads under the new
    names and warns, refuses writes (a retryable 503); the admin API finishes the release."""
    app = served(installation, tmp_path)
    token, ci, root = (
        installation["tokens"]["root"],
        installation["tokens"]["ci"],
        installation["root"],
    )
    source = major(tmp_path)
    faults = FaultSwitch()
    faults.armed.add(CrashPoint.MANIFEST_BEFORE_RENAME)
    with pytest.raises(GraphCliError):
        release(
            admin_context(root, faults),
            graph_ref="example_graph",
            source=source,
            module=None,
            reason="example reason",
        )
    found = call(
        app,
        "POST",
        "/v1/commands/find",
        token=ci,
        body={"args": {"graph": "example_graph", "type": ["inv:Item"]}},
    )
    assert found.status == 200, found.json
    assert "label" in found.json["data"]["items"][0]["attrs"]
    assert [w["code"] for w in found.json["warnings"]] == ["graph.migration_incomplete"]
    created = call(
        app,
        "POST",
        "/v1/commands/create",
        token=ci,
        body={
            "args": {
                "graph": "example_graph",
                "type": "inv:Location",
                "set": ["name=Annex", "code=R2"],
            }
        },
    )
    assert created.status == 503 and created.error["code"] == "write.migration_incomplete"
    assert "Retry-After" in created.headers
    finished = command(app, "ontology/release", token, args=release_args(app, token, source))
    assert finished.status == 200, finished.json
    data = finished.json["data"]
    assert data["pinned"] is True and data["migration"]["completing"] is True
    again = call(
        app,
        "POST",
        "/v1/commands/find",
        token=ci,
        body={"args": {"graph": "example_graph", "type": ["inv:Item"]}},
    )
    assert again.json["warnings"] == []
    created = call(
        app,
        "POST",
        "/v1/commands/create",
        token=ci,
        body={
            "args": {
                "graph": "example_graph",
                "type": "inv:Location",
                "set": ["name=Annex", "code=R2"],
            }
        },
    )
    assert created.status == 200, created.json
    assert command(app, "audit/verify", token).status == 200


def test_the_server_builds_one_name_map_for_all_its_requests(
    installation: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request builds its read service anew; the name map of a graph at its pin outlives it."""
    from graph_cli.services import names

    app = served(installation, tmp_path)
    token, ci = installation["tokens"]["root"], installation["tokens"]["ci"]
    released = command(
        app, "ontology/release", token, args=release_args(app, token, major(tmp_path))
    )
    assert released.status == 200, released.json
    built: list[str] = []
    original = names.NameMaps._build

    def counting(self: names.NameMaps, entry: Any) -> Any:
        built.append(entry.name)
        return original(self, entry)

    monkeypatch.setattr(names.NameMaps, "_build", counting)
    found = call(
        app,
        "POST",
        "/v1/commands/find",
        token=ci,
        body={"args": {"graph": "example_graph", "type": ["inv:Item"]}},
    )
    assert found.status == 200, found.json
    ref = found.json["data"]["items"][0]["id"]
    for _ in range(3):
        history = call(
            app,
            "POST",
            "/v1/commands/history",
            token=ci,
            body={"args": {"graph": "example_graph", "ref": ref}},
        )
        assert history.status == 200, history.json
        assert any("migration" in r for r in history.json["data"]["items"])
    assert built == ["example_graph"]
    assert len(app.name_maps) == 1


# ---- graph-admin in remote mode ---------------------------------------------------------


def admin_cli(env: dict[str, str], cwd: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = launcher.run(
        [str(BIN_DIR / "graph-admin"), *args, "--json"],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, json.loads(result.stdout)


def served_data(isolated_env: dict[str, str], tmp_path: Path) -> tuple[dict[str, str], Path, str]:
    """An installation with a 1.1.0 graph and data, its server policy and an admin token,
    as a server process will serve it: ``(env, data root, admin token)``."""
    owner = home_env(isolated_env, tmp_path / "server")
    policy = tmp_path / "server_policy.yaml"
    policy.write_text(SERVER_POLICY)
    root = data_root(owner)
    token = ""
    for args in (
        ("init",),
        ("graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")),
        None,  # the data, before the server policy: a decision is verified against its policy
        ("policy", "apply", str(policy), "--reason", "serve"),
        ("token", "create", "root", "--scope", "admin"),
    ):
        if args is None:
            write_documents(root, "example_graph", [{"ops": SEED}])
            continue
        code, document = admin_cli(owner, tmp_path, *args)
        assert code == 0, document
        token = document["data"].get("token", token)
    config = Path(owner["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "[server]\nadmin_api = true\n")
    return owner, root, token


def remote_env(
    isolated_env: dict[str, str], tmp_path: Path, port: int, token: str
) -> dict[str, str]:
    env = client_env(
        isolated_env,
        tmp_path / "client",
        port,
        None,
        extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n',
    )
    env["GRAPH_CLI_ADMIN_TOKEN"] = token
    return env


def test_a_release_with_a_migration_in_remote_mode(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    owner, root, token = served_data(isolated_env, tmp_path)
    source = major(tmp_path)
    with graph_server(owner, tmp_path) as server:
        env = remote_env(isolated_env, tmp_path, server.port, token)
        code, dry = admin_cli(
            env, tmp_path, "ontology", "release", "example_graph", str(source), "--dry-run"
        )
        assert code == 0, dry
        assert dry["data"]["dry_run"] is True and dry["data"]["migration"]["entities"] == 1
        code, done = admin_cli(
            env, tmp_path, "ontology", "release", "example_graph", str(source), "--reason", "rename"
        )
        assert code == 0, done
        assert done["data"]["migration"]["migrated"]["entities"] == 1
        code, verified = admin_cli(env, tmp_path, "audit", "verify")
        assert code == 0 and verified["data"]["counts"]["error"] == 0, verified
        code, shown = admin_cli(env, tmp_path, "graph", "show", "example_graph")
        assert shown["data"]["ontology"]["version"] == "2.0.0"
    assert not list(root.rglob("migration.json"))


def test_a_lost_answer_of_a_release_is_finished_by_running_it_again(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """The proxy loses the answer of the first release request after the server applied it:
    the client says ``remote.outcome_unknown`` with the command to run again, and running it
    changes nothing."""
    owner, root, token = served_data(isolated_env, tmp_path)
    source = major(tmp_path)
    with (
        graph_server(owner, tmp_path) as server,
        lossy_proxy(server.port, lambda line, n: line == RELEASE and n == 1) as proxy,
    ):
        env = remote_env(isolated_env, tmp_path, proxy.port, token)
        args = ("ontology", "release", "example_graph", str(source), "--reason", "rename")
        code, lost = admin_cli(env, tmp_path, *args)
        assert proxy.lost == [RELEASE]
        assert (code, lost["error"]["code"]) == (5, "remote.outcome_unknown"), lost
        assert "again" in lost["error"]["message"]
        assert lost["error"]["hint"].startswith("graph-admin ontology release example_graph")
        # the server did apply it
        assert head_seq(root) > 1 and migrate_events(root) == 1
        sealed = head_seq(root)
        code, rerun = admin_cli(env, tmp_path, *args)
        assert code == 0, rerun
        assert (rerun["data"]["released"], rerun["data"]["pinned"]) == (False, False)
        assert (head_seq(root), migrate_events(root)) == (sealed, 1)
        code, verified = admin_cli(env, tmp_path, "audit", "verify")
        assert code == 0 and verified["data"]["counts"]["error"] == 0, verified


def test_a_dry_run_whose_answer_is_lost_is_retried_once(
    isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """A dry run writes nothing, so the client sends it again when the answer is lost."""
    owner, root, token = served_data(isolated_env, tmp_path)
    source = major(tmp_path)
    before = (audit_text(root), head_seq(root))
    with (
        graph_server(owner, tmp_path) as server,
        lossy_proxy(server.port, lambda line, n: line == RELEASE and n == 1) as proxy,
    ):
        env = remote_env(isolated_env, tmp_path, proxy.port, token)
        code, dry = admin_cli(
            env, tmp_path, "ontology", "release", "example_graph", str(source), "--dry-run"
        )
    assert code == 0, dry["error"]
    assert proxy.lost == [RELEASE] and proxy.seen.count(RELEASE) == 2  # sent twice
    assert dry["data"]["dry_run"] is True and dry["data"]["migration"]["entities"] == 1
    assert (audit_text(root), head_seq(root)) == before
