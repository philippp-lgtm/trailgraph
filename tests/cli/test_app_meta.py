"""S: graph whoami, graphs, describe and the app wiring (contracts B2.7, B2.10, B6.12, B9, B11,
B12; plan A2-A6)."""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import data_root, init
from support.app import app, create_graph
from support.fixtures import GOLDENS, TOY_ONTOLOGIES, assert_golden
from support.roles import load_roles
from support.trees import tree_hash
from test_vocabulary_leak import deny_list

POINTER = 'format_version = 1\nproject = "example_project"\ngraphs = ["alpha"]\n'


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def project(tmp_path: Path, text: str = POINTER) -> Path:
    directory = tmp_path / "project" / "sub"
    directory.mkdir(parents=True)
    (tmp_path / "project" / ".graph_cli.toml").write_text(text)
    return directory


def test_whoami_with_env_and_a_forged_entrypoint(run_cli: CliRunner) -> None:
    env = {
        "GRAPH_CLI_AGENT_ID": "agent-7",
        "GRAPH_CLI_AGENT_TYPE": "general",
        "GRAPH_CLI_ENTRYPOINT": "maint",
    }
    outcome = app(run_cli, "whoami", env=env)  # works without a data root
    assert outcome.code == 0, outcome.envelope
    actor = outcome.data["actor"]
    assert actor["agent"] == {
        "assurance": "asserted",
        "id": "agent-7",
        "type": "general",
        "source": "env:GRAPH_CLI_AGENT_ID",
    }
    assert actor["client"]["entrypoint"] == "app"
    assert actor["principal"]["assurance"] == "verified"
    assert "write.create" in outcome.data["permissions"]["allowed"]
    bad = app(run_cli, "whoami", env={"GRAPH_CLI_AGENT_ID": "bad id!"})
    assert bad.data["actor"]["agent"] == {"assurance": "absent"}
    assert bad.warning_codes == ["actor.facet_malformed"]


def test_graphs_and_the_pointer_allowlist(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "alpha")
    create_graph(run_cli, "beta", "notes_and_sources")
    listed = app(run_cli, "graphs")
    assert [g["name"] for g in listed.data["items"]] == ["alpha", "beta"]
    assert listed.data["hidden_count"] == 0
    here = project(tmp_path)
    narrowed = app(run_cli, "graphs", cwd=here)
    assert [g["name"] for g in narrowed.data["items"]] == ["alpha"]
    assert narrowed.data["hidden_count"] == 1
    who = app(run_cli, "whoami", cwd=here)
    assert who.data["actor"]["project"] == {
        "assurance": "asserted",
        "id": "example_project",
        "source": "pointer",
    }


def test_unreadable_manifest_is_skipped_with_a_warning(run_cli: CliRunner, root: Path) -> None:
    create_graph(run_cli, "alpha")
    broken = root / "graphs" / "0192f000-0000-7000-8000-00000000beef"
    broken.mkdir()
    (broken / "manifest.toml").write_text("this is not toml [")
    outcome = app(run_cli, "graphs")
    assert outcome.code == 0
    assert [g["name"] for g in outcome.data["items"]] == ["alpha"]
    assert outcome.warning_codes == ["graph.unreadable"]


