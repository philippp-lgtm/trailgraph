"""Fast console script runs for ``run_cli``: a preloaded process forks one child per run.

Starting ``graph`` or ``graph-admin`` costs about 200 ms, nearly all of it importing the
program; most end-to-end tests run a program a dozen times or more. A launcher per program
and test process imports the program once, the way its console script does
(``from <module> import main``, with the script's directory first on ``sys.path``), and
then forks per run. The forked child takes the run's arguments, environment, working
directory and standard streams and calls ``main`` like the console script; its exit status
(also a signal) is the run's. To the program a run is a fresh process that has just
imported its entry module: no state of an earlier run survives, because every run forks from
the same untouched launcher. This holds as long as importing a program reads nothing of its
environment (variables, home, working directory, clock, randomness);
``tests/test_source_rules.py`` keeps that true.

``run`` is the drop-in for ``subprocess.run`` (``run_cli`` and the tests that build their own
command lines use it). Linux only (``fork`` without ``exec`` is not safe on macOS); elsewhere,
and with ``GRAPH_CLI_TEST_FORK=0``, every run starts the console script as a real process.
``run_cli(..., fresh=True)``, ``tests/test_smoke.py`` and ``tests/cli/test_app_startup.py``
always do, and ``tests/test_harness.py`` compares forked runs with fresh ones.

Run as a script, this file is the launcher: ``python launcher.py <module> <bin dir>``. It
listens on an abstract Unix socket named on stdin's first line and exits when stdin closes.
One connection is one run: a handler process (forked from the launcher) forks the child,
reports its pid, waits for it and reports its exit status, so runs from several threads of
a test go on in parallel.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

ENABLED = sys.platform == "linux" and os.environ.get("GRAPH_CLI_TEST_FORK", "1") != "0"
BIN_DIR = Path(sys.executable).parent  # as harness.BIN_DIR; this file also runs as a script
# Runs of these console scripts fork from a launcher; any other command starts a real process.
FORKED = {
    "graph": "graph_cli.app_cli.main",
    "graph-admin": "graph_cli_admin.maintenance_cli.main",
}
_launchers: dict[str, Launcher] = {}
_launchers_lock = threading.Lock()


def run(
    argv: Sequence[str],
    *,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run(argv, capture_output=True, text=True, ...)``; a run of ``graph`` or
    ``graph-admin`` forks from the program's launcher."""
    assert capture_output and text, "runs always capture text"
    executable = Path(argv[0])
    if ENABLED and executable.parent == BIN_DIR and executable.name in FORKED:
        done = _launcher(executable.name).run(
            [str(a) for a in argv[1:]],
            input=input,
            env=os.environ if env is None else env,
            cwd=Path.cwd() if cwd is None else Path(cwd),
            timeout=600 if timeout is None else timeout,
        )
    else:
        done = subprocess.run(
            [str(a) for a in argv],
            input=input,
            env=env,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    if check:
        done.check_returncode()
    return done


def close_all() -> None:
    """Stop the launchers of this process (at the end of the session)."""
    with _launchers_lock:
        for launcher in _launchers.values():
            launcher.close()
        _launchers.clear()


def _launcher(program: str) -> Launcher:
    with _launchers_lock:
        if program not in _launchers:
            base = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("GRAPH_CLI_", "AWS_")) and k != "CLAUDE_CONFIG_DIR"
            }
            _launchers[program] = Launcher(program, FORKED[program], BIN_DIR, base)
        return _launchers[program]


# ---- the test side ----


