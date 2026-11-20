"""The ``relations`` block and the relation cells the other files share.

- ``relations``: the relations touching those types, each with its ``graph query`` pattern
  (``(:A)-[:rel]->(:B)``, from ``describe schema``) in place of its ends; inverse labels
  only in ``references/model.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.json_access import opt_str, req_obj, req_str
from graph_cli.skills.graph_skill.model import Model, strings
from graph_cli.skills.graph_skill.pieces import code, codes, term_summary

_POLICY_NOTES: Final = {
    "refuse": "retracting or superseding a target is refused while active edges point at it",
    "warn": "retracting or superseding a target warns while active edges point at it",
}


def render_relations(doc: Model) -> list[str]:
    """One line per relation: CURIE, its ``graph query`` pattern, cardinality and notes, then
    the summary. A list, not a table: a pattern with alternative labels (``(:A|B)``) stays as
    it is typed, where a table cell would have to escape the pipe."""
    lines = []
    for curie in doc.touching():
        relation = doc.relations[curie]
        ends = (
            code(doc.patterns[curie])
            if curie in doc.patterns
            else f"{relation_ends(relation, 'domain')} -> {relation_ends(relation, 'range')}"
        )
        facts = ", ".join(
            part
            for part in (
                cardinality_text(req_obj(relation, "cardinality")),
                relation_notes(relation, inverse=False),
            )
            if part
        )
        lines.append(f"- {code(curie)} {ends} ({facts}): {term_summary(relation)}")
    if not lines:
        return ["No relations."]
    return [
        "Each relation with its pattern for `graph query` (a label matches its subtypes too):",
        "",
        *lines,
    ]


def query_patterns(doc: Model, curies: Sequence[str]) -> list[str]:
    """The relations as ``graph query`` patterns, one per line."""
    lines = [doc.patterns[curie] for curie in curies if curie in doc.patterns]
    if not lines:
        return []
    return ["", "As patterns for `graph query`:", "", "```", *lines, "```"]


def relation_ends(relation: JsonObject, key: str) -> str:
    ends = strings(relation.get(key))
    return codes(ends, " or ") if ends else "any"


def cardinality_text(cardinality: JsonObject) -> str:
    out, into = req_str(cardinality, "out"), req_str(cardinality, "in")
    return f"out {out}" if into == "0..n" else f"out {out}, in {into}"


def relation_notes(relation: JsonObject, *, inverse: bool = True) -> str:
    """Characteristics, target policy, qualifiers and, unless left to level 2
    (``references/model.md``), the inverse label, which no command takes."""
    notes = list(strings(relation.get("characteristics")))
    policy = opt_str(relation, "on_target_inactive")
    if policy in _POLICY_NOTES:
        notes.append(_POLICY_NOTES[policy])
    qualifiers = strings(relation.get("qualifiers"))
    if qualifiers:
        notes.append(f"qualifiers {codes(qualifiers)}")
    label = opt_str(relation, "inverse_label")
    if label and inverse:
        notes.append(f"inverse {code(label)}")
    return ", ".join(notes)
