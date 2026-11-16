"""Fast console script runs for ``run_cli``: a preloaded process forks one child per run.

Starting ``graph`` or ``graph-admin`` costs about 200 ms, nearly all of it importing the
program; most end-to-end tests run a program a dozen times or more. A launcher per program
and test process imports the program once, the way its console script does (the entry point
of ``[project.scripts]``, with the script's directory first on ``sys.path``), and then forks
per run. The forked child takes the run's arguments, environment, working directory and
standard streams (pipes, as ``subprocess.run`` gives them) and calls the entry point like the
console script; its exit status (also a signal) is the run's, and the run ends when it has
exited and its pipes are closed. To the program a run is a fresh process that has just
imported its entry module: no state of an earlier run survives, because every run forks from
the same untouched launcher. This holds as long as importing a program reads nothing of its
process (environment, home, working directory, clock, randomness, terminal);
``tests/test_source_rules.py`` keeps that true, and the launcher refuses to start when the
import writes to stderr.

What a forked run cannot have: its own interpreter start. A run whose environment differs from
the launcher's in a variable the interpreter reads at start (``PYTHON*``, ``LANG``, ``LC_*``)
therefore starts as a real process. All runs of one launcher share its string hash seed: a
test that checks that output does not change between runs makes one of them fresh.
``kernel.timing``'s ``startup`` phase counts from the launcher's import.

``run`` is the drop-in for ``subprocess.run`` (``run_cli`` and the tests that build their own
command lines use it). Linux only (``fork`` without ``exec`` is not safe on macOS); elsewhere,
and with ``GRAPH_CLI_TEST_FORK=0``, every run starts the console script as a real process.
``run_cli(..., fresh=True)`` and ``tests/test_smoke.py`` always do, and
``tests/test_harness.py`` compares forked runs with fresh ones.

Run as a script, this file is the launcher: ``python launcher.py <program> <bin dir>
<socket> [<module:function>]``. It listens on a Unix socket in a private directory and
exits when its stdin closes; its stderr is a log file next to the socket. One connection is
one run: a handler process (forked from the launcher) receives the request and the run's
three pipe ends, forks the child, and reports its exit status; when the test side closes the
connection first (timeout, interrupt), the handler kills the child. Runs from several threads
of a test go on in parallel.
"""

from __future__ import annotations

import contextlib
import json
import locale
import os
import select
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from importlib.metadata import entry_points
from pathlib import Path

ENABLED = sys.platform == "linux" and os.environ.get("GRAPH_CLI_TEST_FORK", "1") != "0"
BIN_DIR = Path(sys.executable).parent  # as harness.BIN_DIR; this file also runs as a script
# Runs of these console scripts fork from a launcher; any other command starts a real process.
FORKED = ("graph", "graph-admin")
# Read by the interpreter at its start: a run that sets them differently needs its own start.
STARTUP_VARIABLES = ("PYTHON", "LANG", "LC_")
MESSAGE = 1 << 20  # the largest request (arguments and environment) a run may send

_launchers: dict[str, Launcher] = {}
_launchers_lock = threading.Lock()


