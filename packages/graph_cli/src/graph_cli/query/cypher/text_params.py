"""Numbers meant as text: ``--param`` values bound as the text written, literals warned about.

``graph query --param n=2.1`` parses the value as JSON, so ``n`` is the number
2.1, and a number never equals the text ``"2.1"`` that a text field holds
(task numbers, version strings, codes). Two rules, both over a resolved query:

- ``text_bindings``: a value that parsed as a number (or a list of numbers)
  is bound as the text written when the parameter meets text only. That
  means every occurrence of the parameter is a comparison with a field
  (``=``, ``<>``, ``IN`` either way, ``STARTS WITH``, ``ENDS WITH``,
  ``CONTAINS``, an inline map; ordering comparisons and any other use, such
  as ``RETURN $n`` or arithmetic, count against it) and every field it is
  compared with has a text base type (``string``, ``text`` or ``iri``; enum
  and lifecycle values are strings). A parameter that is also used as a number
  anywhere stays a number. Fields of anonymous nodes and relationships are
  not looked up, so a parameter compared with one stays as given.
- ``number_for_text``: a number written into the query and compared with a
  text-only field is reported, never rewritten; the query means what it says.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.query.cypher.ast import Const, ListExpr, Parameter
from graph_cli.query.cypher.compare import Comparison, comparisons
from graph_cli.query.cypher.resolve import FieldUse, ResolvedQuery
from graph_cli.query.cypher.values import Value, is_number_text
from graph_cli.query.cypher.walk import parameters

TEXT_BASES: Final = frozenset({"string", "text", "iri"})
_ORDERING: Final = frozenset({"<", "<=", ">", ">="})


def text_bindings(resolved: ResolvedQuery, texts: Mapping[str, str]) -> dict[str, Value]:
    """The parameters of ``texts`` (name to the text as written) to bind as text.

    A scalar is bound as its text; a JSON list of numbers as a list of the
    numbers' texts. Only names the query uses and that meet the rules of the
    module docstring are returned; an empty result means nothing changes.
    """
    fields = {(f.variable, f.field): f for f in resolved.fields}
    occurrences: dict[str, int] = {}
    for use in parameters(resolved.query):
        occurrences[use.name] = occurrences.get(use.name, 0) + 1
    compared: dict[str, list[Comparison]] = {}
    for found in comparisons(resolved.query):
        if isinstance(found.value, Parameter):
            compared.setdefault(found.value.name, []).append(found)
    rebound: dict[str, Value] = {}
    for name, text in texts.items():
        uses = compared.get(name, [])
        if name not in occurrences or len(uses) != occurrences[name]:
            continue
        if any(use.op in _ORDERING or not _text_only(use, fields) for use in uses):
            continue
        value = _as_text(text)
        if value is not None:
            rebound[name] = value
    return rebound


def number_for_text(resolved: ResolvedQuery) -> list[tuple[str | None, str, JsonValue]]:
    """Numbers written in the query and compared with a text-only field, once each.

    Each item is the variable, the field and the literal (a number, or a list
    of numbers; decimals as text, as JSON output shows them).
    """
    fields = {(f.variable, f.field): f for f in resolved.fields}
    found: list[tuple[str | None, str, JsonValue]] = []
    for use in comparisons(resolved.query, literals=True):
        literal = _number_literal(use.value)
        if literal is None or not _text_only(use, fields):
            continue
        item = (use.variable, use.field, literal)
        if item not in found:
            found.append(item)
    return found


def _text_only(use: Comparison, fields: Mapping[tuple[str | None, str], FieldUse]) -> bool:
    if use.variable is None:
        return False
    field = fields.get((use.variable, use.field))
    return field is not None and bool(field.datatypes) and set(field.datatypes) <= TEXT_BASES


def _number_literal(value: Parameter | Const | ListExpr) -> JsonValue | None:
    if isinstance(value, Const):
        return _number_json(value.value)
    if isinstance(value, ListExpr) and value.items:
        listed: list[JsonValue] = []
        for item in value.items:
            number = _number_json(item.value) if isinstance(item, Const) else None
            if number is None:
                return None
            listed.append(number)
        return listed
    return None


def _number_json(value: object) -> JsonValue | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return None


def _as_text(raw: str) -> Value | None:
    """``raw`` (a JSON number or a list of numbers) with every number as written."""
    if not is_number_text(raw):
        return None
    loaded: object = json.loads(raw, parse_int=str, parse_float=str)
    if isinstance(loaded, list):
        return [str(i) for i in loaded]
    return raw.strip()
