"""The hand-written `authoring-graph-skills` skill agrees with the template rules.

The skill names the blocks, the reserved generated files, the size budgets and the listing
limits of a graph skill; each must be the one the renderer and the publish check use.

Links and anchors are checked by ``tests/test_docs.py``, command lines by
``tests/skills/test_admin_skills.py``.
"""

import re

from support.skills import ADMIN_SKILLS_DIR

from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.skills import graph_skill, template, tree
from graph_cli.skills.graph_skill.description import START_SENTENCE
from graph_cli_admin.services_admin import skill_lint
from graph_cli_admin.services_admin.error_codes import ADMIN_CODES

SKILL = (ADMIN_SKILLS_DIR / "authoring-graph-skills" / "SKILL.md").read_text()
FLAT = " ".join(SKILL.split())  # the prose wraps
LINT = (ADMIN_SKILLS_DIR / "authoring-graph-skills" / "references" / "recipe_lint.md").read_text()
LINT_FLAT = " ".join(LINT.split())
ROW = re.compile(r"^\| `(\w+)` \|", re.MULTILINE)


def _number(value: int) -> str:
    return f"{value:,}"


def test_the_skill_describes_every_block_once() -> None:
    assert sorted(ROW.findall(SKILL)) == sorted(template.BLOCKS)


def test_the_skill_names_every_reserved_file() -> None:
    for name in (template.MODEL_FILE, template.FUNCTIONS_FILE, template.ACTIONS_FILE):
        assert f"`{name}`" in SKILL, name
    assert template.MODULES_DIR in SKILL


def test_the_skill_states_the_budgets_and_limits() -> None:
    assert f"at most {_number(graph_skill.L0_BASE)} bytes" in FLAT
    assert f"({_number(graph_skill.L0_FUNCTIONS)} more" in FLAT
    assert f"{_number(graph_skill.L0_ACTIONS)} more with actions" in FLAT
    assert f"at most {_number(graph_skill.CURATED_BUDGET)} bytes" in FLAT
    assert f"at most {tree.MAX_SKILL_LINES} lines" in FLAT
    assert f"at most {tree.MAX_TOTAL_BYTES // 1024} KB" in FLAT
    assert f"At most {tree.MAX_DESCRIPTION} characters" in FLAT
    assert START_SENTENCE in FLAT
    assert f"at most {tree.MAX_DESCRIPTION - len(START_SENTENCE) - 1} characters" in FLAT
    assert f"at most {graph_skill.FEATURED_LIMIT} of them" in FLAT
    assert f"at most {graph_skill.ACTIONS_FEATURED_LIMIT} of them" in FLAT


def test_the_skill_names_every_skill_warning() -> None:
    codes = {spec.code: spec for spec in ADMIN_CODES}
    for code in (
        "admin.skill_over_budget",
        "admin.skill_recipe_covered",
        "admin.skill_generic_example",
        "admin.skill_recipe_broken",
    ):
        assert code in codes and codes[code].category is ErrorCategory.WARNING, code
        assert f"`{code}`" in SKILL, code
    assert f"up to {skill_lint.MAX_LISTED} findings" in FLAT
    assert f"at most {skill_lint.MAX_LISTED}, `details.more` counts the rest" in LINT_FLAT