def run(
    argv: Sequence[str | Path],
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
    command = [str(a) for a in argv]
    executable = Path(command[0])
    run_env = dict(os.environ if env is None else env)
    launcher = None
    if ENABLED and executable.parent == BIN_DIR and executable.name in FORKED:
        launcher = _launcher(executable.name)
    if launcher is not None and launcher.can_run(run_env):
        assert executable.exists(), f"console script not installed: {executable}"
        done = launcher.run(
            command,
            input=input,
            env=run_env,
            cwd=Path.cwd() if cwd is None else Path(cwd),
            timeout=timeout,
        )
    else:
        done = subprocess.run(
            command,
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
            _launchers[program] = Launcher(program, BIN_DIR, base)
        return _launchers[program]


# ---- the test side ----


class Launcher:
    """One launcher process for one console script, started on first use."""

    def __init__(
        self,
        program: str,
        bin_dir: Path,
        base_env: Mapping[str, str],
        *,
        entry: str | None = None,
    ) -> None:
        """``entry`` (``module:function``) replaces the program's console script entry point
        (for the launcher's own tests)."""
        self.program = program
        self.entry = entry
        self.bin_dir = bin_dir
        self.base_env = dict(base_env)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.home = Path(tempfile.mkdtemp(prefix="gcl_"))  # 0700: only this user connects
        self.address = str(self.home / "socket")
        self.log = self.home / "launcher.log"

    def can_run(self, env: Mapping[str, str]) -> bool:
        """Whether a forked run sees the interpreter start the run's environment asks for."""

        def startup(environment: Mapping[str, str]) -> dict[str, str]:
            return {k: v for k, v in environment.items() if k.startswith(STARTUP_VARIABLES)}

        return startup(env) == startup(self.base_env)

    def _start(self) -> None:
        if self.process is not None:
            self._stop()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.address)
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    self.program,
                    str(self.bin_dir),
                    self.address,
                    *([self.entry] if self.entry else []),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                env=self.base_env,
                cwd=self.home,
            )
        assert self.process.stdout is not None
        ready = self.process.stdout.readline()
        if ready != b"ready\n":
            raise RuntimeError(
                f"launcher for {self.program} did not start ({ready!r}):\n{self._log_text()}"
            )

    def _log_text(self) -> str:
        try:
            return self.log.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def _connect(self, deadline: float | None) -> socket.socket:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            conn.settimeout(_remaining(deadline))
            conn.connect(self.address)
            return conn

    def run(
        self,
        argv: list[str],
        *,
        input: str | None,
        env: Mapping[str, str],
        cwd: Path,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        """Like ``subprocess.run(argv, input=..., capture_output=True, text=True, env=...,
        cwd=..., timeout=...)``."""
        if not cwd.is_dir():  # subprocess fails before the program starts, so do we
            raise FileNotFoundError(2, "No such file or directory", str(cwd))
        deadline = None if timeout is None else time.monotonic() + timeout
        encoding = locale.getpreferredencoding(False)
        request = json.dumps({"argv": argv, "env": dict(env), "cwd": str(cwd)}).encode()
        assert len(request) < MESSAGE, "request too large for a forked run"
        stdin_read, stdin_write = os.pipe() if input is not None else (_devnull(), -1)
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        theirs = (stdin_read, stdout_write, stderr_write)
        ours = [fd for fd in (stdin_write, stdout_read, stderr_read) if fd >= 0]
        try:
            with self._connect(deadline) as conn:
                socket.send_fds(conn, [request], list(theirs))
                for fd in theirs:
                    os.close(fd)
                theirs = ()
                data = (input or "").encode(encoding) if input is not None else b""
                result = _communicate(conn, stdin_write, stdout_read, stderr_read, data, deadline)
                ours = []  # _communicate closed them
            if result is None:  # timed out: the closed connection made the handler kill it
                raise subprocess.TimeoutExpired(argv, timeout or 0.0)
            code, out, err = result
            if code is None:
                raise RuntimeError(
                    f"launcher for {self.program} lost run {argv}:\n{self._log_text()}"
                )
            return subprocess.CompletedProcess(
                argv, code, _decoded(out, encoding), _decoded(err, encoding)
            )
        finally:
            for fd in (*theirs, *ours):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            assert process.stdin is not None
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def close(self) -> None:
        self._stop()
        for path in self.home.iterdir():
            path.unlink(missing_ok=True)
        self.home.rmdir()


def _devnull() -> int:
    return os.open(os.devnull, os.O_RDONLY)


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(deadline - time.monotonic(), 0.001)


def _decoded(data: bytes, encoding: str) -> str:
    """As ``subprocess.run(text=True)`` decodes: the locale's encoding, universal newlines."""
    return data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")


def _communicate(
    conn: socket.socket,
    stdin_write: int,
    stdout_read: int,
    stderr_read: int,
    data: bytes,
    deadline: float | None,
) -> tuple[int | None, bytes, bytes] | None:
    """Feed stdin, read stdout and stderr to their end and the exit status from the handler,
    like ``Popen.communicate``; ``None`` when the deadline passes first. Closes the pipes."""
    out: list[bytes] = []
    err: list[bytes] = []
    code: int | None = None
    conn_open = True
    sel = selectors.DefaultSelector()
    if stdin_write >= 0:
        if data:
            os.set_blocking(stdin_write, False)
            sel.register(stdin_write, selectors.EVENT_WRITE)
        else:
            os.close(stdin_write)
            stdin_write = -1
    sel.register(stdout_read, selectors.EVENT_READ, out)
    sel.register(stderr_read, selectors.EVENT_READ, err)
    sel.register(conn, selectors.EVENT_READ)
    readers = {stdout_read, stderr_read}
    try:
        while conn_open or readers:
            events = sel.select(_remaining(deadline))
            if not events and deadline is not None and time.monotonic() >= deadline:
                return None
            for key, _ in events:
                if key.fileobj is conn:
                    message = conn.recv(MESSAGE)
                    sel.unregister(conn)
                    conn_open = False
                    if message:
                        code = int(json.loads(message)["code"])
                elif key.fd == stdin_write:
                    try:
                        data = data[os.write(stdin_write, data) :]
                    except BrokenPipeError:  # the program stopped reading: as communicate()
                        data = b""
                    if not data:
                        sel.unregister(stdin_write)
                        os.close(stdin_write)
                        stdin_write = -1
                else:
                    chunk = os.read(key.fd, 65536)
                    if chunk:
                        key.data.append(chunk)
                    else:
                        sel.unregister(key.fd)
                        os.close(key.fd)
                        readers.discard(key.fd)
        return code, b"".join(out), b"".join(err)
    finally:
        for key in list(sel.get_map().values()):
            if key.fileobj is not conn:
                with contextlib.suppress(OSError):
                    os.close(key.fd)
        with contextlib.suppress(OSError):
            if stdin_write >= 0:
                os.close(stdin_write)
        sel.close()


# ---- the launcher side ----


def _serve(program: str, bin_dir: str, address: str, entry: str | None = None) -> None:
    sys.path[0] = bin_dir  # as for the console script, whose directory comes first
    log_size = os.fstat(2).st_size
    if entry is None:
        (point,) = entry_points(group="console_scripts", name=program)
        main = point.load()
    else:
        import importlib

        module, function = entry.split(":")
        main = getattr(importlib.import_module(module), function)
    if os.fstat(2).st_size != log_size:
        sys.stdout.write("importing the program wrote to stderr\n")
        sys.stdout.flush()
        return
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
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
    """In a handler process: run one request in a child and report its exit status; kill the
    child when the test side goes away first."""
    try:
        message, fds, _, _ = socket.recv_fds(conn, MESSAGE, 3)
        request = json.loads(message)
        pid = os.fork()
        if pid == 0:
            conn.close()
            _child(request, fds, main)
        for fd in fds:
            os.close(fd)
        pidfd = os.pidfd_open(pid)
        readable, _, _ = select.select([conn, pidfd], [], [])
        if pidfd not in readable:  # the connection closed: timeout or interrupt
            os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            conn.send(json.dumps({"code": os.waitstatus_to_exitcode(status)}).encode())
    finally:
        os._exit(0)


def _child(request: dict[str, object], fds: list[int], main: object) -> None:
    """In the child: become the run (streams, directory, environment, arguments), run the
    program's entry point as its console script does, exit the way the interpreter would."""
    code = 1
    interrupted = False
    try:
        for target, fd in enumerate(fds):
            os.dup2(fd, target)
            os.close(fd)
        sys.stdin = sys.__stdin__ = _text(0, "r", sys.stdin)
        sys.stdout = sys.__stdout__ = _text(1, "w", sys.stdout)
        sys.stderr = sys.__stderr__ = _text(2, "w", sys.stderr, line_buffering=True)
        os.chdir(str(request["cwd"]))
        os.environ.clear()
        os.environ.update(request["env"])  # type: ignore[arg-type]
        time.tzset()
        sys.argv = list(request["argv"])  # type: ignore[call-overload]
        try:
            raise SystemExit(main())  # type: ignore[operator]
        except SystemExit as stop:
            code = _exit_code(stop.code)
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())
            interrupted = True
        except BaseException:
            sys.excepthook(*sys.exc_info())
            code = 1
        threading._shutdown()  # type: ignore[attr-defined]  # wait for non-daemon threads
        import atexit

        atexit._run_exitfuncs()
        sys.stdout.flush()
        sys.stderr.flush()
        if interrupted:  # as the interpreter: die of SIGINT
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
    finally:
        os._exit(code)


def _text(fd: int, mode: str, like: object, *, line_buffering: bool = False) -> object:
    """A standard stream on ``fd`` with the encoding the interpreter chose for ``like`` and its
    newline handling on POSIX (none)."""
    return open(
        fd,
        mode,
        encoding=like.encoding,  # type: ignore[attr-defined]
        errors=like.errors,  # type: ignore[attr-defined]
        newline="\n",
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
    _serve(*sys.argv[1:])
