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

- ``modules``: the subject areas (ontology modules) of the graph as a table (module, prefix,
  summary, number of types, links to other modules; built-in modules in one line) and the
  commands to read one module (``describe module``), find a term (``describe search``) and
  read a type (``describe type``). For a modular graph it takes the place of ``types`` and
  ``relations`` in the template.
- ``model``: a pointer to ``references/model.md`` and ``graph describe``.
- ``functions``: up to ``FEATURED_LIMIT`` of the graph's named functions, the ones the
  maintainer marked ``featured`` (by module, then name), one line each (``name(signature)``:
  summary), and the commands to list a module's functions and to search them. Without
  published functions it is one line saying so.
- ``actions``: up to ``ACTIONS_FEATURED_LIMIT`` of the actions the reader may run, the ones the
  maintainer marked ``featured`` (by module, then name), one line each (``name(signature)``:
  summary), the commands to list a module's actions and to search them, and the rule to dry
  run first. Without published actions it is one line saying so.

The compiled function catalog (``graph_cli.functions/1``, the key ``functions`` of the model
documents) is one more input document; the renderer reads only its public shape and never
imports ``graph_cli.kinetic``. A graph without active functions renders exactly as before.
With some, the ``modules`` block gets a ``Functions`` column, every module page a
``Functions`` section and a single-module graph the page ``references/functions.md``
(deprecated functions are left out everywhere).

The compiled action catalog (``graph_cli.action_catalog/1``, the key ``actions``) is one more
input document, present only when the graph has actions the reader may see; it is rendered
the same way: an ``Actions`` column, an ``Actions`` section on every module page and
``references/actions.md`` for a single-module graph. A graph without active actions renders
exactly as before.

``measure`` sizes the rendered ``SKILL.md`` against the level 0 budget of every graph
(``L0_BUDGET`` parts, single-module graphs included) and its curated text against
``CURATED_BUDGET``; ``graph-admin`` warns when either is exceeded and refuses nothing.

A graph with two or more non-built-in modules is modular: every generated skill then also
gets ``references/modules/<module>.md`` (level 1 of the module view as text) and
``references/model.md`` is an index of the modules and of every type with its module and
page, without the per-type sections. A single-module graph renders as before; the module
documents (``modules``, ``module``) only feed the ``modules`` block. This renderer stays
kernel-only: the module line formats follow the CLI text output but are written here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.ids import format_alias
from graph_cli.kernel.json_access import as_object, opt_str, req_list, req_obj, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, skill_hash
from graph_cli.skills.graph_skill.model import Model, objects, strings
from graph_cli.skills.graph_skill.naming import labelled_fields, name_fields, render_names
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
    render_relations,
)
from graph_cli.skills.graph_skill.start import render_start
from graph_cli.skills.graph_skill.type_blocks import (
    lifecycle_attr,
    marked_notes,
    render_constraints,
    render_types,
)
from graph_cli.skills.template import (
    ACTIONS_FILE,
    FUNCTIONS_FILE,
    MODEL_FILE,
    MODULES_DIR,
    Template,
    parse_template,
)
from graph_cli.skills.tree import SKILL_FILE, Problem, SkillFiles, validate

FEATURED_LIMIT: Final = 12  # functions listed in ``SKILL.md``, however many a graph has
ACTIONS_FEATURED_LIMIT: Final = 8  # actions listed in ``SKILL.md``, however many a graph has
GENERATOR: Final = "graph_cli.skills"
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
        MODEL_FILE: _model_file(doc, stamp),
        **{
            f"{MODULES_DIR}{module_id}.md": _module_page(doc, module_id, stamp)
            for module_id in (doc.pages if doc.modular else {})
        },
        **(
            {FUNCTIONS_FILE: _functions_file(doc, stamp)}
            if doc.functions and not doc.modular
            else {}
        ),
        **({ACTIONS_FILE: _actions_file(doc, stamp)} if doc.actions and not doc.modular else {}),
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


def _module_links(item: JsonObject) -> str:
    links = [
        f"{code(req_str(k, 'module'))} ({k['out']} out, {k['in']} in)"
        for k in objects(item, "links")
    ]
    return ", ".join(links) or "none"


def _modules(doc: Model) -> list[str]:
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


def _param_text(param: JsonObject) -> str:
    """``name: type``, ``name?: type`` for an optional parameter (the form of ``graph describe
    functions`` and ``graph describe actions``)."""
    kind = req_str(param, "type")
    if kind == "ref":
        kind += f" {param.get('class')}"
    elif kind == "enum":
        kind += f" {param.get('enum')}"
    if param.get("many") is True:
        kind = f"list of {kind}"
    optional = "?" if param.get("default") is not None or param.get("optional") is True else ""
    return f"{req_str(param, 'name')}{optional}: {kind}"


def _signature(function: JsonObject) -> str:
    return ", ".join(_param_text(as_object(p, "params[]")) for p in req_list(function, "params"))


def _function_line(function: JsonObject) -> str:
    call = f"{req_str(function, 'name')}({_signature(function)})"
    return f"- {code(call)}: {plain(opt_str(function, 'summary'))}"


def _functions(doc: Model) -> list[str]:
    """Level 0: a few featured functions and how to find the rest (K12)."""
    if not doc.functions:
        return ["This graph has no published functions."]
    name = doc.name
    featured = [f for f in doc.functions if f.get("featured") is True][:FEATURED_LIMIT]
    lines = [
        f"{len(doc.functions)} named functions; for a question use one before you write a query:"
        f" `graph run <function> --param <name>=<value> --graph {name} --json`.",
    ]
    if featured:
        lines += ["", *[_function_line(f) for f in featured]]
    lines += [
        "",
        f"More: `graph describe functions --module <module> --graph {name}`; find one:"
        f" `graph describe search <text> --kind function --graph {name} --json`; columns and use:"
        f" `graph describe function <function> --graph {name}`.",
    ]
    return lines


