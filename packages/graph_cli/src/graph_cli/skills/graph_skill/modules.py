"""The ``modules`` block and the pages of a modular graph.

- ``modules``: the subject areas (ontology modules) of the graph as a table (module, prefix,
  summary, number of types, links to other modules; built-in modules in one line) and the
  commands to read one module (``describe module``), find a term (``describe search``) and
  read a type (``describe type``). For a modular graph it takes the place of ``types`` and
  ``relations`` in the template.

A graph with two or more non-built-in modules is modular: every generated skill then also
gets ``references/modules/<module>.md`` (level 1 of the module view as text) and
``references/model.md`` is an index of the modules and of every type with its module and
page, without the per-type sections. A single-module graph renders as before; the module
documents (``modules``, ``module``) only feed the ``modules`` block. This renderer stays
kernel-only: the module line formats follow the CLI text output but are written here.
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.ids import format_alias
from graph_cli.kernel.json_access import opt_str, req_list, req_obj, req_str
from graph_cli.skills.graph_skill.catalogs import module_actions, module_functions
from graph_cli.skills.graph_skill.model import Model, objects
from graph_cli.skills.graph_skill.naming import labelled_fields, name_fields
from graph_cli.skills.graph_skill.pieces import code, codes, md_table, plain, term_summary
from graph_cli.skills.graph_skill.relations import cardinality_text, relation_ends
from graph_cli.skills.template import MODEL_FILE, MODULES_DIR


def _module_links(item: JsonObject) -> str:
    links = [
        f"{code(req_str(k, 'module'))} ({k['out']} out, {k['in']} in)"
        for k in objects(item, "links")
    ]
    return ", ".join(links) or "none"


def render_modules(doc: Model) -> list[str]:
    """Level 0: the subject areas, then how to read one. Built-in modules (shared by every
    graph) get one line, not a row."""
    shown = [m for m in doc.modules if m.get("builtin") is not True]
    built = [req_str(m, "id") for m in doc.modules if m.get("builtin") is True]
    counts = _counts(doc.functions)  # the column exists only for a graph with functions
    action_counts = _counts(doc.actions)  # likewise for actions
    lines = md_table(
        [
            "Module",
            "Prefix",
            "What it holds",
            "Types",
            *(["Functions"] if doc.functions else []),
            *(["Actions"] if doc.actions else []),
            "Links to other modules",
        ],
        [
            [
                code(req_str(m, "id")),
                code(req_str(m, "prefix")),
                plain(opt_str(m, "summary")),
                str(m["types"]),
                *([str(counts.get(req_str(m, "id"), 0))] if doc.functions else []),
                *([str(action_counts.get(req_str(m, "id"), 0))] if doc.actions else []),
                _module_links(m),
            ]
            for m in shown
        ],
    )
    if built:
        lines += ["", f"Built-in modules, shared by every graph: {codes(built)}."]
    name = doc.name
    lines += [
        "",
        f"- Before you work in a module, read it: `graph describe module <module> --graph {name}`"
        " lists its types, its relations, its links to and from other modules (with the other"
        " module named) and its rules. Read its text; `--json` is more than twice the size.",
        f"- To find a term whose module you do not know: `graph describe search <text> --graph"
        f" {name} --json` (types, relations, fields, enum values and states).",
        f"- For the fields, states and relations of one type: `graph describe type <type>"
        f" --graph {name} --json`.",
    ]
    if doc.modular:
        lines.append(
            f"- The same module views as text, and an index of every type with its module:"
            f" [{MODEL_FILE}]({MODEL_FILE}) and the pages `{MODULES_DIR}<module>.md`."
        )
    return lines


def _counts(entries: Sequence[JsonObject]) -> dict[str, int]:
    """Catalog entries per module id."""
    counts: dict[str, int] = {}
    for entry in entries:
        module = opt_str(entry, "module")
        if module is not None:
            counts[module] = counts.get(module, 0) + 1
    return counts


def _page_link(doc: Model, module_id: str) -> str:
    return f"[{module_id}.md](modules/{module_id}.md)" if module_id in doc.pages else ""


def model_index(doc: Model, stamp: str) -> str:
    lines = [
        stamp,
        "",
        f"# Model of graph `{doc.name}`",
        "",
        f"Ontology `{doc.bundle}` {doc.version}, read module by module. The live source is"
        f" `graph describe modules --graph {doc.name} --json`,"
        f" `graph describe module <module> --graph {doc.name} --json` and"
        f" `graph describe type <type> --graph {doc.name} --json`; it wins when they differ."
        " A module without a page is built in and shared by every graph.",
        "",
        "## Modules",
        "",
        *md_table(
            ["Module", "Prefix", "Page", "Summary"],
            [
                [
                    code(req_str(m, "id")),
                    code(req_str(m, "prefix")),
                    _page_link(doc, req_str(m, "id")),
                    plain(opt_str(m, "summary")),
                ]
                for m in doc.modules
            ],
        ),
        "",
        "## Types",
        "",
        *md_table(
            ["Type", "Module", "Page", "Alias", "Abstract", "Extends", "Summary"],
            [
                [
                    code(curie),
                    code(req_str(body, "module")),
                    _page_link(doc, req_str(body, "module")),
                    code(format_alias(doc.prefixes[curie], 1)) if curie in doc.prefixes else "",
                    "yes" if body.get("abstract") is True else "no",
                    code(str(body["extends"])) if body.get("extends") else "",
                    term_summary(body),
                ]
                for curie, body in doc.types.items()
            ],
        ),
    ]
    return "\n".join(lines) + "\n"


def _link_where(link: JsonObject, module_id: str) -> str:
    """The other modules at the ends; the declaring module too when it is neither end."""
    others = [str(m) for m in req_list(link, "other_modules")]
    where = codes(others) or "any"
    owner = req_str(link, "module")
    if owner != module_id and owner not in others:
        where += f"; declared in {code(owner)}"
    return where


def module_page(doc: Model, module_id: str, stamp: str) -> str:
    item = doc.pages[module_id]
    name = doc.name
    lines = [
        stamp,
        "",
        f"# Module `{module_id}` of graph `{name}`",
        "",
        f"Prefix `{req_str(item, 'prefix')}`, version {req_str(item, 'version')}. "
        + plain(opt_str(item, "summary")),
        "",
        f"Ontology `{doc.bundle}` {doc.version}. The live source is `graph describe module"
        f" {module_id} --graph {name} --json`; it wins when they differ. Fields, states and"
        f" relations of a type: `graph describe type <type> --graph {name} --json`.",
        "",
        "## Types",
        "",
    ]
    types = objects(item, "types")
    labelled = labelled_fields(doc)
    lines += (
        md_table(
            ["Type", "Alias", "Abstract", "Extends", "Names", "Summary"],
            [
                [
                    code(req_str(t, "curie")),
                    code(format_alias(doc.prefixes[req_str(t, "curie")], 1))
                    if req_str(t, "curie") in doc.prefixes
                    else "",
                    "yes" if t.get("abstract") is True else "no",
                    code(str(t["extends"])) if t.get("extends") else "",
                    name_fields(labelled.get(req_str(t, "curie"), [])),
                    term_summary(t),
                ]
                for t in types
            ],
        )
        if types
        else ["None."]
    )
    lines += ["", "## Relations inside the module", ""]
    relations = objects(item, "relations")
    lines += (
        md_table(
            ["Relation", "From", "To", "Cardinality", "Summary"],
            [
                [
                    code(req_str(r, "curie")),
                    relation_ends(r, "domain"),
                    relation_ends(r, "range"),
                    cardinality_text(req_obj(r, "cardinality")),
                    term_summary(r),
                ]
                for r in relations
            ],
        )
        if relations
        else ["None."]
    )
    for key, title in (
        ("links_out", "Links to other modules"),
        ("links_in", "Links from other modules"),
    ):
        links = objects(item, key)
        lines += ["", f"## {title}", ""]
        lines += (
            md_table(
                ["Relation", "From", "To", "Other modules", "Summary"],
                [
                    [
                        code(req_str(k, "relation")),
                        relation_ends(k, "from"),
                        relation_ends(k, "to"),
                        _link_where(k, module_id),
                        term_summary(k),
                    ]
                    for k in links
                ],
            )
            if links
            else ["None."]
        )
    rules = objects(item, "rules")
    lines += ["", "## Rules", ""]
    lines += (
        md_table(
            ["Rule", "Severity", "Summary"],
            [[code(req_str(r, "id")), req_str(r, "severity"), term_summary(r)] for r in rules],
        )
        if rules
        else ["None."]
    )
    lines += module_functions(doc, module_id)
    lines += module_actions(doc, module_id)
    return "\n".join(lines) + "\n"
