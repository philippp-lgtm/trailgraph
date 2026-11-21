"""The recipe lint of a graph skill template: three advisory warnings, never a refusal.

A curated template that teaches what the tool already carries makes agents read and follow
more than they need. The lint looks at the text the maintainer wrote (``Template.curated``)
and warns, once per code and command, with up to ``MAX_LISTED`` findings in ``details``:

- ``admin.skill_recipe_covered``: a ``graph query`` recipe that a published function answers
  (``graph_cli.kinetic.cover``, lint mode). The finding names the call that replaces it.
- ``admin.skill_generic_example``: a fenced ``graph <command>`` example that names no type,
  relation, field, state, enum value, function or action of the graph
  (``graph_cli.skills.examples``): skill 1 and the ``start`` block hold it.
- ``admin.skill_recipe_broken``: a ``graph query`` recipe that does not parse or resolve against
  the model the graph will have. Warned, not refused: refusing would block a live template on
  its next push (tighten it later, like the start sentence). A recipe with a ``<placeholder>``
  where Cypher has none is a generic example, not a broken recipe.

Runs where the skill is checked (``render``, ``publish``, ``graph push``), server-side in
remote mode, and for a push that publishes functions only, against the published template
(``skill.lint_files``). Never at install, and a static skill is not linted.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.skill_marker import graph_skill_name
from graph_cli.kinetic.cover import RecipeCover
from graph_cli.kinetic.entries import load_entries
from graph_cli.kinetic.recipes import function_pairs, judge_recipe
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.skills.examples import (
    CommandExample,
    QueryRecipe,
    command_examples,
    generic_examples,
    model_terms,
    query_recipes,
)
from graph_cli.skills.template import Template

MAX_LISTED: Final = 20  # findings in ``details``; ``more`` counts the rest
SHOWN: Final = 3  # findings the message names
MAX_COMMAND: Final = 120  # characters of a generic example in a finding


def lint(
    entry: GraphEntry,
    template: Template,
    described: Mapping[str, JsonValue],
    model: CompiledModel,
    functions: JsonObject | None,
    inventory: Mapping[str, JsonValue],
) -> tuple[WarningRecord, ...]:
    """The lint warnings of ``template`` for graph ``entry`` once ``model`` and the compiled
    function catalog ``functions`` (``None``: no functions) are published. ``described`` is
    ``describe_model`` of the same."""
    examples = command_examples(template)
    recipes = query_recipes(examples, inventory)
    pairs = function_pairs(load_entries(functions) if functions is not None else (), model)
    covered: list[JsonValue] = []
    broken: list[JsonValue] = []
    for recipe in recipes:
        verdict = judge_recipe(recipe.text, recipe.params, model, pairs)
        if verdict.cover:
            covered.append(_covered(entry, recipe, verdict.cover[0]))
        elif verdict.error is not None and not verdict.placeholder_parse_failure:
            broken.append(_broken(recipe, verdict.error))
    generic: list[JsonValue] = [
        _generic(example)
        for example in generic_examples(examples, model_terms(described), entry.name, inventory)
    ]
    name = graph_skill_name(entry.name)
    return tuple(
        record
        for record in (
            _record(
                "admin.skill_recipe_covered",
                entry,
                name,
                covered,
                lambda first, count: (
                    f"{count} query recipe(s) in {name} are answered by a published function:"
                    f" {first}. Point the text at the function or drop the recipe: agents find"
                    " functions with `graph describe search`, and a recipe teaches the query"
                    " where the function carries it"
                ),
            ),
            _record(
                "admin.skill_generic_example",
                entry,
                name,
                generic,
                lambda first, count: (
                    f"{count} example(s) in {name} name no type, relation, field, state, enum"
                    f" value, function or action of the graph: {first}. The graph-cli skill or"
                    " the start block has it; keep an example only if it teaches something about"
                    " this graph"
                ),
            ),
            _record(
                "admin.skill_recipe_broken",
                entry,
                name,
                broken,
                lambda first, count: (
                    f"{count} query recipe(s) in {name} fail against the model of the graph:"
                    f" {first}. Fix them; every agent that copies one gets the error"
                ),
            ),
        )
        if record is not None
    )


def _record(
    code: str,
    entry: GraphEntry,
    skill: str,
    findings: list[JsonValue],
    message: Callable[[str, int], str],
) -> WarningRecord | None:
    if not findings:
        return None
    shown = "; ".join(_where(f) for f in findings[:SHOWN])
    more = f"; and {len(findings) - SHOWN} more" if len(findings) > SHOWN else ""
    return WarningRecord(
        code,
        message(shown + more, len(findings)),
        {
            "graph": entry.name,
            "skill": skill,
            "findings": findings[:MAX_LISTED],
            "more": max(len(findings) - MAX_LISTED, 0),
        },
    )


def _where(finding: JsonValue) -> str:
    """``<file> line <n>: <what>`` of one finding."""
    assert isinstance(finding, dict)
    what = finding.get("call") or finding.get("command") or finding.get("message")
    return f"{finding['file']} line {finding['line']}: {what}"


def _covered(entry: GraphEntry, recipe: QueryRecipe, cover: RecipeCover) -> JsonValue:
    return {
        "file": recipe.path,
        "line": recipe.line,
        "function": cover.function,
        "call": _call(entry.name, cover),
    }


def _call(graph: str, cover: RecipeCover) -> str:
    """The ``graph run`` line that replaces the recipe; ``<ref>`` and ``<value>`` where the
    recipe gives no constant."""
    words = [f"graph run {cover.function} --graph {graph}"]
    words += [f"--param {name}=<ref>" for name in cover.ref_params]
    for name, text in cover.value_params:
        words.append(f"--param {name}={shlex.quote(text) if text is not None else '<value>'}")
    return " ".join(words)


def _broken(recipe: QueryRecipe, error: GraphCliError) -> JsonValue:
    return {
        "file": recipe.path,
        "line": recipe.line,
        "code": error.code,
        "message": f"{error.code}: {error.message}",
    }


def _generic(example: CommandExample) -> JsonValue:
    command = shlex.join(["graph", *(" ".join(word.split()) for word in example.words)])
    if len(command) > MAX_COMMAND:
        command = command[: MAX_COMMAND - 3] + "..."
    return {"file": example.path, "line": example.line, "command": command}
