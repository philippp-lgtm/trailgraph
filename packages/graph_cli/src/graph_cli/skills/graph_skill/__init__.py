"""Generated graph skills: a curated template rendered with the graph's live model.

``render_graph_skill`` is a pure function of the template
(``graph_cli.skills.template``), the ``graph_cli.model/1`` documents of the
graph (``{types, type: {curie: document}, relations, schema}``, what ``graph
describe`` prints) and the graph block ``{id, name, title, language,
alias_prefixes}``. Each block marker in ``SKILL.md`` becomes a generated
block; ``references/model.md`` describes every type of the model. Only the
model and the manifest go in, never graph data (no counts, no records).

One module per part:

- ``render``: the renderer (``render_graph_skill``, ``render_checked``,
  ``graph_block``) and ``BLOCKS``, the blocks in the order they render.
- ``budget``: the level 0 budgets and ``measure``.
- ``model``: reads the model documents.
- ``pieces``: the Markdown pieces the blocks share.
- The blocks (each one optional), one module per section of the generated skill:
  ``start``, ``modules`` (also the pages of a modular graph), ``type_blocks``
  (``types``, ``constraints``), ``relations``, ``naming`` (``names``), ``reference``
  (``model``, ``references/model.md``) and ``catalogs`` (``functions``, ``actions``).
"""

from __future__ import annotations

from graph_cli.skills.graph_skill.budget import (
    CURATED_BUDGET,
    L0_ACTIONS,
    L0_BASE,
    L0_BUDGET,
    L0_FUNCTIONS,
    Size,
    measure,
)
from graph_cli.skills.graph_skill.catalogs import ACTIONS_FEATURED_LIMIT, FEATURED_LIMIT
from graph_cli.skills.graph_skill.render import graph_block, render_checked, render_graph_skill

__all__ = [
    "ACTIONS_FEATURED_LIMIT",
    "CURATED_BUDGET",
    "FEATURED_LIMIT",
    "L0_ACTIONS",
    "L0_BASE",
    "L0_BUDGET",
    "L0_FUNCTIONS",
    "Size",
    "graph_block",
    "measure",
    "render_checked",
    "render_graph_skill",
]