def test_graph_selection(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create_graph(run_cli, "alpha")
    create_graph(run_cli, "beta")
    missing = app(run_cli, "describe", "types")
    assert (missing.code, missing.error["code"]) == (2, "graph.not_selected")
    assert missing.error["details"]["candidates"] == ["alpha", "beta"]
    assert missing.error["hint"] == "graph graphs"
    assert (
        app(run_cli, "describe", "types", env={"GRAPH_CLI_GRAPH": "beta"}).envelope["graph"]["name"]
        == "beta"
    )
    assert (
        app(run_cli, "describe", "types", "--graph", "alpha").envelope["graph"]["name"] == "alpha"
    )
    here = project(tmp_path)
    outside = app(run_cli, "describe", "types", "--graph", "beta", cwd=here)
    assert (outside.code, outside.error["code"]) == (1, "graph.not_in_project")
    unknown = app(run_cli, "describe", "types", "--graph", "gamma")
    assert (unknown.code, unknown.error["code"]) == (1, "graph.not_found")


def test_pointer_cannot_move_the_root(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    here = project(tmp_path, 'format_version = 1\ngraphs = ["alpha"]\ndata_root = "/elsewhere"\n')
    outcome = app(run_cli, "graphs", cwd=here)
    assert (outcome.code, outcome.error["code"]) == (5, "config.forbidden_key")


def test_no_data_root_flag(run_cli: CliRunner, root: Path) -> None:
    outcome = app(run_cli, "graphs", "--data-root", str(root))
    assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument")


def test_missing_root_creates_nothing(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    before = tree_hash(tmp_path)
    outcome = app(run_cli, "graphs")
    assert (outcome.code, outcome.error["code"]) == (5, "backend.unavailable")
    assert outcome.error["hint"] == "graph-admin init"
    assert not data_root(isolated_env).exists()
    assert tree_hash(tmp_path) == before


def test_root_override_warns(run_cli: CliRunner, root: Path) -> None:
    outcome = app(run_cli, "graphs", env={"GRAPH_CLI_ROOT": str(root)})
    assert outcome.code == 0 and outcome.warning_codes == ["root.overridden"]


def test_usage_errors_and_help(run_cli: CliRunner, root: Path) -> None:
    outcome = app(run_cli, "teleport")
    assert (outcome.code, outcome.error["code"]) == (2, "usage.unknown_command")
    before = tree_hash(root)
    result = run_cli("graph", "describe", "type", "--help")
    assert result.returncode == 0 and "Examples:" in result.stdout
    assert result.stdout.count("graph describe type") >= 2
    assert tree_hash(root) == before


@pytest.mark.parametrize("ontology", TOY_ONTOLOGIES)
def test_describe_goldens(run_cli: CliRunner, root: Path, ontology: str) -> None:
    create_graph(run_cli, "example_graph", ontology)
    cls = load_roles(ontology).term("lifecycle_type")
    outcome = app(run_cli, "describe", "type", cls, "--graph", "example_graph")
    assert outcome.code == 0, outcome.envelope
    data = outcome.data
    del data["graph"]["id"]  # minted at graph create
    assert_golden(
        GOLDENS / "app" / f"describe_type_{ontology}.json",
        (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(),
    )
    unknown = app(run_cli, "describe", "type", cls + "x", "--graph", "example_graph")
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    assert unknown.error["details"]["did_you_mean"][0] == cls
    for command in (["types"], ["relations"], ["fields", cls], ["lifecycle", cls]):
        assert app(run_cli, "describe", *command, "--graph", "example_graph").code == 0


def test_inventory_golden_and_leaks(run_cli: CliRunner) -> None:
    outcome = app(run_cli, "describe", "commands")
    assert outcome.code == 0
    inventory = outcome.data
    assert_golden(
        GOLDENS / "app" / "inventory.json", (json.dumps(inventory, indent=2) + "\n").encode()
    )
    assert not [a for a in inventory["actions"] if a.startswith("maint.")]  # B11.7
    assert not [e for e in inventory["error_codes"] if e["code"].startswith("admin.")]
    paths = [c["path"] for c in inventory["commands"]]
    assert not [p for p in paths if p[0] in ("init", "ontology", "audit")]
    words, prefixes = deny_list()  # B11.4: placeholder-only examples
    for command in inventory["commands"]:
        assert len(command["summary"]) <= 100 and len(command["examples"]) >= 2
        for example in command["examples"]:
            text = example["command"]
            assert text.startswith("graph ")
            assert not [w for w in words if w in text], text
            assert not [p for p in prefixes if f"{p}:" in text], text
