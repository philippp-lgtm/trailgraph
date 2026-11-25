"""S/C: the plugin that checks every function and view answer against the output schema of its
tool (``support.tool_answers``) really looks at the answers of both routes, for the caller that
got them, and fails on a wrong one.

Without these tests a renamed key of an answer or a moved wrap target would turn the plugin silent
and the whole suite would stay green. The graph is ``staff`` (its personal data is marked, so the
plain local caller is a limited reader), with a function and a set view over it.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support import tool_answers
from support.admin import admin, init, write_documents
from support.markings import FUNCTION_SOURCES, STAFF, documents
from support.output_schemas import FAILURES
from support.server import call, grant_server, new_token

from graph_cli.catalog.wiring import AppServices
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
CATALOG = """\
schema: graph_cli.view/1
name: staff_catalog
module: staff
summary: Every employee, all values.
use_when: Every employee for a renderer.
sections:
  - kind: list
    id: employees
    type: stf:Employee
    fields: all
"""


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """The staff graph with its data, one function and one set view; returns the data root."""
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    functions, views = tmp_path / "functions", tmp_path / "views"
    for folder in (functions, views):
        folder.mkdir()
    (functions / "colleague_names.yaml").write_text(FUNCTION_SOURCES["colleague_names.yaml"])
    (views / "staff_catalog.yaml").write_text(CATALOG)
    for kind, folder in (("functions", functions), ("views", views)):
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    write_documents(root, "example_graph", documents())
    return root


@pytest.fixture(autouse=True)
def clean() -> Any:
    """The plugin's findings of these tests are the subject here: they must not fail the test."""
    tool_answers.VALIDATORS.clear()
    yield
    FAILURES.clear()


def printed(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> str:
    done = run_cli("graph", *args, "--json", env=env or ENV)
    assert done.returncode == 0, (args, done.stdout)
    FAILURES.clear()  # what the plugin found in the real answer is checked by the caller
    return str(done.stdout)


def found_in(text: str, env: dict[str, str], cwd: Path) -> list[str]:
    FAILURES.clear()
    tool_answers.check_process("graph", text, env, cwd)
    found = list(FAILURES)
    FAILURES.clear()
    return found


def test_a_wrong_answer_of_a_graph_process_is_found(
    run_cli: CliRunner, world: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    env = {**isolated_env, **ENV}
    for args, doctor in (
        (("run", "colleague_names"), lambda d: d["data"]["items"][0].update(name=7)),
        (("run", "colleague_names"), lambda d: d["data"]["items"][0].update(extra="x")),
        (("view", "--name", "staff_catalog"), lambda d: d["data"].update(bytes="many")),
        (("view", "--name", "staff_catalog"), lambda d: d["data"]["sections"][0].update(x=1)),
    ):
        text = printed(run_cli, *args)
        assert found_in(text, env, tmp_path) == [], args  # the answer as printed is fine
        document = json.loads(text)
        doctor(document)
        found = found_in(json.dumps(document), env, tmp_path)
        assert found and all(" tool " in line for line in found), (args, found)


def test_the_caller_of_a_process_is_the_one_its_environment_names(
    run_cli: CliRunner, world: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    text = printed(run_cli, "view", "--name", "staff_catalog", env=EXPLORE)
    env = {**isolated_env, **EXPLORE}
    services = tool_answers._EnvServices(env=env, cwd=tmp_path)
    actor = services.context([], "describe tools", None).actor
    assert (actor.agent.id, actor.agent.type) == ("agent-1", "explore")
    plain = AppServices(env=env, cwd=tmp_path).context([], "describe tools", None).actor
    assert plain.agent.id is None  # what the test process itself would have made of it
    assert found_in(text, env, tmp_path) == []
    # the schema was built for a reader with limits: the staff graph marks its personal data
    (key,) = [k for k in tool_answers.VALIDATORS if k[:2] == ("view", "staff_catalog")]
    assert key[3].limited
    # nothing was recorded for the check: the data root, the state (access log, audit, signals)
    # and everything else below the home of the run are as the run left them

    def tree() -> dict[str, tuple[int, int]]:
        files = (p for p in Path(isolated_env["HOME"]).rglob("*") if p.is_file())
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in files}

    before = tree()
    assert before and found_in(text, env, tmp_path) == []
    assert tree() == before


def test_an_answer_of_the_server_in_process_is_checked_with_the_tool_of_its_caller(
    run_cli: CliRunner, world: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    grant_server(run_cli, tmp_path)
    token = new_token(run_cli, "ci")
    server = ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO())
    seen: list[tuple[Any, ...]] = []
    checked = tool_answers.record

    def spy(*given: Any) -> None:
        seen.append(given)
        checked(*given)

    tool_answers.record = spy  # type: ignore[assignment]
    try:
        for path, args in (
            ("run", {"name": "colleague_names", "graph": "example_graph"}),
            ("view", {"name": "staff_catalog", "graph": "example_graph"}),
            ("call", {"tool": "run_colleague_names", "input": {}, "graph": "example_graph"}),
            ("call", {"tool": "view_staff_catalog", "input": {}, "graph": "example_graph"}),
        ):
            reply = call(server, "POST", f"/v1/commands/{path}", token=token, body={"args": args})
            assert reply.status == 200, reply.json
    finally:
        tool_answers.record = checked  # type: ignore[assignment]
    assert [(g[3], g[4]) for g in seen] == [
        ("function", "colleague_names"),
        ("view", "staff_catalog"),
        ("function", "colleague_names"),
        ("view", "staff_catalog"),
    ]
    reader, actor, entry, kind, name, answer = seen[0]
    assert not FAILURES
    doctored = {**answer, "items": [{**answer["items"][0], "extra": 1}]}
    tool_answers.record(reader, actor, entry, kind, name, doctored)
    assert FAILURES and "extra" in FAILURES[0]
    FAILURES.clear()  # the finding is the subject of this test
    # an answer that has no tool is no failure: the generated card and an action are the
    # command's to check
    assert tool_answers._tool_of({"view": {"name": "card", "origin": "generated"}}) == (None, "")
    assert tool_answers._tool_of({"action": {"name": "x"}}) == (None, "")
