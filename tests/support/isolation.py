"""The isolation of a test run: a throwaway home and XDG directories, and a runner of the console
scripts in them. The fixtures of ``conftest.py`` (``isolated_env``, ``run_cli``) are these for one
test; a module that builds one installation for several tests (a module-scoped fixture) makes its
own with a directory of its own."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from harness import BIN_DIR, CliRunner

from support import launcher
from support.output_schemas import check_text
from support.tool_answers import check_process


def isolated(tmp_path: Path) -> dict[str, str]:
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


def runner(isolated_env: dict[str, str], tmp_path: Path) -> CliRunner:
    """Run an installed console script (``graph``, ``graph-admin``) in ``isolated_env``."""

    def run(
        program: str,
        *args: str,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        fresh: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """``fresh=True`` starts the console script as a real process, not forked."""
        executable = BIN_DIR / program
        assert executable.exists(), f"console script not installed: {executable}"
        command = [str(executable), *args]
        run_env = {**isolated_env, **(env or {})}
        start = subprocess.run if fresh else launcher.run
        completed = start(
            command,
            input=input,
            capture_output=True,
            text=True,
            env=run_env,
            cwd=cwd or tmp_path,
            timeout=60,
        )
        check_text(program, completed.stdout, short=program == "graph")
        check_process(program, completed.stdout, run_env, cwd or tmp_path)
        return completed

    return run
