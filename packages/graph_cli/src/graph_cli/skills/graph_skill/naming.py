"""The ``names`` block: the alias format and what ``graph lookup`` matches.

- ``names``: the live alias format (F12; with ``types`` in the template one example, the
  table has the rest), what ``graph lookup`` matches per
  type (label roles; with ``types`` in the template, a pointer to the roles named there) and
  the rule to ask when the match is not unique (F24). On a modular
  graph it names one alias and points at the module pages and ``describe type`` for the
  prefixes and name fields, so its size does not grow with the types.
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.ids import ALIAS_DIGITS, format_alias
from graph_cli.kernel.json_access import req_str
from graph_cli.skills.graph_skill.model import Model
from graph_cli.skills.graph_skill.pieces import code
from graph_cli.skills.graph_skill.type_blocks import ROLE_MARKS, ROLE_NAMES, ordered_fields


def render_names(doc: Model) -> list[str]:
    """Aliases and what ``lookup`` matches. A modular graph gets one example alias and the
    places that list the rest, so the block does not grow with the types (L0 budget)."""
    lines = []
    if doc.prefixes:
        prefix = next(iter(doc.prefixes.values()))
        first = code(format_alias(prefix, 1))
        padding = (
            f"The number is zero-padded to {ALIAS_DIGITS} digits ({first}, never"
            f" {code(prefix + '-1')}) and grows past 9999. A full id works everywhere; on"
            " the command line 8 or more trailing hex digits of it work too."
        )
        if doc.modular:
            lines.append(
                f"- Aliases such as {first} ({code(next(iter(doc.prefixes)))}) start with the"
                " prefix of the entity's type; the module pages list the prefix of every type,"
                f" `graph describe type <type> --graph {doc.name} --json` names it"
                f" (`alias_prefix`). {padding}"
            )
        elif "types" in doc.blocks:  # the type table names the alias of every type
            lines.append(f"- Aliases such as {first}: the type table names each type's. {padding}")
        else:
            examples = ", ".join(
                f"{code(curie)} {code(format_alias(prefix, 1))}"
                for curie, prefix in doc.prefixes.items()
            )
            lines.append(f"- Aliases: {examples}. {padding}")
    labelled = labelled_fields(doc)
    search = (
        f"- `graph search <value> --graph {doc.name} --json` searches names and every text"
        " field, forgiving case, accents, typos and parts of words. Its results are"
        " suggestions: read one with `graph get` before you use it."
    )
    if not labelled:
        lines.append(
            "- No type of this graph has names (label roles), so `graph lookup` finds nothing;"
            f" use `graph find --graph {doc.name}`."
        )
        lines.append(search)
        return lines
    lookup = (
        f"- `graph lookup <value> --graph {doc.name} --json` finds entities by name, ignoring"
        " case; it matches"
    )
    listed = not doc.modular and not ("types" in doc.blocks and set(labelled) <= set(doc.listed))
    if doc.modular:
        roles = "; ".join(f"{code(role)}: {text}" for role, text in ROLE_NAMES.items())
        lines.append(
            f"{lookup} the name fields of a type, not other text. The module pages list them"
            f" per type; `graph describe type <type> --graph {doc.name} --json` marks them with"
            f" `role` ({roles})."
        )
    elif not listed:  # the ``types`` block marks the name fields of every type that has any
        marks = ", ".join(ROLE_MARKS.values())
        lines.append(f"{lookup} the fields the type table marks {marks}, not other text.")
    else:
        lines.append(f"{lookup} these fields, not other text:")
        for curie, fields in labelled.items():
            lines.append(f"  - {code(curie)}: {name_fields(fields)}")
    lines.append(
        "- Names are not unique, so `lookup` returns candidates; `--for <relation>` or"
        " `--for <function>.<param>` keeps those that can fill that slot. Use the returned"
        " alias or id only when exactly one candidate matches exactly (`match.kind` is"
        " `exact`). With several, or only prefix matches, do not answer about one of them"
        " first: unless type and `facts` show which one the request means, list them with"
        " their names and ask which one is meant, then answer."
    )
    lines.append(search)
    these = "one of these types" if listed else "a type with names"
    lines.append(
        f"- When you create an entity of {these}: if `lookup` finds the same thing under another"
        f" name, add the new name to its {ROLE_NAMES['alt_label']} field instead of creating"
        f" another; fill the {ROLE_NAMES['alt_label']} fields of a new one with the"
        " abbreviations and synonyms people use."
    )
    return lines


def labelled_fields(doc: Model) -> dict[str, list[JsonObject]]:
    """The name fields (label roles) of every concrete type that has any."""
    found = {
        curie: [f for f in ordered_fields(body) if f.get("role") in ROLE_NAMES]
        for curie, body in doc.types.items()
        if body.get("abstract") is not True
    }
    return {curie: fields for curie, fields in found.items() if fields}


def name_fields(fields: Sequence[JsonObject]) -> str:
    return ", ".join(
        f"{code(req_str(f, 'name'))} ({ROLE_NAMES[req_str(f, 'role')]})" for f in fields
    )
