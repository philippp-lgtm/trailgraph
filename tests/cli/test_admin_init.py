"""S: graph-admin init, output modes and usage errors (contracts B9, B12.4, B12.7; plan M7, M8)."""

import json
import tomllib
from pathlib import Path

from harness import CliRunner
from support.admin import admin, parse
from support.trees import tree_hash


def xdg(env: dict[str, str], name: str) -> Path:
    return Path(env[name]) / "graph_cli"


def test_init_creates_root_and_config(run_cli: CliRunner, isolated_env: dict[str, str]) -> None:
    outcome = admin(run_cli, "init", "--device-name", "box")
    assert outcome.code == 0
    data_root = xdg(isolated_env, "XDG_DATA_HOME")
    config_file = xdg(isolated_env, "XDG_CONFIG_HOME") / "config.toml"
    assert outcome.data["created"] == {"data_root": True, "config": True}
    assert (outcome.data["data_root"], outcome.data["root_source"]) == (
        str(data_root.resolve()),
        "default",
    )
    assert (data_root / "graphs").is_dir()
    config = tomllib.loads(config_file.read_text())
    assert config["device"] == {"id": outcome.data["device_id"], "name": "box"}
    assert "paths" not in config
    assert outcome.envelope["command"] == "init"


def test_init_is_idempotent_and_keeps_the_config(run_cli: CliRunner) -> None:
    first = admin(run_cli, "init")
    second = admin(run_cli, "init", "--device-name", "ignored")
    assert second.code == 0
    assert second.data["created"] == {"data_root": False, "config": False}
    assert second.data["device_id"] == first.data["device_id"]


def test_init_persists_a_flag_root(run_cli: CliRunner, tmp_path: Path) -> None:
    root = tmp_path / "graphs_root"
    assert admin(run_cli, "init", "--data-root", str(root)).data["root_source"] == "flag"
    again = admin(run_cli, "init")
    assert (again.data["data_root"], again.data["root_source"]) == (str(root.resolve()), "config")


def test_env_root_warns(run_cli: CliRunner, tmp_path: Path) -> None:
    outcome = admin(run_cli, "init", env={"GRAPH_CLI_ROOT": str(tmp_path / "env_root")})
    assert outcome.data["root_source"] == "env"
    assert outcome.warning_codes == ["root.overridden"]


def test_output_mode_from_env_and_text_default(run_cli: CliRunner) -> None:
    result = run_cli("graph-admin", "init", env={"GRAPH_CLI_OUTPUT": "json"})
    assert json.loads(result.stdout)["ok"] is True
    text = run_cli("graph-admin", "init")
    assert text.returncode == 0 and "device_id:" in text.stdout and text.stderr == ""


def test_usage_errors_are_envelopes(run_cli: CliRunner) -> None:
    outcome = admin(run_cli, "init", "--bogus")
    assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument")
    outcome = admin(run_cli, "nonsense")
    assert (outcome.code, outcome.error["code"]) == (2, "usage.unknown_command")
    text = run_cli("graph-admin", "init", "--bogus")
    assert text.returncode == 2 and "No such option" in text.stderr and text.stdout == ""


def test_broken_config_is_config_invalid(run_cli: CliRunner, isolated_env: dict[str, str]) -> None:
    config_file = xdg(isolated_env, "XDG_CONFIG_HOME") / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('format_version = 1\n[runtime]\nmode = "remote"\n')
    outcome = parse(run_cli("graph-admin", "init", "--json"))
    assert (outcome.code, outcome.error["code"]) == (5, "config.invalid")


def test_help_has_examples_and_no_side_effects(run_cli: CliRunner, tmp_path: Path) -> None:
    before = tree_hash(tmp_path)
    for args in (("--help",), ("init", "--help")):
        result = run_cli("graph-admin", *args)
        assert result.returncode == 0
    assert result.stdout.count("graph-admin init") >= 2
    assert tree_hash(tmp_path) == before
