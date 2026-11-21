"""S: ``graph start``, the first call of a session (graph start plan S1-S3).

The inventory graph holds the inventory functions, actions and views and a published skill; a
second graph has no catalogs. ``start`` answers in a few lines which graphs the caller reaches and
how many functions, actions and views its token may use (and may not), refreshes stale skill
copies as ``graph self update`` does without touching the program, and is never refused for a
stale skill (it is how one is found).
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.catalog_leaks import assert_hidden_absent
from support.fixtures import FIXTURES
from support.policies import write_policy
from support.trees import tree_hash

from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.storage.files.audit_log import FileAuditLog

FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
GRAPH_SKILL = FIXTURES / "skills" / "graph-example-graph"
POINTER = 'format_version = 1\ngraphs = ["example_graph", "plain_graph"]\n'
BUDGET = 1500


def publish(run_cli: CliRunner, graph: str, kind: str, folder: Path) -> None:
    published = admin(run_cli, "graph", kind, "publish", graph, str(folder))
    assert published.code == 0, published.envelope


@pytest.fixture
def project(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory", title="Example inventory")
    create_graph(run_cli, "plain_graph", "inventory")
    for kind, folder in (("functions", FUNCTIONS), ("actions", ACTIONS), ("views", VIEWS)):
        publish(run_cli, "example_graph", kind, folder)
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(GRAPH_SKILL))
    assert published.code == 0, published.envelope
    directory = tmp_path / "project"
    (directory / ".claude").mkdir(parents=True)
    (directory / ".graph_cli.toml").write_text(POINTER)
    return directory


def start(run_cli: CliRunner, project: Path, *args: str, env: dict[str, str] | None = None) -> Any:
    return app(run_cli, "start", *args, cwd=project, env=env)


def by_name(data: Any) -> dict[str, Any]:
    return {g["name"]: g for g in data["graphs"]}


def test_start_names_the_graphs_and_what_the_token_may_use(
    run_cli: CliRunner, project: Path
) -> None:
    done = start(run_cli, project)
    assert done.code == 0, done.envelope
    data = done.data
    assert data["schema"] == "graph_cli.start/1"
    assert data["program"] == {
        "name": "graph",
        "version": data["program"]["version"],
        "state": "local",
    }
    assert data["skills"] == []  # nothing installed here: nothing to refresh
    example, plain = by_name(data)["example_graph"], by_name(data)["plain_graph"]
    assert example["title"] == "Example inventory" and example["level"] == "write"
    assert example["skill"] == "graph-example-graph" and plain["skill"] is None
    assert example["counts"] == {
        "modules": 2,
        "types": example["counts"]["types"],
        "functions": 2,
        "actions": 2,
        "views": 3,
    }
    assert plain["counts"]["functions"] == plain["counts"]["actions"] == 0
    assert "not_allowed" not in example and data["hidden_count"] == 0
    assert data["identity"]["acting"]


def test_the_text_is_short_and_names_the_search_commands(run_cli: CliRunner, project: Path) -> None:
    text = run_cli("graph", "start", cwd=project).stdout
    assert len(text.encode()) <= BUDGET
    lines = text.splitlines()
    assert lines[0].startswith("graph ")
    assert any(
        line.startswith('  example_graph "Example inventory" (write): modules ') for line in lines
    )
    assert "functions 2, actions 2, views 3" in text and "(no skill)" in text
    assert "Search: graph describe search TEXT [--kind function|action|view]" in text
    assert "Module: graph describe module M" in text
    assert "tell the user instead of working around it" in text


def test_start_refreshes_a_stale_copy_and_reports_it(run_cli: CliRunner, project: Path) -> None:
    installed = app(
        run_cli,
        "skills",
        "install",
        "--scope",
        "project",
        "--dir",
        str(project),
        "--project-graphs",
    )
    assert installed.code == 0, installed.envelope
    skills = project / ".claude" / "skills"
    fresh = start(run_cli, project)
    assert {(s["skill"], s["state"]) for s in fresh.data["skills"]} == {
        ("graph-cli", "fresh"),
        ("graph-example-graph", "fresh"),
    }

    def republish(note: str) -> None:
        """The maintainer republishes the graph skill: installed copies are stale."""
        source = tmp_skill(project.parent, note)
        (source / "SKILL.md").write_text((source / "SKILL.md").read_text() + f"\n{note}\n")
        publish(run_cli, "example_graph", "skill", source)

    republish("first")
    before = tree_hash(skills)
    checked = start(run_cli, project, "--check")
    assert checked.code == 0
    assert {(s["skill"], s["state"]) for s in checked.data["skills"]} == {
        ("graph-cli", "fresh"),
        ("graph-example-graph", "stale"),
    }
    assert tree_hash(skills) == before  # --check writes nothing
    done = start(run_cli, project)
    assert {(s["skill"], s["state"]) for s in done.data["skills"]} == {
        ("graph-cli", "fresh"),
        ("graph-example-graph", "refreshed"),
    }
    assert tree_hash(skills) != before
    again = start(run_cli, project)
    assert all(s["state"] == "fresh" for s in again.data["skills"])
    republish("second")
    text = run_cli("graph", "start", cwd=project).stdout
    assert "graph-example-graph refreshed; if loaded earlier, load again." in text
    assert "skills fresh: 1" in text


def tmp_skill(base: Path, name: str) -> Path:
    """A copy of the graph skill source (the directory must carry the skill's name)."""
    folder = base / name / GRAPH_SKILL.name
    shutil.copytree(GRAPH_SKILL, folder)
    return folder


def test_the_user_scope_never_gets_the_graph_skills_of_the_project_it_runs_in(
    run_cli: CliRunner, project: Path, isolated_env: dict[str, str]
) -> None:
    """Like ``self update``: a scope is refreshed with what it has; only the project scope also
    gets the skills of its pointer's graphs. Which project an agent runs in decides nothing
    about what is installed for the user."""
    skills = Path(isolated_env["HOME"]) / ".claude" / "skills"
    skills.parent.mkdir()
    installed = app(run_cli, "skills", "install", "--scope", "user", cwd=project)
    assert installed.code == 0, installed.envelope
    assert sorted(p.name for p in skills.iterdir()) == ["graph-cli"]  # no graph skill yet
    before = tree_hash(skills)
    checked = start(run_cli, project, "--check")
    assert [(s["scope"], s["skill"], s["state"]) for s in checked.data["skills"]] == [
        ("user", "graph-cli", "fresh")
    ]
    assert tree_hash(skills) == before
    done = start(run_cli, project)
    assert [(s["scope"], s["skill"], s["state"]) for s in done.data["skills"]] == [
        ("user", "graph-cli", "fresh")
    ]
    assert sorted(p.name for p in skills.iterdir()) == ["graph-cli"] and tree_hash(skills) == before
    # the project scope, which has a skill, does get the graphs of its pointer
    app(run_cli, "skills", "install", "--scope", "project", "--dir", str(project))
    again = start(run_cli, project)
    assert {(s["scope"], s["skill"], s["state"]) for s in again.data["skills"]} == {
        ("user", "graph-cli", "fresh"),
        ("project", "graph-cli", "fresh"),
        ("project", "graph-example-graph", "refreshed"),
    }
    assert sorted(p.name for p in skills.iterdir()) == ["graph-cli"]
    # a graph skill the user scope has is refreshed there, and nothing else is added
    mine = app(run_cli, "skills", "install", "--scope", "user", "--graph", "example_graph")
    assert mine.code == 0, mine.envelope
    changed = tmp_skill(project.parent, "user")
    (changed / "SKILL.md").write_text((changed / "SKILL.md").read_text() + "\nuser note\n")
    publish(run_cli, "example_graph", "skill", changed)
    refreshed = start(run_cli, project)
    assert ("user", "graph-example-graph", "refreshed") in {
        (s["scope"], s["skill"], s["state"]) for s in refreshed.data["skills"]
    }
    assert sorted(p.name for p in skills.iterdir()) == ["graph-cli", "graph-example-graph"]


def test_a_copy_edited_by_hand_is_reported_and_left_alone(
    run_cli: CliRunner, project: Path
) -> None:
    app(run_cli, "skills", "install", "--scope", "project", "--dir", str(project))
    skill = project / ".claude" / "skills" / "graph-cli" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nmy note\n")
    before = tree_hash(skill.parent)
    done = start(run_cli, project)
    assert done.code == 0, done.envelope
    assert [(s["skill"], s["state"]) for s in done.data["skills"] if s["skill"] == "graph-cli"] == [
        ("graph-cli", "modified")
    ]
    assert tree_hash(skill.parent) == before  # left alone (the graph's own skill is installed)
    assert (skill.parent.parent / "graph-example-graph").is_dir()
    assert (
        "skills left alone: graph-cli (modified)" in run_cli("graph", "start", cwd=project).stdout
    )


def test_start_is_never_refused_for_a_stale_skill_and_does_not_warn_about_it(
    run_cli: CliRunner, project: Path
) -> None:
    app(
        run_cli,
        "skills",
        "install",
        "--scope",
        "project",
        "--dir",
        str(project),
        "--project-graphs",
    )
    changed = tmp_skill(project.parent, "changed")
    (changed / "SKILL.md").write_text((changed / "SKILL.md").read_text() + "\nmore\n")
    publish(run_cli, "example_graph", "skill", changed)
    stale = app(run_cli, "find", "--type", "inv:Item", "--graph", "example_graph", cwd=project)
    assert "skills.stale" in stale.warning_codes  # a read warns...
    done = start(run_cli, project, "--check")
    assert done.code == 0 and "skills.stale" not in done.warning_codes  # ...start never does


def test_the_program_is_never_replaced(run_cli: CliRunner, project: Path) -> None:
    done = start(run_cli, project)
    assert done.data["program"]["state"] == "local"
    commands = app(run_cli, "describe", "commands")
    entry = next(c for c in commands.data["commands"] if c["path"] == ["start"])
    assert entry["kind"] == "meta"  # the skill gate never judges a meta command


def test_one_graph_adds_its_modules(run_cli: CliRunner, project: Path) -> None:
    done = start(run_cli, project, "--graph", "example_graph")
    assert done.code == 0, done.envelope
    (graph,) = done.data["graphs"]
    modules = {m["id"]: m for m in graph["modules"]}
    assert modules["inventory"]["functions"] == 2 and modules["inventory"]["actions"] == 2
    assert "inventory" in run_cli("graph", "start", "--graph", "example_graph", cwd=project).stdout
    missing = start(run_cli, project, "--graph", "no_such_graph")
    assert (missing.code, missing.error["code"]) == (1, "graph.not_found")


def test_the_pointer_narrows_the_graphs_and_counts_what_it_hides(
    run_cli: CliRunner, project: Path
) -> None:
    (project / ".graph_cli.toml").write_text('format_version = 1\ngraphs = ["example_graph"]\n')
    done = start(run_cli, project)
    assert list(by_name(done.data)) == ["example_graph"] and done.data["hidden_count"] == 1
    assert (
        "+1 outside the pointer file: graph graphs" in run_cli("graph", "start", cwd=project).stdout
    )


def test_without_a_data_root_there_is_nothing_to_reach(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    done = app(run_cli, "start", cwd=tmp_path)
    assert done.code == 0, done.envelope
    assert done.data["graphs"] == [] and done.data["hidden_count"] == 0
    assert "none reachable" in run_cli("graph", "start", cwd=tmp_path).stdout


NO_EXPLORE = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_functions, agent_type: [explore], action: ["read.function"]}
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""
HIDE_LOCATIONS = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Location": none
"""
EXPLORE = {"GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}


def test_a_reader_without_rights_gets_not_allowed_counts_and_no_hidden_name(
    run_cli: CliRunner, project: Path, tmp_path: Path
) -> None:
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    plain = start(run_cli, project)
    assert plain.data["graphs"][0]["counts"]["actions"] == 2
    reader = start(run_cli, project, env=EXPLORE)
    example = by_name(reader.data)["example_graph"]
    # read.function is withheld, but the graph stays readable: never "none" for that
    assert example["level"] == "read" and example["withheld"] == ["read.function"]
    assert "(read; no read.function)" in run_cli("graph", "start", cwd=project, env=EXPLORE).stdout
    assert by_name(plain.data)["example_graph"]["withheld"] == []
    assert example["counts"]["functions"] == 0 and example["counts"]["actions"] == 0
    assert example["not_allowed"]["functions"] == example["not_allowed"]["actions"] == 2
    assert (
        "actions 0 (+2 not allowed)" in run_cli("graph", "start", cwd=project, env=EXPLORE).stdout
    )
    # the sums agree with what describe lists for the same reader
    listed = app(run_cli, "describe", "actions", "--graph", "example_graph", env=EXPLORE)
    assert listed.data["not_allowed"] == example["not_allowed"]["actions"]
    for kind in ("functions", "views"):
        found = app(run_cli, "describe", kind, "--graph", "example_graph", env=EXPLORE)
        assert found.data["not_allowed"] == example["not_allowed"][kind]
    # and nothing of start is a recorded decision beyond what graphs records
    # ...hide the location type as well: its function, action and views are absent, not counted
    hide = write_policy(tmp_path / "work", "g.yaml", HIDE_LOCATIONS)
    applied = admin(
        run_cli, "policy", "apply", str(hide), "--graph", "example_graph", "--reason", "x"
    )
    assert applied.code == 0, applied.envelope
    limited = start(run_cli, project, env=EXPLORE)
    shown = by_name(limited.data)["example_graph"]
    assert shown["not_allowed"].get("functions", 0) == 1  # items_by_condition: only items
    assert shown["not_allowed"].get("actions", 0) == 1  # retire_item; move_item names the location
    assert_hidden_absent(
        [limited.envelope, run_cli("graph", "start", cwd=project, env=EXPLORE).stdout],
        {"items_in_location", "move_item", "location_overview"},
    )
    assert json.dumps(limited.envelope).count("items_in_location") == 0


def test_the_text_stays_within_the_budget_for_five_graphs_and_names_the_rest(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    names = [f"example_graph_number_{n:02d}" for n in range(1, 7)]
    for name in names:
        create_graph(run_cli, name, "inventory", title="A title that is quite long indeed " * 2)
        for kind, folder in (("functions", FUNCTIONS), ("actions", ACTIONS), ("views", VIEWS)):
            publish(run_cli, name, kind, folder)
    text = run_cli("graph", "start", cwd=tmp_path).stdout
    assert len(text.encode()) <= BUDGET, text
    assert "and 1 more: graph graphs" in text
    assert text.count("example_graph_number_") == 5
    data = app(run_cli, "start", cwd=tmp_path).data
    assert len(data["graphs"]) == 6  # the JSON names all of them


def test_start_records_no_decision(
    run_cli: CliRunner, isolated_env: dict[str, str], project: Path, tmp_path: Path
) -> None:
    """Counting takes no decision, like a listing: the installation stream does not move."""
    policy = write_policy(tmp_path / "work", "i.yaml", NO_EXPLORE)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    root = Path(isolated_env["XDG_DATA_HOME"]) / "graph_cli"
    log = FileAuditLog(root)
    before = list(log.read_lines(INSTALLATION_STREAM))
    assert start(run_cli, project, env=EXPLORE).code == 0
    assert start(run_cli, project, "--graph", "example_graph", env=EXPLORE).code == 0
    assert list(log.read_lines(INSTALLATION_STREAM)) == before
