"""Judging a ``graph query`` recipe written into a graph skill (recipe lint, rules R3 and R5).

A recipe is the Cypher text of a ``graph query`` example and its ``--param`` texts, as the
maintainer wrote them for agents to adapt. ``judge_recipe`` reads it the way ``graph query``
would, against the model the graph will have, and answers one of three things:

- ``cover``: published functions answer the recipe (``cover.recipe_covers``, lint mode; the
  rules are in the docstring of ``kinetic.cover``), best first. The agent could run the
  function instead of learning the query.
- ``error``: the text does not parse or resolve against the model (a ``GraphCliError``): the
  recipe is broken for every agent that copies it.
- ``placeholder_parse_failure``: the text does not parse and holds a ``<placeholder>`` where
  Cypher has no place for one (``(n:<type>)``, ``n.<attribute>``, ``...``). That is a generic
  example of the command, not a recipe of this graph; the caller leaves it to the
  generic-example check and does not report it as broken.

Reading a recipe: a ``<word>`` outside a string literal becomes the parameter ``$word``. A
parameter without a value, or with a ``<placeholder>`` as value, gets a sample (a list holding
one text on the right of ``IN``, else a text); when the sample does not resolve (the other
shape would) the other shape is tried. A concrete ``--param`` value is read as ``graph query``
reads it (JSON when it parses) and a number that meets only text fields is bound as the text
written (``query.cypher.text_params``), so a recipe that ``graph query`` runs resolves here.

Pure: only the text, the model and the function entries.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.cover import (
    FunctionLintSignature,
    RecipeCover,
    function_lint_signature,
    recipe_covers,
    recipe_signature,
)
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.ast import Parameter, Query
from graph_cli.query.cypher.compare import comparisons
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import ResolvedQuery, resolve
from graph_cli.query.cypher.text_params import text_bindings
from graph_cli.query.cypher.values import Value, is_number_text, parameter_value
from graph_cli.query.cypher.walk import parameters

# A string literal or a quoted name (left alone), ``<word>`` (a parameter), or another ``<...>``
# or an ellipsis (placeholders for text Cypher has no place for).
_SCAN: Final = re.compile(
    r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`|<([a-z][a-z0-9_]*)>|(<[A-Za-z][^<>\s'"`]*>|\.\.\.|…)""",
    re.DOTALL,
)
_PLACEHOLDER_VALUE: Final = re.compile(r"^<[^<>]*>$")
_SAMPLE: Final = "x"


@dataclass(frozen=True, slots=True)
class RecipeVerdict:
    cover: tuple[RecipeCover, ...] = ()
    error: GraphCliError | None = None
    placeholder_parse_failure: bool = False


def function_pairs(
    entries: Sequence[FunctionEntry], model: CompiledModel
) -> tuple[tuple[FunctionEntry, FunctionLintSignature], ...]:
    """The functions with their lint signatures; one whose stored query no longer resolves
    against ``model`` is left out (checking the catalog reports it)."""
    pairs = []
    for entry in entries:
        try:
            pairs.append((entry, function_lint_signature(entry, model)))
        except GraphCliError:
            continue
    return tuple(pairs)


def judge_recipe(
    text: str,
    params: Mapping[str, str],
    model: CompiledModel,
    pairs: Sequence[tuple[FunctionEntry, FunctionLintSignature]],
) -> RecipeVerdict:
    """Read the recipe and find the functions that cover it (see the module docstring)."""
    cypher, placeholders = _parameterised(text)
    try:
        parsed = parse(cypher)
    except GraphCliError as exc:
        return RecipeVerdict(error=exc, placeholder_parse_failure=placeholders)
    try:
        resolved = _resolved(parsed, params, model)
    except GraphCliError as exc:
        return RecipeVerdict(error=exc)
    return RecipeVerdict(cover=recipe_covers(recipe_signature(resolved), pairs, model))


def _parameterised(text: str) -> tuple[str, bool]:
    """``<word>`` outside string literals as ``$word``; whether the text held a placeholder."""
    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        if match.group(1) is None and match.group(2) is None:
            return match.group(0)  # a string literal or a quoted name
        found = True
        return f"${match.group(1)}" if match.group(1) is not None else match.group(0)

    return _SCAN.sub(replace, text), found


def _resolved(query: Query, params: Mapping[str, str], model: CompiledModel) -> ResolvedQuery:
    lists = {
        c.value.name
        for c in comparisons(query)
        if isinstance(c.value, Parameter) and c.shape == "in_param"
    }
    values: dict[str, Value] = {}
    texts: dict[str, str] = {}
    sampled: set[str] = set()
    for use in parameters(query):
        if use.name in values:
            continue
        given = params.get(use.name)
        if given is None or _PLACEHOLDER_VALUE.match(given):
            if use.paging:
                values[use.name] = 0
            else:
                values[use.name] = [_SAMPLE] if use.name in lists else _SAMPLE
                sampled.add(use.name)
            continue
        values[use.name] = parameter_value(given)
        if is_number_text(given):
            texts[use.name] = given
    try:
        return _resolve_bound(query, values, texts, model)
    except GraphCliError as first:
        if not sampled:
            raise
        flipped = dict(values)
        for name in sampled:
            flipped[name] = _SAMPLE if name in lists else [_SAMPLE]
        try:
            return _resolve_bound(query, flipped, texts, model)
        except GraphCliError:
            raise first from None


def _resolve_bound(
    query: Query, values: Mapping[str, Value], texts: Mapping[str, str], model: CompiledModel
) -> ResolvedQuery:
    resolved = resolve(query, model, values)
    rebound = text_bindings(query, resolved, texts)
    return resolve(query, model, {**values, **rebound}) if rebound else resolved
