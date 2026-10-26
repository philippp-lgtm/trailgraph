"""S: graph-admin view as a process (graph view plan, step 4)."""

import http.client
import json
import re
import signal
import socket
import subprocess
from pathlib import Path

from harness import BIN_DIR, CliRunner
from support.activity import short_dir
from support.admin import admin, init
from support.app import create_graph


def test_serves_until_interrupted(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[activity]\nsignal_reads = true\n")
    with short_dir() as state:
        activity = state / "graph_cli" / "activity"
        proc = subprocess.Popen(
            [str(BIN_DIR / "graph-admin"), "view", "--graph", "example_graph", "--no-open"],
            env={**isolated_env, "XDG_STATE_HOME": str(state)},
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stderr is not None
            line = proc.stderr.readline()
            found = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", line)
            assert found, line
            port, token = int(found.group(1)), found.group(2)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/graph?t={token}")
            data = json.loads(conn.getresponse().read())
            assert data["graph"]["name"] == "example_graph" and data["signal_reads"] is True
            (sock,) = activity.glob("*.sock")
        finally:
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, err
        assert "stopped" in out
        assert not sock.exists()


def test_a_view_without_reads_says_why(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[activity]\nsignal_reads = true\n")
    state = tmp_path / ("s" * 120)  # too long for a Unix socket path
    proc = subprocess.Popen(
        [str(BIN_DIR / "graph-admin"), "view", "--graph", "example_graph", "--no-open"],
        env={**isolated_env, "XDG_STATE_HOME": str(state)},
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stderr is not None
        lines = [proc.stderr.readline() for _ in range(2)]
    finally:
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=10)
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
    proc = subprocess.Popen(
        [str(BIN_DIR / "graph-admin"), "view", *serve],
        env=isolated_env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        found = re.search(r"http://127\.0\.0\.1:(\d+)/ ", line)
        assert found and "--token-file" in line and token not in line, line
        conn = http.client.HTTPConnection("127.0.0.1", int(found.group(1)), timeout=5)
        conn.request("GET", f"/graph?t={token}")
        assert conn.getresponse().status == 200
    finally:
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0 and token not in out + err, err


def test_a_bad_token_file_is_refused(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources")
    short, empty = tmp_path / "short", tmp_path / "empty"
    short.write_text("s3cr3t-but-short\n")
    empty.write_text(" \n")
    for file, why in (
        (short, "shorter than 32 characters"),
        (empty, "is empty"),
        (tmp_path / "missing", "cannot be read"),
        (tmp_path, "cannot be read"),
    ):
        refused = run_cli(
            "graph-admin", "view", "--graph", "example_graph", "--token-file", str(file)
        )
        assert refused.returncode == 2, refused.stderr
        assert "usage.invalid_argument" in refused.stderr and why in refused.stderr
        assert "s3cr3t" not in refused.stderr + refused.stdout
