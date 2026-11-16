"""The test harness never touches the real user environment, and a run forked from the
launcher behaves like a fresh process."""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from harness import BIN_DIR, REPO_ROOT, CliRunner
from support.launcher import ENABLED, FORKED, Launcher


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
    launcher = Launcher("probe", BIN_DIR, env, entry="support.launcher_probe:main")
    yield launcher
    launcher.close()


def _run(launcher: Launcher, tmp_path: Path, *args: str, input: str | None = None) -> Any:
    return launcher.run(
        [str(BIN_DIR / "probe"), *args],
        input=input,
        env={**os.environ, "PROBE": "yes"},
        cwd=tmp_path,
        timeout=10,
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
def test_more_of_a_fresh_process(probe: Launcher, tmp_path: Path) -> None:
    assert _run(probe, tmp_path, "interrupt").returncode == -signal.SIGINT
    assert _run(probe, tmp_path, "dunder").stdout == "via __stdout__\n"
    raw = json.loads(_run(probe, tmp_path, "echo", input="a\r\nb").stdout)["stdin"]
    assert raw == "a\r\nb"  # stdin without newline translation, as on POSIX
    with pytest.raises(FileNotFoundError):
        _run(probe, tmp_path / "missing", "echo")


@linux_only
def test_a_run_ends_when_its_output_does(probe: Launcher, tmp_path: Path) -> None:
    started = time.monotonic()
    _run(probe, tmp_path, "leave_child")  # as subprocess.run: until the pipes close
    assert time.monotonic() - started >= 0.4


@linux_only
def test_a_different_interpreter_start_means_a_fresh_process(probe: Launcher) -> None:
    assert probe.can_run(dict(probe.base_env))
    assert not probe.can_run({**probe.base_env, "PYTHONIOENCODING": "latin-1"})
    assert not probe.can_run({**probe.base_env, "LC_ALL": "C"})


@linux_only
def test_a_program_whose_import_writes_to_stderr_is_refused(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "tests")}
    noisy = Launcher("noisy", BIN_DIR, env, entry="support.launcher_probe_noisy:main")
    try:
        with pytest.raises(RuntimeError, match="noise at import"):
            _run(noisy, tmp_path, "anything")
    finally:
        noisy.close()


@linux_only
def test_a_run_past_its_timeout_is_killed(probe: Launcher, tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe.run(
            [str(BIN_DIR / "probe"), "sleep"],
            input=None,
            env=dict(os.environ),
            cwd=tmp_path,
            timeout=1,
        )
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


# What importing an entry point does, traced in a fresh interpreter: the launcher's premise.
IMPORT_TRACE = """
import atexit, json, os, signal, sys, threading, time
from importlib.metadata import entry_points

reads = []

target, prefix = sys.argv[1], sys.argv[2]

def caller():
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_globals.get("__name__", "").startswith(prefix):
            return f"{frame.f_code.co_filename}:{frame.f_lineno}"
        frame = frame.f_back
    return None

class Environ(type(os.environ)):
    def __getitem__(self, key):
        if (where := caller()):
            reads.append(f"environ[{key}] at {where}")
        return super().__getitem__(key)
    def __contains__(self, key):
        if (where := caller()):
            reads.append(f"{key} in environ at {where}")
        return super().__contains__(key)
    def __iter__(self):
        if (where := caller()):
            reads.append(f"iter(environ) at {where}")
        return super().__iter__()

os.environ.__class__ = Environ
for module, name in ((os, "getcwd"), (time, "time"), (time, "monotonic")):
    def traced(*args, _real=getattr(module, name), _name=name):
        if (where := caller()):
            reads.append(f"{_name}() at {where}")
        return _real(*args)
    setattr(module, name, traced)
def handlers():
    return [signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGCHLD)]

signals = handlers()
threads, exits = threading.active_count(), atexit._ncallbacks()
sys.path[0] = os.path.dirname(sys.executable)
if ":" in target:  # module:<name>, for the positive control
    __import__(target.split(":", 1)[1])
else:
    (point,) = entry_points(group="console_scripts", name=target)
    point.load()
print(json.dumps({
    "reads": reads,
    "signals": signals == handlers(),
    "threads": threading.active_count() - threads,
    "atexit": atexit._ncallbacks() - exits,
}))
"""


@pytest.mark.parametrize("program", sorted(FORKED))
def test_importing_a_program_reads_nothing_of_the_process(program: str) -> None:
    """Dynamic twin of the import-time rule in test_source_rules.py: importing the entry point
    reads no environment variable, working directory or clock, and starts no thread, signal
    handler or exit hook."""
    assert _import_trace(program, "graph_cli") == {
        "reads": [],
        "signals": True,
        "threads": 0,
        "atexit": 0,
    }
    control = _import_trace("module:support.launcher_probe_reads", "support.")
    assert [read.split(" at ")[0] for read in control["reads"]] == ["environ[HOME]", "getcwd()"]


def _import_trace(target: str, prefix: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-c", IMPORT_TRACE, target, prefix],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "tests")},
    )
    return json.loads(done.stdout)  # type: ignore[no-any-return]
