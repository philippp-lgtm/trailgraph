"""Both console scripts are installed, start, and report their version."""

import importlib
import subprocess
import sys
import tomllib
from importlib.metadata import version

import pytest
from harness import REPO_ROOT, CliRunner


@pytest.mark.parametrize(
    ("program", "distribution"), [("graph", "graph_cli"), ("graph-admin", "graph_cli_admin")]
)
def test_version(run_cli: CliRunner, program: str, distribution: str) -> None:
    result = run_cli(program, "--version", fresh=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{program}, version {version(distribution)}\n"


@pytest.mark.parametrize("distribution", ["graph_cli", "graph_cli_admin"])
def test_version_constant_matches_distribution(distribution: str) -> None:
    """``__version__`` is a constant (no metadata lookup at startup); it must follow
    the version in the distribution's pyproject.toml."""
    pyproject = REPO_ROOT / "packages" / distribution / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert importlib.import_module(distribution).__version__ == declared


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
