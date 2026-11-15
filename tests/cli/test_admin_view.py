"""S: graph-admin view as a process (graph view plan, step 4)."""

import http.client
import json
import re
import signal
import socket
import subprocess
from pathlib import Path

import pytest
from harness import BIN_DIR, CliRunner
from support.activity import short_dir
from support.admin import admin, init
from support.app import create_graph
from support.policies import write_policy


def start_view(
    args: tuple[str, ...], env: dict[str, str], cwd: Path, *, sigint_ignored: bool = False
) -> subprocess.Popen[str]:
    """``graph-admin view ARGS`` as a process. Its SIGINT is set to the default, not
    inherited: a shell starts background jobs with SIGINT ignored, so a suite run in the
    background would hand that on and every Ctrl+C test would wait for a view that keeps
    ignoring it. ``sigint_ignored`` starts it like such a background job."""

    def set_sigint() -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN if sigint_ignored else signal.SIG_DFL)

    return subprocess.Popen(
        [str(BIN_DIR / "graph-admin"), "view", *args],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=set_sigint,
    )


def stop_view(proc: subprocess.Popen[str], sig: int = signal.SIGINT) -> tuple[str, str]:
    """Send ``sig`` and collect the output. A view that does not stop within 10 s fails
    the test and is killed, never left running."""
    proc.send_signal(sig)
    try:
        return proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        pytest.fail(f"graph-admin view did not stop on {signal.Signals(sig).name}: {err}")


def address(proc: subprocess.Popen[str]) -> tuple[int, str]:
    """Port and token from the first line on stderr."""
    assert proc.stderr is not None
    line = proc.stderr.readline()
    found = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", line)
    assert found, line
    return int(found.group(1)), found.group(2)


def with_reads(env: dict[str, str]) -> None:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[activity]\nsignal_reads = true\n")


