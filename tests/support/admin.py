"""Running graph-admin as a subprocess and reading its JSON envelope (S tests)."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import CliRunner


@dataclass(frozen=True)
class Outcome:
    code: int
    envelope: dict[str, Any]
    stderr: str

    @property
    def data(self) -> Any:
        return self.envelope["data"]

    @property
    def error(self) -> dict[str, Any]:
        error = self.envelope["error"]
        assert isinstance(error, dict), self.envelope
        return error

    @property
    def warning_codes(self) -> list[str]:
        return [w["code"] for w in self.envelope["warnings"]]


def parse(result: subprocess.CompletedProcess[str]) -> Outcome:
    assert result.stdout.count("\n") == 1, (result.stdout, result.stderr)
    return Outcome(result.returncode, json.loads(result.stdout), result.stderr)


def admin(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    """``graph-admin <args> --json``; stderr must stay empty in JSON mode (B9.1)."""
    outcome = parse(run_cli("graph-admin", *args, "--json", env=env))
    assert outcome.stderr == "", outcome.stderr
    return outcome


def data_root(env: dict[str, str]) -> Path:
    """The default data root of the isolated env (B12.1 step 4)."""
    return (Path(env["XDG_DATA_HOME"]) / "graph_cli").resolve()


def init(run_cli: CliRunner, env: dict[str, str]) -> Path:
    assert admin(run_cli, "init").code == 0
    return data_root(env)
