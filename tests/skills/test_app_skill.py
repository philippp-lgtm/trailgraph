"""G: the committed skill 1 is the render of the live app inventory (B11.9, item 7 S2, S4)."""

from harness import CliRunner
from support.skills import SKILL_DIR, assert_skill_is_fresh, live_inventory

from graph_cli.skills.app_skill import SKILL_NAME


def test_committed_skill_is_the_render_of_the_live_inventory(run_cli: CliRunner) -> None:
    assert SKILL_DIR.name == SKILL_NAME
    assert_skill_is_fresh(live_inventory(run_cli))
