"""The ``types`` and ``constraints`` blocks: entity types with their fields and states, and
the rules that guard writes.

- ``types``: the entity types (types with an alias prefix; without any, every
  concrete type) with their supertypes, fields (label roles named), enum values inline
  (F11), and states. A field the model marks (``markings``, field visibility plan F7) shows
  ``marked <curie>`` with its markings joined by ``+``, here and in the ``Flags`` column of
  the model file; the documents of a reader hold only the fields it sees, so a graph without
  markings, or a reader without a marked field, renders as before.
- ``constraints``: transition guards, write-once fields (left out when the template has
  ``types``, which marks them) and rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.ids import format_alias
from graph_cli.kernel.json_access import opt_str, req_str
from graph_cli.skills.graph_skill.model import Model, objects, strings
from graph_cli.skills.graph_skill.pieces import (
    code,
    codes,
    md_table,
    plain,
    term_summary,
    value_code,
)
from graph_cli.skills.template import MODEL_FILE

ROLE_NAMES: Final[Mapping[str, str]] = {
    "label": "main name",
    "alt_label": "other names",
    "hidden_label": "hidden names, matched but never shown",
}
ROLE_MARKS: Final[Mapping[str, str]] = {  # the label roles as the ``types`` block names them
    "label": "main name",
    "alt_label": "other names",
    "hidden_label": "hidden names",
}
WHEN: Final[Mapping[str, str]] = {"write": "checked on every write", "batch": "checked in batch"}


def render_types(doc: Model) -> list[str]:
    rows = []
    for curie in doc.listed:
        body = doc.types[curie]
        alias = doc.prefixes.get(curie)
        name = code(curie) + (f" ({code(format_alias(alias, 1))})" if alias else "")
        fields = [_field(f) for f in ordered_fields(body) if f.get("kind") != "lifecycle"]
        ancestors = strings(body.get("ancestors"))  # relations of a supertype apply too
        what = term_summary(body)
        what += f" Is a {codes(ancestors, ' and ')}." if ancestors else ""
        rows.append(
            [
                name,
                what.strip(),
                "; ".join(fields) or "none",
                _states(body),
            ]
        )
    lines = md_table(["Type", "What it is", "Fields", "States"], rows)
    others = [curie for curie in doc.types if curie not in doc.listed]
    if others:
        lines += [
            "",
            f"Other types of the model, described in `{MODEL_FILE}`: {codes(others)}.",
        ]
    return lines


def ordered_fields(body: JsonObject) -> list[JsonObject]:
    """Names first (main, other, hidden), then required fields, then the rest; by name."""
    roles = list(ROLE_NAMES)

    def key(field: JsonObject) -> tuple[int, int, str]:
        role = field.get("role")
        rank = roles.index(role) if isinstance(role, str) and role in roles else len(roles)
        return (rank, 0 if field.get("required") is True else 1, req_str(field, "name"))

    return sorted(objects(body, "fields"), key=key)


def _field(field: JsonObject) -> str:
    notes = []
    base = req_str(field, "base")
    if field.get("kind") != "enum" and base not in ("string", "text"):
        notes.append(base)
    for flag, text in (("required", "required"), ("many", "many"), ("unique", "unique")):
        if field.get(flag) is True:
            notes.append(text)
    if field.get("mutability") == "immutable":
        notes.append("write-once")
    if field.get("default") is not None:
        notes.append(f"default {value_code(field['default'])}")
    values = strings(field.get("values"))
    if values:
        notes.append(f"one of {codes(values)}")
    role = field.get("role")
    if isinstance(role, str) and role in ROLE_MARKS:
        notes.append(ROLE_MARKS[role])
    notes += marked_notes(field)
    name = code(req_str(field, "name"))
    return f"{name} ({', '.join(notes)})" if notes else name


def marked_notes(field: JsonObject) -> list[str]:
    """The mark of a marked field, ``marked ex:personal`` (conjunctive markings joined by
    ``+``); nothing for an unmarked one."""
    markings = strings(field.get("markings"))
    return [f"marked {'+'.join(markings)}"] if markings else []


def _states(body: JsonObject) -> str:
    parts = []
    for lifecycle in objects(body, "lifecycles"):
        pairs = [(req_str(t, "from"), req_str(t, "to")) for t in objects(lifecycle, "transitions")]
        shown: list[str] = []
        one_way: dict[str, list[str]] = {}  # to -> from states, in model order
        for a, b in pairs:
            if (b, a) not in pairs:
                one_way.setdefault(b, []).append(a)
            elif (a, b) < (b, a):
                shown.append(f"{code(a)} <-> {code(b)}")
        shown += [f"{codes(froms, ' or ')} -> {code(to)}" for to, froms in one_way.items()]
        initial = codes(strings(lifecycle.get("initial")))
        terminal = codes(strings(lifecycle.get("terminal")))
        text = f"{code(lifecycle_attr(lifecycle))}: " + ", ".join(shown)
        text += f"; initial {initial}" if initial else ""
        text += f"; terminal {terminal}" if terminal else ""
        parts.append(text)
    return "; ".join(parts) or "none"


def lifecycle_attr(lifecycle: JsonObject) -> str:
    return opt_str(lifecycle, "attr") or "status"


def render_constraints(doc: Model) -> list[str]:
    lines = []
    for curie in doc.listed:
        for lifecycle in objects(doc.types[curie], "lifecycles"):
            guarded: dict[tuple[str, str, str], list[str]] = {}  # (to, id, summary) -> froms
            for transition in objects(lifecycle, "transitions"):
                for guard in objects(transition, "guards"):
                    key = (
                        req_str(transition, "to"),
                        req_str(guard, "id"),
                        plain(opt_str(guard, "summary")),
                    )
                    guarded.setdefault(key, []).append(req_str(transition, "from"))
            for (to, guard_id, summary), froms in guarded.items():
                lines.append(
                    f"- {code(curie)} {code(lifecycle_attr(lifecycle))} {codes(froms, ' or ')} ->"
                    f" {code(to)}: {summary} (guard {code(guard_id)})"
                )
    for curie in doc.listed if "types" not in doc.blocks else ():  # ``types`` marks them
        once = [
            req_str(f, "name")
            for f in objects(doc.types[curie], "fields")
            if f.get("mutability") == "immutable"
        ]
        if once:
            lines.append(
                f"- Write-once on {code(curie)} (set at create, never changed): {codes(once)}"
            )
    rules: dict[str, tuple[JsonObject, list[str]]] = {}
    for curie in doc.listed:
        for rule in objects(doc.types[curie], "rules"):
            rules.setdefault(req_str(rule, "id"), (rule, []))[1].append(curie)
    for rule_id, (rule, on) in sorted(rules.items()):
        when = req_str(rule, "when")
        lines.append(
            f"- Rule {code(rule_id)} on {codes(on)} ({req_str(rule, 'severity')},"
            f" {WHEN.get(when, f'when {when}')}): {term_summary(rule)}"
        )
    return lines or ["No transition guards, write-once fields or rules."]