def test_serves_until_interrupted(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    with_reads(isolated_env)
    with short_dir() as state:
        activity = state / "graph_cli" / "activity"
        env = {**isolated_env, "XDG_STATE_HOME": str(state)}
        proc = start_view(("--graph", "example_graph", "--no-open"), env, tmp_path)
        try:
            port, token = address(proc)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/graph?t={token}")
            data = json.loads(conn.getresponse().read())
            assert data["graph"]["name"] == "example_graph" and data["signal_reads"] is True
            (sock,) = activity.glob("*.sock")
        finally:
            out, err = stop_view(proc)
        assert proc.returncode == 0, err
        assert "stopped" in out
        assert not sock.exists()


def test_sigterm_stops_the_view_as_ctrl_c_does(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    with_reads(isolated_env)
    with short_dir() as state:
        env = {**isolated_env, "XDG_STATE_HOME": str(state)}
        proc = start_view(("--graph", "example_graph", "--no-open"), env, tmp_path)
        try:
            address(proc)
            (sock,) = (state / "graph_cli" / "activity").glob("*.sock")
        finally:
            out, err = stop_view(proc, signal.SIGTERM)
        assert proc.returncode == 0 and "stopped" in out, err
        assert not sock.exists()


def test_a_signal_right_after_the_address_still_stops_cleanly(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """The signal handlers are in place before the address is printed: a Ctrl+C at once
    is no abort halfway through the start."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    for _ in range(3):
        proc = start_view(("--graph", "example_graph", "--no-open"), isolated_env, tmp_path)
        try:
            address(proc)
        finally:
            out, err = stop_view(proc)
        assert proc.returncode == 0 and "stopped" in out, err


def test_a_view_started_with_sigint_ignored_keeps_ignoring_it(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    """As a background job of a shell expects; SIGTERM still stops it cleanly."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    proc = start_view(
        ("--graph", "example_graph", "--no-open"), isolated_env, tmp_path, sigint_ignored=True
    )
    try:
        address(proc)
        proc.send_signal(signal.SIGINT)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1)
    finally:
        out, err = stop_view(proc, signal.SIGTERM)
    assert proc.returncode == 0 and "stopped" in out, err


def test_a_view_without_reads_says_why(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    with_reads(isolated_env)
    state = tmp_path / ("s" * 120)  # too long for a Unix socket path
    env = {**isolated_env, "XDG_STATE_HOME": str(state)}
    proc = start_view(("--graph", "example_graph", "--no-open"), env, tmp_path)
    try:
        assert proc.stderr is not None
        lines = [proc.stderr.readline() for _ in range(2)]
    finally:
        stop_view(proc)
    assert "the activity socket cannot be bound (" in lines[1], lines
    assert str(state / "graph_cli" / "activity") in lines[1]
    assert not list((state / "graph_cli" / "activity").glob("*.sock"))


def test_json_mode_and_a_taken_port_are_refused(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    refused = admin(run_cli, "view", "--graph", "example_graph")
    assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = str(taken.getsockname()[1])
        busy = run_cli("graph-admin", "view", "--graph", "example_graph", "--port", port)
    assert busy.returncode != 0 and "admin.view_port_unavailable" in busy.stderr
    assert "--port 0" in busy.stderr


def test_a_token_file_gives_the_token_and_stays_out_of_the_address(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    token = "T" * 40
    token_file = tmp_path / "view_token"
    token_file.write_text(f"  {token}\n")
    serve = ("--graph", "example_graph", "--no-open", "--token-file", str(token_file))
    proc = start_view(serve, isolated_env, tmp_path)
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        found = re.search(r"http://127\.0\.0\.1:(\d+)/ ", line)
        assert found and "--token-file" in line and token not in line, line
        conn = http.client.HTTPConnection("127.0.0.1", int(found.group(1)), timeout=5)
        conn.request("GET", f"/graph?t={token}")
        assert conn.getresponse().status == 200
    finally:
        out, err = stop_view(proc)
    assert proc.returncode == 0 and token not in out + err, err


def test_a_bad_token_file_is_refused(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    short, empty, binary = tmp_path / "short", tmp_path / "empty", tmp_path / "binary"
    short.write_text("s3cr3t-but-short\n")
    empty.write_text(" \n")
    binary.write_bytes(b"s3cr3t" + bytes(range(128, 192)))
    for file, why in (
        (short, "shorter than 32 characters"),
        (empty, "is empty"),
        (binary, "is not UTF-8"),
        (tmp_path / "missing", "cannot be read"),
        (tmp_path, "cannot be read"),
    ):
        refused = run_cli(
            "graph-admin", "view", "--graph", "example_graph", "--token-file", str(file)
        )
        assert refused.returncode == 2, refused.stderr
        assert "usage.invalid_argument" in refused.stderr and why in refused.stderr
        assert "s3cr3t" not in refused.stderr + refused.stdout
        assert "0x" not in refused.stderr and "position" not in refused.stderr


HIDDEN_FROM_NO_PROJECT = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
scopes:
  viewer: {"*": read}
no_project: {"*": read, example_graph: none}
"""


def listed(args: tuple[str, ...], env: dict[str, str], cwd: Path) -> list[str]:
    """The graphs a view started with ``args`` lists."""
    proc = start_view(("--no-open", *args), env, cwd)
    try:
        port, token = address(proc)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/graphs?t={token}")
        return [g["name"] for g in json.loads(conn.getresponse().read())["graphs"]]
    finally:
        stop_view(proc)


def test_a_project_gets_the_view_its_scope(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    create_graph(run_cli, "other_graph", "inventory")
    policy = write_policy(tmp_path / "work", "installation.yaml", HIDDEN_FROM_NO_PROJECT)
    assert admin(run_cli, "policy", "apply", str(policy), "--reason", "test policy").code == 0
    assert listed((), isolated_env, tmp_path) == ["other_graph"]
    assert sorted(listed(("--project", "viewer"), isolated_env, tmp_path)) == [
        "example_graph",
        "other_graph",
    ]
    refused = run_cli("graph-admin", "view", "--project", "not a name")
    assert refused.returncode == 2 and "usage.invalid_argument" in refused.stderr


def test_an_invalid_installed_policy_keeps_the_view_from_starting(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    policy = root / "policy" / "installation.json"
    policy.parent.mkdir()
    policy.write_text('{"schema": "graph_cli.policy/1", "rules": "everything"}\n')
    refused = run_cli("graph-admin", "view", "--no-open")  # no fallback to the built-in policy
    assert refused.returncode != 0 and "policy.invalid" in refused.stderr, refused.stderr