class Launcher:
    """One launcher process for one console script, started on first use."""

    def __init__(self, program: str, module: str, bin_dir: Path, base_env: Mapping[str, str]):
        self.program = program
        self.module = module
        self.bin_dir = bin_dir
        self.base_env = dict(base_env)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.address = ""
        self.streams = Path(tempfile.mkdtemp(prefix="graph_cli_launcher_"))

    def _start(self) -> None:
        self.address = f"\0graph_cli_launcher_{os.getpid()}_{uuid.uuid4().hex}"
        self.process = subprocess.Popen(
            [sys.executable, __file__, self.module, str(self.bin_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=self.base_env,
            cwd=self.streams,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(self.address.encode("unicode_escape") + b"\n")
        self.process.stdin.flush()
        ready = self.process.stdout.readline()
        if ready != b"ready\n":
            raise RuntimeError(f"launcher for {self.program} did not start: {ready!r}")

    def _connect(self) -> socket.socket:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(self.address)
            return conn

    def run(
        self,
        args: list[str],
        *,
        input: str | None,
        env: Mapping[str, str],
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        """Like ``subprocess.run([script, *args], input=..., capture_output=True, text=True,
        env=..., cwd=..., timeout=...)``."""
        run_id = uuid.uuid4().hex
        paths = {name: self.streams / f"{run_id}.{name}" for name in ("in", "out", "err")}
        paths["in"].write_text(input if input is not None else "")
        argv = [str(self.bin_dir / self.program), *args]
        request = {
            "argv": argv,
            "env": dict(env),
            "cwd": str(cwd),
            "stdin": str(paths["in"]) if input is not None else os.devnull,
            "stdout": str(paths["out"]),
            "stderr": str(paths["err"]),
        }
        try:
            with self._connect() as conn:
                conn.sendall(json.dumps(request).encode() + b"\n")
                reader = conn.makefile("rb")
                pid = int(json.loads(reader.readline())["pid"])
                conn.settimeout(timeout)
                try:
                    line = reader.readline()
                except TimeoutError:
                    _kill(pid)
                    raise subprocess.TimeoutExpired(
                        argv, timeout, _read(paths["out"]), _read(paths["err"])
                    ) from None
                if not line:
                    raise RuntimeError(f"launcher for {self.program} lost run {argv}")
                code = int(json.loads(line)["code"])
            return subprocess.CompletedProcess(argv, code, _read(paths["out"]), _read(paths["err"]))
        finally:
            for path in paths.values():
                path.unlink(missing_ok=True)

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for path in self.streams.glob("*"):
            path.unlink(missing_ok=True)
        self.streams.rmdir()


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


# ---- the launcher side ----


def _serve(module_name: str, bin_dir: str) -> None:
    address = sys.stdin.buffer.readline().rstrip(b"\n").decode("unicode_escape")
    sys.path[0] = bin_dir  # as for the console script, whose directory comes first
    import importlib

    main = importlib.import_module(module_name).main
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(address)
    listener.listen(64)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while True:
        readable, _, _ = select.select([listener, sys.stdin], [], [], 1.0)
        _reap()
        if sys.stdin in readable and not sys.stdin.buffer.read1(1):
            return  # the test process is gone or done
        if listener in readable:
            conn, _ = listener.accept()
            if os.fork() == 0:
                listener.close()
                _handle(conn, main)
            conn.close()


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _handle(conn: socket.socket, main: object) -> None:
    """In a handler process: run one request in a child, report its pid and exit status."""
    code = 1
    try:
        reader = conn.makefile("rb")
        request = json.loads(reader.readline())
        pid = os.fork()
        if pid == 0:
            conn.close()
            _child(request, main)
        conn.sendall(json.dumps({"pid": pid}).encode() + b"\n")
        _, status = os.waitpid(pid, 0)
        conn.sendall(json.dumps({"code": os.waitstatus_to_exitcode(status)}).encode() + b"\n")
        code = 0
    finally:
        os._exit(code)


def _child(request: dict[str, object], main: object) -> None:
    """In the child: become the run (streams, directory, environment, arguments), run the
    program's ``main`` as its console script does, exit the way the interpreter would."""
    code = 1
    try:
        for fd, key, flags in (
            (0, "stdin", os.O_RDONLY),
            (1, "stdout", os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
            (2, "stderr", os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
        ):
            opened = os.open(str(request[key]), flags, 0o600)
            os.dup2(opened, fd)
            os.close(opened)
        sys.stdin = _text(0, "r", sys.stdin)
        sys.stdout = _text(1, "w", sys.stdout)
        sys.stderr = _text(2, "w", sys.stderr, line_buffering=True)  # as in 3.9+
        os.chdir(str(request["cwd"]))
        os.environ.clear()
        os.environ.update(request["env"])  # type: ignore[arg-type]
        import time

        time.tzset()
        sys.argv = list(request["argv"])  # type: ignore[call-overload]
        try:
            raise SystemExit(main())  # type: ignore[operator]
        except SystemExit as stop:
            code = _exit_code(stop.code)
        except BaseException:
            sys.excepthook(*sys.exc_info())
            code = 1
        threading._shutdown()  # type: ignore[attr-defined]  # wait for non-daemon threads
        import atexit

        atexit._run_exitfuncs()
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


def _text(fd: int, mode: str, like: object, *, line_buffering: bool = False) -> object:
    """A standard stream on ``fd`` with the encoding the interpreter chose for ``like``."""
    return open(
        fd,
        mode,
        encoding=like.encoding,  # type: ignore[attr-defined]
        errors=like.errors,  # type: ignore[attr-defined]
        closefd=False,
        buffering=1 if line_buffering else -1,
    )


def _exit_code(value: object) -> int:
    """The exit status of ``SystemExit(value)``, as the interpreter computes it."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value & 0xFF
    print(value, file=sys.stderr)
    return 1


if __name__ == "__main__":
    _serve(sys.argv[1], sys.argv[2])
