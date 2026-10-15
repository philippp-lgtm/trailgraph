"""The committed skill 1 and the live inventory it is generated from (item 7 S2)."""

from pathlib import Path
from typing import Any

from harness import REPO_ROOT, CliRunner

from graph_cli.skills.app_skill import SKILL_NAME, render_app_skill
from support.app import app
from support.fixtures import UPDATE_GOLDENS

SKILL_DIR = REPO_ROOT / "skills" / SKILL_NAME


def live_inventory(run_cli: CliRunner) -> dict[str, Any]:
    """``graph describe commands --json``: the public contract the skill is made from."""
    outcome = app(run_cli, "describe", "commands")
    assert outcome.code == 0, outcome.envelope
    data: dict[str, Any] = outcome.data
    return data


def committed_files(directory: Path = SKILL_DIR) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): path.read_text()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def assert_skill_is_fresh(inventory: dict[str, Any], directory: Path = SKILL_DIR) -> None:
    """The committed files equal a fresh render; ``GRAPH_CLI_UPDATE_GOLDENS=1`` rewrites them."""
    rendered = render_app_skill(inventory)
    if UPDATE_GOLDENS:
        for stale in set(committed_files(directory)) - set(rendered):
            (directory / stale).unlink()
        for name, text in rendered.items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(text)
    committed = committed_files(directory)
    assert sorted(committed) == sorted(rendered), "run with GRAPH_CLI_UPDATE_GOLDENS=1"
    for name, text in rendered.items():
        assert committed[name] == text, f"{name} is stale; run with GRAPH_CLI_UPDATE_GOLDENS=1"
