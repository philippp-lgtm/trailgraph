"""S: large JSON answers go to a file (cli_kit.spill, catalog.spill; contracts B9.2): stdout
keeps the envelope without data, a warning names the file, the file holds the whole answer."""

import json
import os
import shlex
import stat
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init, write_documents
from support.app import ENV, app, create_graph
from support.fixtures import FIXTURES

from graph_cli.kernel.changeset import read_documents

LIMIT = "2000"


@pytest.fixture
def spill_dir(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    seeds = read_documents(
        (FIXTURES / "seeds" / "notes_and_sources" / "changes.jsonl").read_bytes()
    )
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    directory = tmp_path / "spill"
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(
        config.read_text()
        + f'\n[output]\nspill_dir = "{directory}"\n\n[audit]\naccess_log = true\n'
    )
    return directory


SPILL = {**ENV, "GRAPH_CLI_SPILL_BYTES": LIMIT}


def test_a_large_answer_goes_to_a_private_file(
    run_cli: CliRunner, spill_dir: Path, isolated_env: dict[str, str]
) -> None:
    whole = app(run_cli, "find", "--limit", "500", env=ENV)
    short = app(run_cli, "find", "--limit", "500", env=SPILL)
    assert short.code == 0 and short.data is None
    (warning,) = [w for w in short.envelope["warnings"] if w["code"] == "output.spilled"]
    path = Path(warning["details"]["path"])
    assert warning["details"]["limit"] == int(LIMIT)
    assert warning["details"]["bytes"] == len(json.dumps(whole.envelope, ensure_ascii=False)) + 1
    assert path.parent == spill_dir / "example_graph" and path.name.startswith("find_")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(spill_dir.stat().st_mode) == 0o700
    stored = json.loads(path.read_text())
    assert stored == whole.envelope
    assert len(path.read_text().splitlines()) > 10  # pretty-printed for line-wise tools
    assert short.envelope["page"] == whole.envelope["page"]
    first = short.envelope["hints"][0]
    assert first["command"] == shlex.join(["jq", ".data | keys", str(path)])
    log = Path(isolated_env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    output = json.loads(log.read_text().splitlines()[-1])["output"]
    assert output["spilled_bytes"] == warning["details"]["bytes"] > output["bytes"]


def test_small_answers_text_mode_and_zero_stay_on_stdout(
    run_cli: CliRunner, spill_dir: Path
) -> None:
    small = app(run_cli, "lookup", "zzqq", env=SPILL)
    assert small.data is not None and not small.warning_codes
    text = run_cli("graph", "find", "--limit", "500", env=SPILL)
    assert text.returncode == 0 and text.stderr == "" and len(text.stdout) > int(LIMIT)
    off = app(run_cli, "find", "--limit", "500", env={**ENV, "GRAPH_CLI_SPILL_BYTES": "0"})
    assert off.data is not None and not spill_dir.exists()


def test_without_a_graph_the_file_is_in_the_directory_itself(
    run_cli: CliRunner, spill_dir: Path
) -> None:
    short = app(run_cli, "describe", "commands", env=SPILL)
    (warning,) = short.envelope["warnings"]
    path = Path(warning["details"]["path"])
    assert path.parent == spill_dir and path.name.startswith("describe_commands_")


def test_an_open_directory_is_refused_and_the_answer_printed(
    run_cli: CliRunner, spill_dir: Path
) -> None:
    spill_dir.mkdir(mode=0o755)
    os.chmod(spill_dir, 0o755)
    whole: Any = app(run_cli, "find", "--limit", "500", env=SPILL)
    assert whole.code == 0 and whole.data["items"]
    (warning,) = whole.envelope["warnings"]
    assert warning["code"] == "output.spill_failed"
    assert "private" in warning["details"]["reason"]
    assert list(spill_dir.iterdir()) == []
