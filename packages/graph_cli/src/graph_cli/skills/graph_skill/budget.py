"""The level 0 budgets of a generated skill and the size check against them.

``measure`` sizes the rendered ``SKILL.md`` against the level 0 budget of every graph
(``L0_BUDGET`` parts, single-module graphs included) and its curated text against
``CURATED_BUDGET``; ``graph-admin`` warns when either is exceeded and refuses nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.skills.graph_skill.description import (
    START_SENTENCE,
    description_of,
    start_sentence_gap,
)
from graph_cli.skills.graph_skill.model import Model
from graph_cli.skills.graph_skill.render import BLOCKS, frontmatter_end, render_graph_skill, used
from graph_cli.skills.template import Template
from graph_cli.skills.tree import SKILL_FILE

# Level 0 of the disclosure levels: the most bytes ``SKILL.md`` of a graph's skill should
# have (about 5000 tokens at 4 bytes a token, the size the Agent Skills guidance suggests for
# a skill body), single-module graphs included. A test holds ``L0_BASE`` for 50 modules of any
# size; the blocks of a modular graph grow with the modules only, those of a single-module
# graph with its types (past the budget, split the model into modules). A graph with
# functions may use ``L0_FUNCTIONS`` more for the ``functions`` block (``FEATURED_LIMIT``
# lines of a name, a signature and a 100-character summary, two lines of pointers);
# a graph with actions another ``L0_ACTIONS`` for the ``actions`` block (``ACTIONS_FEATURED_LIMIT``
# lines, the pointers and the dry-run rule). No count, column or list per module of either is in
# the skill any more (``graph start`` and ``describe`` give them). ``L0_BUDGET``, the most
# for any graph, is held by a test for 50 modules of 20 functions and 10 actions each.
# ``graph-admin`` warns at render, publish and push when a skill exceeds the budget of its
# graph (``measure``); it refuses nothing.
L0_BASE: Final = 20_000
L0_FUNCTIONS: Final = 2_400
L0_ACTIONS: Final = 1_950
L0_BUDGET: Final = L0_BASE + L0_FUNCTIONS + L0_ACTIONS
# The curated text of ``SKILL.md`` (outside the frontmatter and the block markers): when to
# use the graph and the rules no schema says. Workflow detail and more recipes go into
# curated ``references/*.md``, read when the work needs them.
CURATED_BUDGET: Final = 6_000


@dataclass(frozen=True, slots=True)
class Size:
    """The rendered ``SKILL.md`` against its budgets, in bytes of UTF-8."""

    skill: int
    budget: int  # ``L0_BASE``, plus the allowances of a graph with functions or actions
    curated: int  # the template's ``SKILL.md`` outside the frontmatter and the markers
    curated_budget: int
    blocks: Mapping[str, int]  # each generated block of the template
    description: int = 0  # characters of the curated description
    description_gap: str | None = None  # why the renderer cannot add the ``graph start`` sentence

    @property
    def description_over(self) -> bool:
        return self.description_gap is not None

    @property
    def over(self) -> bool:
        return (
            self.skill > self.budget or self.curated > self.curated_budget or self.description_over
        )

    def to_json(self) -> JsonObject:
        return {
            "skill_bytes": self.skill,
            "budget": self.budget,
            "curated_bytes": self.curated,
            "curated_budget": self.curated_budget,
            "blocks": {
                name: size
                for name, size in sorted(self.blocks.items(), key=lambda kv: (-kv[1], kv[0]))
            },
            **(
                {
                    "description_chars": self.description,
                    "description_gap": self.description_gap,
                    "start_sentence": START_SENTENCE,
                }
                if self.description_gap
                else {}
            ),
        }


def measure(
    template: Template, model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]
) -> Size:
    """``Size`` of the skill ``render_graph_skill`` makes of ``template``."""
    doc = Model.of(model, graph, used(template))
    rendered = render_graph_skill(template, model, graph)[SKILL_FILE]
    lines = template.lines
    marked = {index for index, _ in template.blocks}
    body = [line for index, line in enumerate(lines) if index not in marked]
    curated = "\n".join(body[frontmatter_end(body) :]).strip()
    description = description_of(lines) or ""
    return Size(
        skill=_bytes(rendered),
        budget=L0_BASE
        + (L0_FUNCTIONS if doc.functions else 0)
        + (L0_ACTIONS if doc.actions else 0),
        curated=_bytes(curated),
        curated_budget=CURATED_BUDGET,
        blocks={name: _bytes("\n".join(BLOCKS[name](doc))) for _, name in template.blocks},
        description=len(description),
        description_gap=start_sentence_gap(lines),
    )


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))
