"""The test harness never touches the real user environment, and a run forked from the
launcher behaves like a fresh process."""

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, REPO_ROOT, CliRunner
from support.launcher import ENABLED, Launcher


def test_isolated_env_points_into_tmp(isolated_env: dict[str, str], tmp_path: Path) -> None:
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(isolated_env[key]).is_relative_to(tmp_path), key
    overrides = {k: v for k, v in isolated_env.items() if k.startswith("GRAPH_CLI_")}
    assert overrides == {"GRAPH_CLI_SPILL_BYTES": "0"}  # answers stay on stdout
    assert isolated_env["HOME"] != os.environ.get("HOME")
    assert "CLAUDE_CONFIG_DIR" not in isolated_env  # the user's installed skills stay unseen


# ---- the launcher (support.launcher): forked runs behave like fresh processes ----


@pytest.fixture
def probe(tmp_path: Path) -> Iterator[Launcher]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "tests")}
    launcher = Launcher("probe", "support.launcher_probe", BIN_DIR, env)
    yield launcher
    launcher.close()


def _run(launcher: Launcher, tmp_path: Path, *args: str, input: str | None = None) -> Any:
    return launcher.run(
        list(args), input=input, env={**os.environ, "PROBE": "yes"}, cwd=tmp_path, timeout=10
    )


linux_only = pytest.mark.skipif(not ENABLED, reason="the launcher forks on Linux only")


@linux_only
def test_a_run_gets_its_arguments_environment_directory_and_streams(
    probe: Launcher, tmp_path: Path
) -> None:
    done = _run(probe, tmp_path, "echo", "3", input="some input")
    assert done.returncode == 3
    seen = json.loads(done.stdout)
    assert seen["argv"] == [str(BIN_DIR / "probe"), "echo", "3"]
    assert (seen["cwd"], seen["env"], seen["stdin"]) == (str(tmp_path), "yes", "some input")
    assert seen["path0"] == str(BIN_DIR)  # as for a console script
    assert done.stderr == "to stderr\n"


@linux_only
def test_no_state_survives_a_run(probe: Launcher, tmp_path: Path) -> None:
    _run(probe, tmp_path, "echo")
    assert json.loads(_run(probe, tmp_path, "echo").stdout)["runs"] == ["echo"]


@linux_only
def test_exits_as_the_interpreter_would(probe: Launcher, tmp_path: Path) -> None:
    raised = _run(probe, tmp_path, "raise")
    assert raised.returncode == 1
    assert "ValueError: boom" in raised.stderr
    message = _run(probe, tmp_path, "exit_text")
    assert (message.returncode, message.stderr) == (1, "a message\n")
    assert _run(probe, tmp_path, "kill").returncode == -signal.SIGKILL


@linux_only
def test_a_run_past_its_timeout_is_killed(probe: Launcher, tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe.run(["sleep"], input=None, env=dict(os.environ), cwd=tmp_path, timeout=1)
    assert time.monotonic() - started < 10


@linux_only
def test_runs_from_several_threads_go_on_in_parallel(probe: Launcher, tmp_path: Path) -> None:
    with ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(lambda n: _run(probe, tmp_path, "echo", str(n)).returncode, range(8)))
    assert codes == list(range(8))


@linux_only
@pytest.mark.parametrize(
    "args",
    [
        ("graph", "--help"),
        ("graph", "--version"),
        ("graph", "get", "missing", "--json"),
        ("graph-admin", "--help"),
        ("graph-admin", "graph", "list", "--json"),
        ("graph-admin", "no-such-command"),
    ],
)
def test_a_forked_run_answers_like_a_fresh_process(
    run_cli: CliRunner, args: tuple[str, ...]
) -> None:
    forked = run_cli(*args)
    fresh = run_cli(*args, fresh=True)
    assert (forked.returncode, forked.stdout, forked.stderr) == (
        fresh.returncode,
        fresh.stdout,
        fresh.stderr,
    )
