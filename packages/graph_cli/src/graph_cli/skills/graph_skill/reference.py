"""The ``model`` block and ``references/model.md``.

- ``model``: a pointer to ``references/model.md`` and ``graph describe``.

``references/model.md`` describes every type of the model: the types and relations as tables, then
one section per type (fields, lifecycles, relations, rules). For a modular graph it is an index
of the modules and of every type with its module and page instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.ids import format_alias
from graph_cli.kernel.json_access import opt_str, req_obj, req_str
from graph_cli.skills.graph_skill.model import Model, objects, strings
from graph_cli.skills.graph_skill.modules import model_index
from graph_cli.skills.graph_skill.pieces import (
    code,
    codes,
    md_table,
    plain,
    term_summary,
    value_code,
)
from graph_cli.skills.graph_skill.relations import (
    cardinality_text,
    query_patterns,
    relation_ends,
    relation_notes,
)
from graph_cli.skills.graph_skill.type_blocks import lifecycle_attr, marked_notes
from graph_cli.skills.template import MODEL_FILE, MODULES_DIR

FIELD_COLUMNS: Final = (
    "Field",
    "Type",
    "Base",
    "Flags",
    "Default",
    "Mutability",
    "Values and limits",
    "Declared in",
    "Summary",
)
# Value shapes the base name alone does not tell.
VALUE_SHAPES: Final[Mapping[str, str]] = {
    "blob": "`sha256:<hex>` of a stored file",
    "local_path": '`{"root": <name>, "path": <relative path>}`',
    "lang_string": '`"<text>"@<tag>` or `{"lang": <tag>, "value": <text>}`; untagged: the graph'
    " language",
    "lang_text": '`"<text>"@<tag>` or `{"lang": <tag>, "value": <text>}`; untagged: the graph'
    " language",
}


def render_model_pointer(doc: Model) -> list[str]:
    if doc.modular:
        return [
            f"The modules and every type with its module and page: [{MODEL_FILE}]({MODEL_FILE});"
            f" one page per module in `{MODULES_DIR}`. Fields, lifecycles and rules of a type, the"
            f" live source, which wins when they differ:"
            f" `graph describe type <type> --graph {doc.name} --json`."
        ]
    return [
        f"Every type with all its fields, lifecycles, relations and rules: [{MODEL_FILE}]"
        f"({MODEL_FILE}). The live source, which wins when they differ:"
        f" `graph describe type <type> --graph {doc.name} --json`."
    ]


def model_file(doc: Model, stamp: str) -> str:
    if doc.modular:
        return model_index(doc, stamp)
    lines = [
        stamp,
        "",
        f"# Model of graph `{doc.name}`",
        "",
        f"Ontology `{doc.bundle}` {doc.version}. The live source is"
        f" `graph describe types --graph {doc.name} --json` and"
        f" `graph describe type <type> --graph {doc.name} --json`; it wins when they differ.",
        "",
        "## Types",
        "",
        *md_table(
            ["Type", "Alias", "Abstract", "Extends", "Summary"],
            [
                [
                    code(curie),
                    code(format_alias(doc.prefixes[curie], 1)) if curie in doc.prefixes else "",
                    "yes" if body.get("abstract") is True else "no",
                    code(str(body["extends"])) if body.get("extends") else "",
                    term_summary(body),
                ]
                for curie, body in doc.types.items()
            ],
        ),
        "",
        "## Relations",
        "",
    ]
    if doc.relations:
        lines += md_table(
            ["Relation", "From", "To", "Cardinality", "Notes", "Summary"],
            [
                [
                    code(curie),
                    relation_ends(relation, "domain"),
                    relation_ends(relation, "range"),
                    cardinality_text(req_obj(relation, "cardinality")),
                    relation_notes(relation),
                    term_summary(relation),
                ]
                for curie, relation in sorted(doc.relations.items())
            ],
        )
        lines += query_patterns(doc, sorted(doc.relations))
    else:
        lines.append("No relations.")
    for curie, body in doc.types.items():
        lines += ["", *_type_section(curie, body)]
    return "\n".join(lines) + "\n"


def _type_section(curie: str, body: JsonObject) -> list[str]:
    lines = [f"## `{curie}`", "", term_summary(body) or "No summary."]
    description = opt_str(body, "description")
    if description:
        lines += ["", description.strip()]
    facts = []
    if body.get("abstract") is True:
        facts.append("Abstract (no entities of its own).")
    ancestors = strings(body.get("ancestors"))
    if ancestors:
        facts.append(f"Is a {codes(ancestors)}.")
    subtypes = strings(body.get("subtypes"))
    if subtypes:
        facts.append(f"Subtypes: {codes(subtypes)}.")
    if facts:
        lines += ["", " ".join(facts)]
    fields = objects(body, "fields")
    lines += ["", "Fields:", ""]
    if fields:
        lines += md_table(
            FIELD_COLUMNS,
            [
                [
                    code(req_str(f, "name")),
                    f"{req_str(f, 'kind')} {code(req_str(f, 'type'))}",
                    req_str(f, "base"),
                    _flags(f),
                    value_code(f["default"]) if f.get("default") is not None else "",
                    req_str(f, "mutability"),
                    _limits(f),
                    code(req_str(f, "declared_in")),
                    term_summary(f),
                ]
                for f in fields
            ],
        )
    else:
        lines.append("None.")
    for lifecycle in objects(body, "lifecycles"):
        lines += ["", *_lifecycle_section(lifecycle)]
    out = objects(body, "relations_out")
    if out:
        lines += [
            "",
            "Relations out:",
            "",
            *md_table(
                ["Relation", "To", "Cardinality", "Inverse", "Qualifiers", "Summary"],
                [
                    [
                        code(req_str(r, "curie")),
                        relation_ends(r, "range"),
                        req_str(r, "cardinality"),
                        code(str(r["inverse_label"])) if r.get("inverse_label") else "",
                        codes(strings(r.get("qualifiers"))),
                        term_summary(r),
                    ]
                    for r in out
                ],
            ),
        ]
    into = objects(body, "relations_in")
    if into:
        lines += [
            "",
            "Relations in:",
            "",
            *md_table(
                ["Relation", "From", "Cardinality in", "Inverse", "Summary"],
                [
                    [
                        code(req_str(r, "curie")),
                        relation_ends(r, "from"),
                        req_str(r, "cardinality"),
                        code(str(r["inverse_label"])) if r.get("inverse_label") else "",
                        term_summary(r),
                    ]
                    for r in into
                ],
            ),
        ]
    rules = objects(body, "rules")
    if rules:
        lines += [
            "",
            "Rules:",
            "",
            *md_table(
                ["Rule", "Severity", "When", "Summary"],
                [
                    [
                        code(req_str(r, "id")),
                        req_str(r, "severity"),
                        req_str(r, "when"),
                        term_summary(r),
                    ]
                    for r in rules
                ],
            ),
        ]
    return lines


def _flags(field: JsonObject) -> str:
    flags = [flag for flag in ("required", "many", "unique") if field.get(flag) is True]
    role = field.get("role")
    if isinstance(role, str):
        flags.append(role)
    flags += marked_notes(field)
    return ", ".join(flags)


def _limits(field: JsonObject) -> str:
    parts = []
    values = strings(field.get("values"))
    if values:
        parts.append(f"one of {codes(values)}")
    shape = VALUE_SHAPES.get(req_str(field, "base"))
    if shape is not None:
        parts.append(shape)
    limits = req_obj(field, "limits")
    parts += [f"{name} {value_code(limits[name])}" for name in sorted(limits)]
    return ", ".join(parts)


def _lifecycle_section(lifecycle: JsonObject) -> list[str]:
    lines = [
        f"Lifecycle {code(lifecycle_attr(lifecycle))} ({code(req_str(lifecycle, 'lifecycle'))}):"
        f" {term_summary(lifecycle)}",
        "",
    ]
    initial = set(strings(lifecycle.get("initial")))
    terminal = set(strings(lifecycle.get("terminal")))
    for state in objects(lifecycle, "states"):
        name = req_str(state, "name")
        marks = [m for m, s in (("initial", initial), ("terminal", terminal)) if name in s]
        mark = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"- State {code(name)}{mark}: {plain(opt_str(state, 'summary'))}")
    for transition in objects(lifecycle, "transitions"):
        guards = objects(transition, "guards")
        text = f"- {code(req_str(transition, 'from'))} -> {code(req_str(transition, 'to'))}"
        if guards:
            text += ": " + "; ".join(
                f"guard {code(req_str(g, 'id'))}: {plain(opt_str(g, 'summary'))}" for g in guards
            )
        lines.append(text)
    return lines
