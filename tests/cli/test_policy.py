"""S: installed policies in both programs (contracts B6.13-B6.16, B5.15, B12.6).

Toy graphs and the neutral project names ``example_project`` and
``another_project`` only.
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def test_an_invalid_installed_policy_fails_closed(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create_graph(run_cli, "example_graph")
    assert app(run_cli, "graphs").code == 0
    policy = root / "policy" / "installation.json"
    policy.parent.mkdir()
    policy.write_text('{"schema": "graph_cli.policy/1", "rules": "everything"}\n')
    for args in (("graphs",), ("whoami",), ("find", "--graph", "example_graph")):
        outcome = app(run_cli, *args)
        assert outcome.code == 5, (args, outcome.envelope)
        assert outcome.error["code"] == "policy.invalid"
        assert outcome.error["details"]["file"] == "policy/installation.json"
    # graph-admin decides without the broken file, so the maintainer can replace it.
    listed = admin(run_cli, "graph", "list")
    assert listed.code == 0, listed.envelope
    assert "admin.policy_fallback" in listed.warning_codes
