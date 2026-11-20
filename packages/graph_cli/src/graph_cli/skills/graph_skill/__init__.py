"""Generated graph skills: a curated template rendered with the graph's live model.

``render_graph_skill`` is a pure function of the template
(``graph_cli.skills.template``), the ``graph_cli.model/1`` documents of the
graph (``{types, type: {curie: document}, relations, schema}``, what ``graph
describe`` prints) and the graph block ``{id, name, title, language,
alias_prefixes}``. Each block marker in ``SKILL.md`` becomes a generated
block; ``references/model.md`` describes every type of the model. Only the
model and the manifest go in, never graph data (no counts, no records).

A stamp after the frontmatter names the graph, the ontology release and the
template hash, never the program version or a time, so a new program with
the same render installs the same files.

Blocks (each one optional):


``measure`` sizes the rendered ``SKILL.md`` against the level 0 budget of every graph
(``L0_BUDGET`` parts, single-module graphs included) and its curated text against
``CURATED_BUDGET``; ``graph-admin`` warns when either is exceeded and refuses nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.ports.catalog import GraphEntry, skill_hash
from graph_cli.skills.graph_skill.catalogs import (
    ACTIONS_FEATURED_LIMIT,
    FEATURED_LIMIT,
    actions_file,
    functions_file,
    render_actions,
    render_functions,
)
from graph_cli.skills.graph_skill.model import Model
from graph_cli.skills.graph_skill.modules import module_page, render_modules
from graph_cli.skills.graph_skill.naming import render_names
from graph_cli.skills.graph_skill.reference import model_file, render_model_pointer
from graph_cli.skills.graph_skill.relations import render_relations
from graph_cli.skills.graph_skill.start import render_start
from graph_cli.skills.graph_skill.type_blocks import render_constraints, render_types
from graph_cli.skills.template import (
    ACTIONS_FILE,
    FUNCTIONS_FILE,
    MODEL_FILE,
    MODULES_DIR,
    Template,
    parse_template,
)
from graph_cli.skills.tree import SKILL_FILE, Problem, SkillFiles, validate

__all__ = ["ACTIONS_FEATURED_LIMIT", "FEATURED_LIMIT"]

GENERATOR: Final = "graph_cli.skills"
# Level 0 of the disclosure levels: the most bytes ``SKILL.md`` of a graph's skill should
# have (about 5000 tokens at 4 bytes a token, the size the Agent Skills guidance suggests for
# a skill body), single-module graphs included. A test holds ``L0_BASE`` for 50 modules of any
# size; the blocks of a modular graph grow with the modules only, those of a single-module
# graph with its types (past the budget, split the model into modules). A graph with
# functions may use ``L0_FUNCTIONS`` more for the ``functions`` block (``FEATURED_LIMIT``
# lines of a name, a signature and a 100-character summary, two lines of pointers) and the
# ``Functions`` column;
# a graph with actions another ``L0_ACTIONS`` for the ``actions`` block (``ACTIONS_FEATURED_LIMIT``
# lines, the pointers and the dry-run rule) and the ``Actions`` column. ``L0_BUDGET``, the most
# for any graph, is held by a test for 50 modules of 20 functions and 10 actions each.
# ``graph-admin`` warns at render, publish and push when a skill exceeds the budget of its
# graph (``measure``); it refuses nothing.
L0_BASE: Final = 20_000
L0_FUNCTIONS: Final = 2_500
L0_ACTIONS: Final = 2_000
L0_BUDGET: Final = L0_BASE + L0_FUNCTIONS + L0_ACTIONS
# The curated text of ``SKILL.md`` (outside the frontmatter and the block markers): when to
# use the graph and the rules no schema says. Workflow detail and more recipes go into
# curated ``references/*.md``, read when the work needs them.
CURATED_BUDGET: Final = 6_000


def render_graph_skill(
    template: Template, model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]
) -> dict[str, str]:
    """``{relative path: content}`` of the skill; ``ValueError`` on documents it cannot read."""
    doc = Model.of(model, graph, _used(template))
    stamp = _stamp(doc, template)
    blocks = {name: render(doc) for name, render in _BLOCKS.items()}
    lines = template.lines
    for index, name in reversed(template.blocks):
        lines[index : index + 1] = blocks[name]
    end = _frontmatter_end(lines)
    lines[end:end] = ["", stamp]
    return {
        **template.files,
        SKILL_FILE: "\n".join(lines),
        MODEL_FILE: model_file(doc, stamp),
        **{
            f"{MODULES_DIR}{module_id}.md": module_page(doc, module_id, stamp)
            for module_id in (doc.pages if doc.modular else {})
        },
        **(
            {FUNCTIONS_FILE: functions_file(doc, stamp)}
            if doc.functions and not doc.modular
            else {}
        ),
        **({ACTIONS_FILE: actions_file(doc, stamp)} if doc.actions and not doc.modular else {}),
    }


@dataclass(frozen=True, slots=True)
class Size:
    """The rendered ``SKILL.md`` against its budgets, in bytes of UTF-8."""

    skill: int
    budget: int  # ``L0_BASE``, plus the allowances of a graph with functions or actions
    curated: int  # the template's ``SKILL.md`` outside the frontmatter and the markers
    curated_budget: int
    blocks: Mapping[str, int]  # each generated block of the template

    @property
    def over(self) -> bool:
        return self.skill > self.budget or self.curated > self.curated_budget

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
        }


def measure(
    template: Template, model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]
) -> Size:
    """``Size`` of the skill ``render_graph_skill`` makes of ``template``."""
    doc = Model.of(model, graph, _used(template))
    rendered = render_graph_skill(template, model, graph)[SKILL_FILE]
    lines = template.lines
    marked = {index for index, _ in template.blocks}
    body = [line for index, line in enumerate(lines) if index not in marked]
    curated = "\n".join(body[_frontmatter_end(body) :]).strip()
    return Size(
        skill=_bytes(rendered),
        budget=L0_BASE
        + (L0_FUNCTIONS if doc.functions else 0)
        + (L0_ACTIONS if doc.actions else 0),
        curated=_bytes(curated),
        curated_budget=CURATED_BUDGET,
        blocks={name: _bytes("\n".join(_BLOCKS[name](doc))) for _, name in template.blocks},
    )


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _used(template: Template) -> frozenset[str]:
    return frozenset(name for _, name in template.blocks)


def render_checked(
    files: SkillFiles,
    model: Mapping[str, JsonValue],
    graph: Mapping[str, JsonValue],
    *,
    name: str,
    directory: str | None = None,
) -> tuple[dict[str, bytes] | None, list[Problem]]:
    """The rendered skill of a template, or ``None`` with every broken rule: the tree rules
    of the template, the template rules, then the tree rules of the result (size, and
    leaks a model text may bring in)."""
    problems = validate(files, name=name, directory=directory)
    template, template_problems = parse_template(files)
    problems += template_problems
    if problems or template is None:
        return None, problems
    rendered = {
        path: text.encode("utf-8")
        for path, text in sorted(render_graph_skill(template, model, graph).items())
    }
    problems = validate(rendered, name=name)
    return (None, problems) if problems else (rendered, [])


def graph_block(entry: GraphEntry) -> dict[str, JsonValue]:
    """The ``graph`` input of ``render_graph_skill``: what the manifest says of the graph."""
    return {
        "id": str(entry.id),
        "name": entry.name,
        "title": entry.title,
        "language": entry.language,
        "alias_prefixes": {
            str(curie): entry.alias_prefixes[curie] for curie in sorted(entry.alias_prefixes)
        },
    }


def _stamp(doc: Model, template: Template) -> str:
    digest = skill_hash({path: text.encode("utf-8") for path, text in template.files.items()})
    return (
        f"<!-- Generated by {GENERATOR} for graph {doc.name} from its published template"
        f" ({digest}) and the ontology {doc.bundle} {doc.version}. Do not edit this file;"
        " the template is edited and published again. -->"
    )


def _frontmatter_end(lines: list[str]) -> int:
    """The index after the closing ``---`` (``tree.validate`` requires the block)."""
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return index + 1
    raise ValueError("the template has no closed frontmatter")


# ---- blocks ------------------------------------------------------------------------


_BLOCKS: Final[Mapping[str, Callable[[Model], list[str]]]] = {
    "start": render_start,
    "modules": render_modules,
    "types": render_types,
    "relations": render_relations,
    "constraints": render_constraints,
    "names": render_names,
    "model": render_model_pointer,
    "functions": render_functions,
    "actions": render_actions,
}


# ---- references/model.md -----------------------------------------------------------
