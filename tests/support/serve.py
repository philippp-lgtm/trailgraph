"""A real ``graph-server`` subprocess for S and P tests (contracts B14.9)."""

import http.client
import json
import re
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness import BIN_DIR

READY = re.compile(r"listening on http://([^:]+):(\d+)")


@dataclass
class Running:
    process: subprocess.Popen[str]
    port: int
    log: list[str] = field(default_factory=list)

    def request(
        self, method: str, path: str, *, token: str | None = None, body: Any = None
    ) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, json.loads(payload) if payload else None


@contextmanager
def graph_server(env: Mapping[str, str], cwd: Path, *args: str) -> Iterator[Running]:
    process = subprocess.Popen(
        [str(BIN_DIR / "graph-server"), "serve", "--port", "0", *args],
        env=dict(env),
        cwd=cwd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )
    assert process.stderr is not None
    first = process.stderr.readline()
    match = READY.search(first)
    assert match is not None, first + process.stderr.read()
    running = Running(process, int(match.group(2)))

    def drain() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            running.log.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        yield running
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=30)
        reader.join(timeout=5)


def client_env(
    base: Mapping[str, str], home: Path, port: int, token: str | None, *, extra: str = ""
) -> dict[str, str]:
    """A second machine: its own home and config in remote mode, pointing at the server."""
    config_home = home / ".config"
    (config_home / "graph_cli").mkdir(parents=True, exist_ok=True)
    (config_home / "graph_cli" / "config.toml").write_text(
        "format_version = 1\n"
        f'[runtime]\nmode = "remote"\nendpoint = "http://127.0.0.1:{port}"\n' + extra
    )
    env = {
        **base,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
    }
    if token is not None:
        env["GRAPH_CLI_TOKEN"] = token
    return env
