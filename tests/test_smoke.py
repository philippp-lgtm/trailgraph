"""Both console scripts are installed, start, and report their version."""

import subprocess
import sys

import pytest
from harness import CliRunner


@pytest.mark.parametrize(
    ("program", "distribution"), [("graph", "graph_cli"), ("graph-admin", "graph_cli_admin")]
)
def test_version(run_cli: CliRunner, program: str, distribution: str) -> None:
    result = run_cli(program, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"{program}, version ")


@pytest.mark.parametrize("program", ["graph", "graph-admin"])
def test_help(run_cli: CliRunner, program: str) -> None:
    result = run_cli(program, "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


@pytest.mark.parametrize("module", ["graph_cli", "graph_cli_admin"])
def test_module_entry_point(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, "--version"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_app_help_does_not_mention_maintenance(run_cli: CliRunner) -> None:
    result = run_cli("graph", "--help")
    assert "graph-admin" not in result.stdout
