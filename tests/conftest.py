"""Shared test harness.

CLIs are tested as processes, the way a user or console AI runs them (forked from a
preloaded launcher per program, ``support.launcher``, for speed). Every
run gets a throwaway home and XDG directories, so tests never read or write the
real user config, data or state. Every envelope a test sees is checked against the
declared output schemas (``support.output_schemas``) and every function and view answer of the
test process against the schema of its tool (``support.tool_answers``).
"""

from pathlib import Path

import pytest
from harness import CliRunner
from support import launcher

pytest_plugins = ["support.output_schemas", "support.tool_answers", "support.doc_claims"]


def pytest_sessionfinish(session: pytest.Session) -> None:
    launcher.close_all()


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    """Environment with a throwaway home and XDG dirs (``support.isolation.isolated``)."""
    from support.isolation import isolated  # after the plugins it imports are loaded

    return isolated(tmp_path)


@pytest.fixture
def run_cli(isolated_env: dict[str, str], tmp_path: Path) -> CliRunner:
    """Run an installed console script (``graph``, ``graph-admin``) in the isolated env."""
    from support.isolation import runner

    return runner(isolated_env, tmp_path)
