"""Multi-process test helpers (P tests).

Workers are separate Python processes: ``python -m support.workers.<name>``
with their arguments as one JSON document on argv and their result as one
JSON line on stdout. A ``go`` file in a shared directory releases all
workers at once.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness import REPO_ROOT

TESTS_DIR = REPO_ROOT / "tests"


def spawn(worker: str, args: Mapping[str, Any]) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(TESTS_DIR)}
    return subprocess.Popen(
        [sys.executable, "-m", f"support.workers.{worker}", json.dumps(args)],
        cwd=TESTS_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def result(proc: subprocess.Popen[str], *, timeout: float = 60) -> dict[str, Any]:
    """Wait for a worker and return its JSON result line; fail on a non-zero exit."""
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, f"worker exited {proc.returncode}: {err}"
    lines = out.strip().splitlines()
    assert lines, f"worker printed nothing: {err}"
    data = json.loads(lines[-1])
    assert isinstance(data, dict)
    return data


def wait_for(path: Path, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path.name}"
        time.sleep(0.005)


def release(path: Path) -> None:
    """Create a signal file (barrier ``go`` or a release flag)."""
    path.touch()
