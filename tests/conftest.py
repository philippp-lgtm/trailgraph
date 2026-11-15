"""Shared test harness.

CLIs are tested as subprocesses, the way a user or console AI runs them. Every
run gets a throwaway home and XDG directories, so tests never read or write the
real user config, data or state. Every envelope a test sees is checked against the
declared output schemas (``support.output_schemas``).
"""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from harness import BIN_DIR, CliRunner

pytest_plugins = ["support.output_schemas"]


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    """Environment with a throwaway home and XDG dirs, no GRAPH_CLI_* overrides, no AWS_*
    object store settings and no ``CLAUDE_CONFIG_DIR`` (the user skill directory is the
    throwaway home's), except that large answers stay on stdout
    (``GRAPH_CLI_SPILL_BYTES=0``): tests read whole envelopes and never write to the shared
    spill directory; spill tests set their own."""
    home = tmp_path / "home"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GRAPH_CLI_", "AWS_")) and k != "CLAUDE_CONFIG_DIR"
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        GRAPH_CLI_SPILL_BYTES="0",
    )
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        Path(env[key]).mkdir(parents=True)
    return env


@pytest.fixture
def run_cli(isolated_env: dict[str, str], tmp_path: Path) -> CliRunner:
    """Run an installed console script (``graph``, ``graph-admin``) in the isolated env."""
    from support.output_schemas import check_text  # the plugin, loaded after this file

    def run(
        program: str,
        *args: str,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        executable = BIN_DIR / program
        assert executable.exists(), f"console script not installed: {executable}"
        completed = subprocess.run(
            [str(executable), *args],
            input=input,
            capture_output=True,
            text=True,
            env={**isolated_env, **(env or {})},
            cwd=cwd or tmp_path,
            timeout=60,
        )
        check_text(program, completed.stdout, short=program == "graph")
        return completed

    return run
