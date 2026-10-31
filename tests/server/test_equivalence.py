"""S: every command the server runs answers the same locally and remotely (B14.10).

Two installations are prepared alike on both toy ontologies: one used by
``graph`` directly, one owned by ``graph-server`` and used by ``graph`` in
remote mode from another home. The same scenario runs on both; the
envelopes (JSON) and the text renderings must be equal once ids, hashes and
times are numbered by first appearance and the actor's principal, client
and facet sources are left out: the only differences the server may make.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR
from support.app import create_args
from support.equivalence import differences, home_env, normalized
from support.files import synthetic_pdf, write_pdf, write_text
from support.fixtures import FIXTURES, ONTOLOGIES, TOY_ONTOLOGIES, compiled_model
from support.roles import Roles, lifecycle_path, load_roles
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY
from support.services import OpsFactory

from graph_cli.app_cli.commands import all_specs

pytestmark = pytest.mark.slow

Run = Callable[..., tuple[int, Any, str, str]]
GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"


def runner(env: dict[str, str], cwd: Path) -> Run:
    def run(*args: str, text: bool = False, input: str | None = None) -> tuple[int, Any, str, str]:
        argv = [str(BIN_DIR / "graph"), *args, *([] if text else ["--json"])]
        result = subprocess.run(
            argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=60, input=input
        )
        document = None if text else json.loads(result.stdout)
        return result.returncode, document, result.stdout, result.stderr

    return run


def functions_step(ontology: str) -> tuple[tuple[str, ...], ...]:
    """Publish the fixture functions of the ontology, if it has some (the others have none)."""
    folder = FIXTURES / "functions" / ontology / "valid"
    if not folder.is_dir():
        return ()
    return (("graph", "functions", "publish", "example_graph", str(folder)),)


def prepare(env: dict[str, str], cwd: Path, ontology: str, version: str | None = None) -> None:
    version = version or compiled_model(ontology).bundle_version
    policy = cwd / "installation.yaml"
    policy.write_text(SERVER_POLICY)
    for args in (
        ("init",),
        ("graph", "create", "example_graph", "--ontology", str(ONTOLOGIES / ontology / version)),
        ("policy", "apply", str(policy), "--reason", "equivalence"),
        ("graph", "skill", "publish", "example_graph", str(GRAPH_SKILL)),
        *functions_step(ontology),
    ):
        result = subprocess.run(
            [str(BIN_DIR / "graph-admin"), *args], env=env, cwd=cwd, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


def scenario(run: Run, roles: Roles, factory: OpsFactory, cwd: Path) -> list[Any]:
    """Every command of the inventory, in order, with their outcomes."""
    seen: list[Any] = []

    def call(*args: str, input: str | None = None) -> Any:
        code, document, _, _ = run(*args, input=input)
        seen.append([list(args), code, document])
        return document

    def text(*args: str) -> None:
        code, _, stdout, stderr = run(*args, text=True)
        seen.append([list(args), code, stdout, stderr])

    def create(cls: str, local: str) -> str:
        ids: dict[str, str] = {}
        for op in factory.create(cls, local):
            document = call(*create_args(op, ids))
            assert document["ok"], document
            ids[op["local"]] = document["data"]["entities"][0]["id"]
        return ids[local]

    cls = roles.term("lifecycle_type")
    for args in (
        ("whoami",),
        ("graphs",),
        ("describe", "types"),
        ("describe", "relations"),
        ("describe", "schema", "--compact"),
        ("describe", "type", cls),
        ("describe", "fields", cls),
        ("describe", "lifecycle", cls),
        ("describe", "type", "no:Such"),
        ("describe", "modules"),
        ("describe", "module", "core"),
        ("describe", "types", "--module", "core"),
        ("describe", "module", "no_such_module"),
        ("describe", "search", "name", "--kind", "type", "--kind", "attribute"),
        ("describe", "search", "name", "--module", "core", "--limit", "2"),
        ("describe", "functions"),
        ("describe", "functions", "--include-deprecated"),
        ("describe", "functions", "--module", "no_such_module"),
        ("describe", "function", "items_in_location"),
        ("describe", "function", "no_such_function"),
        ("describe", "search", "location", "--kind", "function"),
        ("describe", "search", "location"),
    ):
        call(*args)
    text("describe", "types")
    text("describe", "functions")
    text("describe", "function", "items_in_location")
    text("describe", "modules")
    entity = create(cls, "x")
    attr = next(n for n, v in factory.attrs(cls).items() if isinstance(v, str))
    call("get", entity)
    call("get", "0" * 16)
    call("find", "--type", cls, "--limit", "2")
    call("lookup", str(factory.attrs(cls)[attr]))
    call("lookup", str(factory.attrs(cls)[attr]), "--for", "items_in_location.location")
    call("lookup", "x", "--for", "items_in_location.no_such_param")
    call("search", str(factory.attrs(cls)[attr]))
    call("neighbors", entity)
    call("update", entity, "--set", f"{attr}=changed value", "--idempotency-key", "k-1")
    call("update", entity, "--set", f"{attr}=changed value", "--idempotency-key", "k-1")
    call("update", entity, "--set", f"{attr}=other", "--dry-run")
    call("history", entity)
    text("get", entity)
    guarded = roles.get("guarded_transition")
    assert isinstance(guarded, dict)
    lifecycle = factory.model.lifecycles[guarded["lifecycle"]]
    for state in lifecycle_path(
        factory.model, guarded["lifecycle"], lifecycle.initial[0], guarded["from"]
    ):
        call("transition", entity, state)
    call("transition", entity, "no_such_state")
    rel = roles.term("acyclic_relation")
    linked = factory.concrete(factory.model.relations[rel].domain[0])
    a, b = create(linked, "a"), create(linked, "b")
    call("link", a, rel, b)
    call("lookup", "a", "--match", "prefix", "--for", rel, "--for-source", rel)
    call("link", b, rel, a)
    call("unlink", a, rel, b)
    target = roles.term("supersede_target_type")
    old, new = create(target, "old"), create(target, "new")
    call("supersede", new, old)
    call("supersede", new, old, "--reason", "replaced")
    retractable = roles.term("retractable_type")
    gone = create(retractable, "r")
    call("retract", gone, "--reason", "wrong")
    call("get", gone)
    call("find", "--type", cls, "--as-of", "tx:1")
    changes = cwd / "changes.json"
    changes.write_text(json.dumps({"ops": factory.create(cls, "applied")}))
    call("apply", "changes.json", "--dry-run")
    call("apply", "changes.json", "--summary")
    batch = "\n".join(json.dumps({"ops": factory.create(cls, f"b{n}")}) for n in range(2))
    call("apply", "-", input=batch)
    call("apply", "-", input="{broken")
    call("apply", "missing.json")
    cypher = cwd / "count.cypher"
    cypher.write_text(f"MATCH (n:{cls.partition(':')[2]}) RETURN count(n) AS c")
    call("query", "MATCH (n) RETURN n.alias AS alias ORDER BY alias", "--limit", "2")
    call("query", "--file", "count.cypher")
    call("query", "-", input=cypher.read_text())
    call("query", "--file", "missing.cypher")
    text("query", "--file", "count.cypher")
    call("run", "no_such_function")
    if "inv:Location" in factory.model.classes:  # the ontology with published functions
        place, other = create("inv:Location", "place"), create("inv:Category", "kind")
        call("run", "items_in_location", "--param", f"location={place}")
        call("run", "items_in_location", "--param", f"location={place}", "--limit", "1")
        call("run", "items_in_location", "--param", f"location={other}")  # wrong class
        call("run", "items_in_location", "--param", "location=Main building")
        call("run", "items_in_location")
        call("run", "items_in_location", "--param", f"location={place}", "--param", "x=1")
        call("run", "items_by_condition", "--param", "condition=damaged")
        call("run", "items_by_condition", "--param", "condition=mint")
        text("run", "items_in_location", "--param", f"location={place}")
    out = cwd / "emitted"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    call("skills", "emit", "--out", "emitted", "--graph", "example_graph", "--force")
    seen.append(sorted(str(p.relative_to(out)) for p in out.rglob("*")))
    call("skills", "emit", "--out", "emitted", "--graph", "other_graph", "--force")
    return seen


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_local_and_remote_answers_are_equal(
    isolated_env: dict[str, str], tmp_path: Path, ontology: str
) -> None:
    roles = load_roles(ontology)
    local = home_env(isolated_env, tmp_path / "local")
    prepare(local, tmp_path, ontology)
    workdir = tmp_path / "work"
    workdir.mkdir()
    expected = scenario(
        runner(local, workdir), roles, OpsFactory(compiled_model(ontology)), workdir
    )

    runs = [o for o in expected if o[0][0] == "run" and o[1] == 0]
    assert bool(runs) == (ontology == "inventory")  # a function ran where one is published
    listed = next(o for o in expected if o[0] == ["describe", "functions"])
    assert bool(listed[2]["data"]["functions"]) == (ontology == "inventory")  # both are covered
    owner = home_env(isolated_env, tmp_path / "server")
    prepare(owner, tmp_path, ontology)
    created = subprocess.run(
        [str(BIN_DIR / "graph-admin"), "token", "create", "ci", "--json"],
        env=owner,
        capture_output=True,
        text=True,
    )
    token = json.loads(created.stdout)["data"]["token"]
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            tmp_path / "client",
            server.port,
            token,
            extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n',
        )
        actual = scenario(
            runner(client, workdir), roles, OpsFactory(compiled_model(ontology)), workdir
        )
    assert len(actual) == len(expected)
    assert covered(expected) == {s.path for s in all_specs()} - {
        ("describe", "commands"),  # the same code on both sides: the client's inventory
        ("file", "add"),  # files: test_files_answer_the_same
        ("file", "path"),
        ("file", "check"),
        ("skills", "install"),  # emit covers how graph skills arrive; install writes them
        ("skills", "check"),
        ("self", "update"),  # runs skills install and check; run_skills
        ("connect",),  # sets up a client from a code: tests/cli/test_app_connect.py
    }
    for mine, theirs in zip(normalized(actual), normalized(expected), strict=True):
        assert mine == theirs, differences(mine, theirs)


def covered(outcomes: list[Any]) -> set[tuple[str, ...]]:
    """The command paths a scenario ran."""
    paths = {s.path for s in all_specs()}
    found = set()
    for args, *_ in outcomes:
        found |= {p for p in paths if tuple(args[: len(p)]) == p}
    return found


def files_scenario(run: Run, home: Path) -> list[Any]:
    """The file commands on a graph with ``files ^1.0``, from ``<home>/notes``."""
    notes = home / "notes"
    write_pdf(notes / "scans" / "report 2024.pdf")
    write_text(notes / "other.txt", "other content")
    seen: list[Any] = []

    def call(*args: str) -> Any:
        code, document, _, _ = run(*args)
        if args[:2] == ("file", "path") and "--origin" not in args and document["ok"]:
            stored = Path(document["data"]["path"])
            assert stored.read_bytes() == synthetic_pdf()  # here, whatever the machine
            assert not os.access(stored, os.W_OK)
            document["data"]["path"] = "<stored copy>"
        seen.append([list(args), code, document])
        return document

    call("file", "add", "scans/report 2024.pdf")
    call("file", "add", "scans/report 2024.pdf")
    call("file", "add", "other.txt", "--no-origin", "--title", "Other")
    call("file", "add", "missing.pdf")
    call("file", "add", "scans", "--dry-run")
    call("file", "path", "FILE-0001")
    call("file", "path", "FILE-0001", "--origin")
    call("file", "path", "FILE-0002", "--origin")
    call("file", "check")
    (notes / "scans" / "report 2024.pdf").write_bytes(b"%PDF-1.4 changed")
    call("file", "check", "FILE-0001", "FILE-0002")
    (notes / "scans" / "report 2024.pdf").unlink()
    call("file", "check")
    call("file", "path", "FILE-0001", "--origin")
    return seen


def test_files_answer_the_same(isolated_env: dict[str, str], tmp_path: Path) -> None:
    roots = '[roots]\nnotes = "~/notes"\n'
    local_home = tmp_path / "local"
    local = home_env(isolated_env, local_home)
    prepare(local, tmp_path, "notes_and_sources", "1.1.0")
    config = Path(local["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + roots)
    expected = files_scenario(runner(local, local_home / "notes"), local_home)

    owner = home_env(isolated_env, tmp_path / "server")
    prepare(owner, tmp_path, "notes_and_sources", "1.1.0")
    created = subprocess.run(
        [str(BIN_DIR / "graph-admin"), "token", "create", "ci", "--json"],
        env=owner,
        capture_output=True,
        text=True,
    )
    token = json.loads(created.stdout)["data"]["token"]
    client_home = tmp_path / "client"
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            client_home,
            server.port,
            token,
            extra='[device]\nid = "0192e000-0000-7000-8000-00000000d001"\n' + roots,
        )
        actual = files_scenario(runner(client, client_home / "notes"), client_home)

        installed = run_skills(client, client_home)
    mine = normalized(json.loads(json.dumps(actual).replace(str(client_home), "<home>")))
    theirs = normalized(json.loads(json.dumps(expected).replace(str(local_home), "<home>")))
    for a, b in zip(mine, theirs, strict=True):
        assert a == b, differences(a, b)
    assert installed == ["fresh", "fresh"]


def run_skills(env: dict[str, str], home: Path) -> list[str]:
    """``skills install`` and ``check`` in the remote mode: the graph skill from the server."""
    project = home / "project"
    (project / ".claude").mkdir(parents=True)
    install = runner(env, project)
    code, document, _, _ = install(
        "skills", "install", "--scope", "project", "--graph", "example_graph"
    )
    assert code == 0, document
    assert (project / ".claude" / "skills" / "graph-example-graph" / "SKILL.md").is_file()
    code, document, _, _ = install("skills", "check", "--scope", "project")
    assert code == 0, document
    code, updated, _, _ = install("self", "update", "--check")
    assert code == 0, updated
    assert updated["data"]["program"]["reason"] == "no_build"  # a checkout is not replaced
    return [s["status"] for s in document["data"]["skills"]]


def freshness_scenario(run: Run, owner: dict[str, str], project: Path) -> list[Any]:
    """Plan origin_lifecycle E3 from ``project``: graph skills judged where the graph lives,
    skill 1 by the program that runs the command; the same answers either way."""
    (project / ".claude").mkdir(parents=True)
    (project / ".graph_cli.toml").write_text('format_version = 1\ngraphs = ["example_graph"]\n')
    skills = project / ".claude" / "skills"
    seen: list[Any] = []
    codes = iter(range(1, 100))

    def call(*args: str) -> Any:
        code, document, _, _ = run(*args)
        error = document["error"] or {}
        shown = json.dumps(
            [code, error.get("code"), error.get("hint"), document["warnings"]]
        ).replace(str(project), "<project>")
        seen.append([list(args), json.loads(shown)])
        return document

    def create() -> Any:
        return call(
            "create", "inv:Location", "--set", "name=Shelf", "--set", f"code=S{next(codes)}"
        )

    def republish(line: str) -> None:
        changed = project.parent / f"{project.name}_skill"
        shutil.copytree(GRAPH_SKILL, changed / GRAPH_SKILL.name)
        with (changed / GRAPH_SKILL.name / "SKILL.md").open("a") as handle:
            handle.write(f"\n{line}\n")
        args = ("graph", "skill", "publish", "example_graph", str(changed / GRAPH_SKILL.name))
        result = subprocess.run(
            [str(BIN_DIR / "graph-admin"), *args], env=owner, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr

    call("skills", "install", "--scope", "project", "--graph", "example_graph")
    assert create()["ok"] is True
    republish("Changed once.")
    call("find", "--type", "inv:Location")  # a read warns
    assert create()["error"]["code"] == "skills.outdated"  # a write is refused
    call("create", "inv:Location", "--set", "name=Shelf", "--dry-run")
    call("skills", "install", "--scope", "project")
    assert create()["ok"] is True
    marker = skills / "graph-cli" / ".graph_cli_skill.json"
    document = json.loads(marker.read_text())
    document["source"]["inventory_hash"] = "sha256:" + "0" * 64
    marker.write_text(json.dumps(document))
    call("find", "--type", "inv:Location")
    assert create()["error"]["code"] == "skills.outdated"
    call("whoami")  # meta commands are how a stale skill is found and refreshed
    call("skills", "install", "--scope", "project")
    assert create()["ok"] is True
    return seen


def test_installed_skills_are_judged_the_same(isolated_env: dict[str, str], tmp_path: Path) -> None:
    local = home_env(isolated_env, tmp_path / "local")
    prepare(local, tmp_path, "inventory")
    mine = tmp_path / "local_project"
    expected = freshness_scenario(runner(local, mine), local, mine)

    owner = home_env(isolated_env, tmp_path / "server")
    prepare(owner, tmp_path, "inventory")
    created = subprocess.run(
        [str(BIN_DIR / "graph-admin"), "token", "create", "ci", "--json"],
        env=owner,
        capture_output=True,
        text=True,
    )
    token = json.loads(created.stdout)["data"]["token"]
    project = tmp_path / "client_project"
    with graph_server(owner, tmp_path) as server:
        client = client_env(
            {**isolated_env, "GRAPH_CLI_GRAPH": "example_graph"},
            tmp_path / "client",
            server.port,
            token,
        )
        actual = freshness_scenario(runner(client, project), owner, project)
    assert normalized(actual) == normalized(expected)
    stale = [w for _, (_, _, _, warnings) in expected for w in warnings]
    assert {w["code"] for w in stale} == {"skills.stale"}
    refused = [hint for _, (_, code, hint, _) in expected if code == "skills.outdated"]
    assert refused == ["graph self update --dir <project>"] * 3