def _function_table(functions: Sequence[JsonObject]) -> list[str]:
    return md_table(
        ["Function", "Parameters", "Summary"],
        [
            [code(req_str(f, "name")), _signature(f) or "none", plain(opt_str(f, "summary"))]
            for f in functions
        ],
    )


def _function_hint(doc: Model) -> list[str]:
    return [
        "",
        f"Run one with `graph run <function> --param <name>=<value> --graph {doc.name} --json`;"
        f" `graph describe function <function> --graph {doc.name}` gives its columns and when to"
        " use it.",
    ]


def _functions_file(doc: Model, stamp: str) -> str:
    """``references/functions.md`` of a single-module graph."""
    lines = [
        stamp,
        "",
        f"# Functions of graph `{doc.name}`",
        "",
        f"Ontology `{doc.bundle}` {doc.version}. The live source is `graph describe functions"
        f" --graph {doc.name} --json`; it wins when they differ.",
        "",
        *_function_table(doc.functions),
        *_function_hint(doc),
    ]
    return "\n".join(lines) + "\n"


def _module_functions(doc: Model, module_id: str) -> list[str]:
    """The ``Functions`` section of a module page (none for a graph without functions)."""
    if not doc.functions:
        return []
    own = [f for f in doc.functions if opt_str(f, "module") == module_id]
    if not own:
        return ["", "## Functions", "", "None."]
    return ["", "## Functions", "", *_function_table(own), *_function_hint(doc)]


def _action_summary(action: JsonObject) -> str:
    """The summary, and that the action needs ``--reason`` when it does (saves a refusal)."""
    summary = plain(opt_str(action, "summary"))
    return f"{summary} Needs `--reason`." if action.get("reason") == "required" else summary


def _action_line(action: JsonObject) -> str:
    call = f"{req_str(action, 'name')}({_signature(action)})"
    return f"- {code(call)}: {_action_summary(action)}"


def _actions(doc: Model) -> list[str]:
    """Level 0: a few featured actions, how to find the rest and the dry-run rule (A11)."""
    if not doc.actions:
        return ["This graph has no published actions."]
    name = doc.name
    featured = [a for a in doc.actions if a.get("featured") is True][:ACTIONS_FEATURED_LIMIT]
    lines = [
        f"{len(doc.actions)} named actions; for a change use one before you write with the"
        f" primitives. Dry run first: `graph do <action> --param <name>=<value> --graph {name}"
        " --dry-run`, read what it will change, then run it without `--dry-run`.",
    ]
    if featured:
        lines += ["", *[_action_line(a) for a in featured]]
    lines += [
        "",
        f"More: `graph describe actions --module <module> --graph {name}`; find one:"
        f" `graph describe search <text> --kind action --graph {name} --json`; preconditions and"
        f" use: `graph describe action <action> --graph {name}`. For an entity you already have,"
        f" `graph get <id> --affordances --graph {name}` lists the actions that fit it, whether"
        " each applies and what blocks it.",
    ]
    return lines


def _action_table(actions: Sequence[JsonObject]) -> list[str]:
    return md_table(
        ["Action", "Parameters", "Summary"],
        [[code(req_str(a, "name")), _signature(a) or "none", _action_summary(a)] for a in actions],
    )


def _action_hint(doc: Model) -> list[str]:
    return [
        "",
        f"Dry run first: `graph do <action> --param <name>=<value> --graph {doc.name}"
        " --dry-run`, read what it will change, then run it without `--dry-run`;"
        f" `graph describe action <action> --graph {doc.name}` gives its preconditions and when"
        " to use it.",
    ]


def _actions_file(doc: Model, stamp: str) -> str:
    """``references/actions.md`` of a single-module graph."""
    lines = [
        stamp,
        "",
        f"# Actions of graph `{doc.name}`",
        "",
        f"Ontology `{doc.bundle}` {doc.version}. The live source is `graph describe actions"
        f" --graph {doc.name} --json`; it wins when they differ.",
        "",
        *_action_table(doc.actions),
        *_action_hint(doc),
    ]
    return "\n".join(lines) + "\n"


def _module_actions(doc: Model, module_id: str) -> list[str]:
    """The ``Actions`` section of a module page (none for a graph without actions)."""
    if not doc.actions:
        return []
    own = [a for a in doc.actions if opt_str(a, "module") == module_id]
    if not own:
        return ["", "## Actions", "", "None."]
    return ["", "## Actions", "", *_action_table(own), *_action_hint(doc)]


def _model_pointer(doc: Model) -> list[str]:
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


_BLOCKS: Final[Mapping[str, Callable[[Model], list[str]]]] = {
    "start": render_start,
    "modules": _modules,
    "types": render_types,
    "relations": render_relations,
    "constraints": render_constraints,
    "names": render_names,
    "model": _model_pointer,
    "functions": _functions,
    "actions": _actions,
}


# ---- references/model.md -----------------------------------------------------------


def _model_file(doc: Model, stamp: str) -> str:
    if doc.modular:
        return _model_index(doc, stamp)
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


# ---- modular graphs: references/model.md as an index, references/modules/<module>.md -----


def _page_link(doc: Model, module_id: str) -> str:
    return f"[{module_id}.md](modules/{module_id}.md)" if module_id in doc.pages else ""


def _model_index(doc: Model, stamp: str) -> str:
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


def _module_page(doc: Model, module_id: str, stamp: str) -> str:
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
    lines += _module_functions(doc, module_id)
    lines += _module_actions(doc, module_id)
    return "\n".join(lines) + "\n"
