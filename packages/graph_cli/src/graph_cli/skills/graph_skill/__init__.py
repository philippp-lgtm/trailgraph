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

- ``start``: load the ``graph-cli`` skill before the first command, how to tell the graph is
  reachable, the graph language, and the rules of working with ``graph`` that apply from the
  first call (plain calls, the envelope, asking for less, never guessing, functions and
  actions first, dry runs, ``--reason`` and ``--expect``, findings), so that a graph skill
  loaded without skill 1 still leads to safe calls (F42, F74); for a model with
  ``files:File``, one line on the file workflow; for a graph with published views a clause
  on the ``graph view`` rule that names ``graph describe views``.
- ``modules``: the subject areas (ontology modules) of the graph as a table (module, prefix,
  summary, number of types, links to other modules; built-in modules in one line) and the
  commands to read one module (``describe module``), find a term (``describe search``) and
  read a type (``describe type``). For a modular graph it takes the place of ``types`` and
  ``relations`` in the template.
- ``types``: the entity types (types with an alias prefix; without any, every
  concrete type) with their supertypes, fields (label roles named), enum values inline
  (F11), and states. A field the model marks (``markings``, field visibility plan F7) shows
  ``marked <curie>`` with its markings joined by ``+``, here and in the ``Flags`` column of
  the model file; the documents of a reader hold only the fields it sees, so a graph without
  markings, or a reader without a marked field, renders as before.
- ``relations``: the relations touching those types, each with its ``graph query`` pattern
  (``(:A)-[:rel]->(:B)``, from ``describe schema``) in place of its ends; inverse labels
  only in ``references/model.md``.
- ``constraints``: transition guards, write-once fields (left out when the template has
  ``types``, which marks them) and rules.
- ``names``: the live alias format (F12; with ``types`` in the template one example, the
  table has the rest), what ``graph lookup`` matches per
  type (label roles; with ``types`` in the template, a pointer to the roles named there) and
  the rule to ask when the match is not unique (F24). On a modular
  graph it names one alias and points at the module pages and ``describe type`` for the
  prefixes and name fields, so its size does not grow with the types.
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

The compiled view catalog (``graph_cli.view_catalog/1``, the key ``views``) is one more input
document, present only when the graph has views the reader may see. Only its presence shows: one
clause in the ``start`` block says the graph has views and where to list them (no list in
level 0: discovery is ``graph describe views`` and ``describe search``). A graph without active
views renders exactly as before.

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
from graph_cli.kernel.ids import ALIAS_DIGITS, format_alias
from graph_cli.kernel.json_access import as_list, as_object, opt_str, req_list, req_obj, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, skill_hash
from graph_cli.skills.graph_skill.pieces import (
    code,
    codes,
    md_table,
    plain,
    term_summary,
    value_code,
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

MODEL_SCHEMA: Final = "graph_cli.model/1"
FUNCTIONS_SCHEMA: Final = "graph_cli.functions/1"  # the compiled catalog, ``graph_cli.kinetic``
FEATURED_LIMIT: Final = 12  # functions listed in ``SKILL.md``, however many a graph has
ACTIONS_SCHEMA: Final = "graph_cli.action_catalog/1"  # the compiled action catalog
ACTIONS_FEATURED_LIMIT: Final = 8  # actions listed in ``SKILL.md``, however many a graph has
VIEWS_SCHEMA: Final = "graph_cli.view_catalog/1"  # the compiled view catalog
GENERATOR: Final = "graph_cli.skills"
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
LANG_BASES: Final = frozenset({"lang_string", "lang_text"})
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
FILE_TYPE: Final = "files:File"  # the standard module files (B7.8), not graph vocabulary
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
WHEN: Final[Mapping[str, str]] = {"write": "checked on every write", "batch": "checked in batch"}


def render_graph_skill(
    template: Template, model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]
) -> dict[str, str]:
    """``{relative path: content}`` of the skill; ``ValueError`` on documents it cannot read."""
    doc = _Model.of(model, graph, _used(template))
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
    doc = _Model.of(model, graph, _used(template))
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


# ---- reading the documents ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Model:
    name: str
    title: str | None
    language: str | None  # the graph language of the manifest (BCP 47)
    blocks: frozenset[str]  # the blocks the template uses: one block may leave to another
    bundle: str
    version: str
    prefixes: Mapping[str, str]  # type CURIE -> alias prefix, types of the model only
    types: Mapping[str, JsonObject]  # CURIE -> the ``type`` body of ``describe type``
    relations: Mapping[str, JsonObject]  # CURIE -> one item of ``describe relations``
    patterns: Mapping[str, str]  # relation CURIE -> its ``describe schema`` pattern
    modules: Sequence[JsonObject]  # the items of ``describe modules``, built-ins included
    pages: Mapping[str, JsonObject]  # module id -> ``module`` body of ``describe module``
    functions: Sequence[JsonObject]  # active catalog entries, by module then name
    actions: Sequence[JsonObject]  # active action catalog entries, by module then name
    views: Sequence[JsonObject]  # active view catalog entries, by module then name

    @staticmethod
    def of(
        model: Mapping[str, JsonValue],
        graph: Mapping[str, JsonValue],
        blocks: frozenset[str] = frozenset(),
    ) -> _Model:
        bundle = as_object(dict(model), "model")
        listing = _document(bundle, "types")
        relations = _document(bundle, "relations")
        schema = _document(bundle, "schema")
        modules = _objects(bundle, "modules")
        pages: dict[str, JsonObject] = {}
        for module_id, value in sorted(req_obj(bundle, "module").items()):
            body = req_obj(_document({"module": value}, "module"), "module")
            if req_str(body, "id") != module_id or "/" in module_id:
                raise ValueError(f"module document {module_id!r} describes {body.get('id')!r}")
            pages[module_id] = body
        types: dict[str, JsonObject] = {}
        for curie, value in sorted(req_obj(bundle, "type").items()):
            body = req_obj(_document({"type": value}, "type"), "type")
            if req_str(body, "curie") != curie:
                raise ValueError(f"type document {curie!r} describes {body.get('curie')!r}")
            types[curie] = body
        listed = sorted(
            req_str(as_object(t, "types[]"), "curie") for t in req_list(listing, "types")
        )
        if listed != sorted(types):
            raise ValueError("the type documents do not match the types listing")
        ontology = req_obj(listing, "ontology")
        block = as_object(dict(graph), "graph")
        prefixes = {
            curie: str(prefix)
            for curie, prefix in sorted(req_obj(block, "alias_prefixes").items())
            if curie in types
        }
        return _Model(
            name=req_str(block, "name"),
            title=opt_str(block, "title"),
            language=opt_str(block, "language"),
            blocks=blocks,
            bundle=req_str(ontology, "bundle"),
            version=req_str(ontology, "version"),
            prefixes=prefixes,
            types=types,
            relations={
                req_str(item, "curie"): item
                for item in (as_object(r, "relations[]") for r in req_list(relations, "relations"))
            },
            patterns={
                req_str(item, "relation"): req_str(item, "pattern")
                for item in (
                    as_object(r, "relationships[]") for r in req_list(schema, "relationships")
                )
            },
            modules=modules,
            pages=pages,
            functions=_active_functions(bundle),
            actions=_active_actions(bundle),
            views=_active_views(bundle),
        )

    @property
    def modular(self) -> bool:
        """Two or more modules besides the built-in ones: the model is read module by module."""
        return len(self.pages) >= 2

    @property
    def listed(self) -> list[str]:
        """The entity types of the graph: those with an alias prefix, else every concrete one."""
        if self.prefixes:
            return sorted(self.prefixes)
        return [curie for curie, body in self.types.items() if body.get("abstract") is not True]

    def touching(self) -> list[str]:
        """Relations from or to a listed type."""
        found = set()
        for curie in self.listed:
            body = self.types[curie]
            for key in ("relations_out", "relations_in"):
                found |= {req_str(as_object(r, key), "curie") for r in req_list(body, key)}
        return sorted(found)


def _active_functions(bundle: JsonObject) -> list[JsonObject]:
    """The active entries of the compiled catalog document, if the bundle has one."""
    catalog = bundle.get("functions")
    if catalog is None:
        return []
    document = as_object(catalog, "functions")
    if document.get("schema") != FUNCTIONS_SCHEMA:
        raise ValueError(f"functions: expected {FUNCTIONS_SCHEMA}, got {document.get('schema')!r}")
    entries = [e for e in _objects(document, "functions") if e.get("status") != "deprecated"]
    return sorted(entries, key=lambda e: (opt_str(e, "module") or "", req_str(e, "name")))


def _active_actions(bundle: JsonObject) -> list[JsonObject]:
    """The active entries of the compiled action catalog document, if the bundle has one."""
    catalog = bundle.get("actions")
    if catalog is None:
        return []
    document = as_object(catalog, "actions")
    if document.get("schema") != ACTIONS_SCHEMA:
        raise ValueError(f"actions: expected {ACTIONS_SCHEMA}, got {document.get('schema')!r}")
    entries = [e for e in _objects(document, "actions") if e.get("status") != "deprecated"]
    return sorted(entries, key=lambda e: (opt_str(e, "module") or "", req_str(e, "name")))


def _active_views(bundle: JsonObject) -> list[JsonObject]:
    """The active entries of the compiled view catalog document, if the bundle has one."""
    catalog = bundle.get("views")
    if catalog is None:
        return []
    document = as_object(catalog, "views")
    if document.get("schema") != VIEWS_SCHEMA:
        raise ValueError(f"views: expected {VIEWS_SCHEMA}, got {document.get('schema')!r}")
    entries = [e for e in _objects(document, "views") if e.get("status") != "deprecated"]
    return sorted(entries, key=lambda e: (opt_str(e, "module") or "", req_str(e, "name")))


def _document(bundle: JsonObject, key: str) -> JsonObject:
    doc = req_obj(bundle, key)
    if doc.get("schema") != MODEL_SCHEMA:
        raise ValueError(f"{key}: expected {MODEL_SCHEMA}, got {doc.get('schema')!r}")
    return doc


def _objects(doc: JsonObject, key: str) -> list[JsonObject]:
    return [as_object(item, f"{key}[]") for item in req_list(doc, key)]


def _strings(value: JsonValue) -> list[str]:
    return [str(item) for item in as_list(value, "list")] if value is not None else []


def _stamp(doc: _Model, template: Template) -> str:
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


def _patterns_source(doc: _Model) -> str:
    """Where the query patterns are read: the whole schema, or one module at a time."""
    if doc.modular:
        return (
            f"read the module first with `graph describe module <module> --graph {doc.name}`;"
            f" `graph describe schema --module <module> --graph {doc.name} --compact --json`"
            " lists its patterns"
        )
    return "`graph describe schema --compact --json` lists the patterns"


def _start(doc: _Model) -> list[str]:
    name = doc.name
    title = f" (title: {plain(doc.title)})" if doc.title else ""
    return [
        "Load the `graph-cli` skill as well before your first `graph` command: it has every"
        " command with its options, the change sets of `graph apply` and the error codes. The"
        " rules of working with `graph` below hold from the first call, with or without it.",
        "",
        f"Run `graph graphs --json`. Use this skill only if an item named {code(name)} is"
        f" listed{title}. Pass `--graph {name}` to every command, as the examples here do,"
        f" so they work in every directory; a project whose pointer makes {code(name)}"
        " the default may leave it out.",
        *_language(doc),
        "",
        "Working with `graph`:",
        "",
        "- Run each call as a plain command that starts with `graph` and passes `--json`"
        + (
            " (the module views of `graph describe module` read better as text)"
            if doc.modular
            else ""
        )
        + ": no `cd`, variables, loops, pipes or heredocs, which a restricted harness refuses."
        " If a call is refused, run it again in that form before you give up.",
        "- Read the answer: `ok`, then `data`; on failure `error.hint` is the command to run"
        " next. Never cut an answer short; ask for less: `--fields <attr>,<attr>` on `find`,"
        " `get` and `neighbors`, or `graph query` with `RETURN` of just the columns you need"
        f" ({_patterns_source(doc)}).",
        "- For what is known about one entity, `graph view <id> --json` answers in one call (key"
        " fields, relations with counts, files, what applies next): use it before walking edges"
        " with `get` and `neighbors`."
        + (
            f" The graph also has published views: `graph describe views --graph {name}`."
            if doc.views
            else ""
        ),
        "- Never guess an option, type, field, relation or state: `graph <command> --help`"
        f" lists the options, `graph describe type <type> --graph {name} --json` the fields,"
        " states and relations.",
        *(
            [
                "- For a question, run a named function when one fits (`graph run <function>"
                " --param <name>=<value>`); write a `graph query` only when none does. An empty"
                " value in a function's answer means nothing is recorded: say so instead of"
                " searching further."
            ]
            if doc.functions
            else []
        ),
        (
            "- For a change, run a named action when one fits (`graph do <action> --param"
            " <name>=<value> --dry-run`, read what it will change, then run it without"
            " `--dry-run`). Otherwise preview"
            if doc.actions
            else "- Preview"
        )
        + " `create`, `apply`, `transition`, `supersede` and `retract` with `--dry-run`, then"
        " run them without; one `update`, `link` or `unlink` may run directly. Say why in"
        " `--reason` (in English, whatever language the values are in), and pass `--expect"
        " <rev>` when the write depends on what you read. A"
        " change set for `graph apply` goes into a file in the working directory.",
        "- Read `data.findings` of every write and tell the user about each warning.",
        *(
            [
                "",
                f"The graph stores files: `graph file add <file> --graph {name} --json`"
                " stores one, and entities point to it with `files:from_file` and the page;"
                " the `graph-cli` skill section Files says how.",
            ]
            if FILE_TYPE in doc.types
            else []
        ),
    ]


def _language(doc: _Model) -> list[str]:
    """The graph language, and the tagged form when the model has language-tagged fields."""
    if doc.language is None:
        return []
    text = f"Text values are written in the graph language {code(doc.language)}."
    tagged = any(
        req_str(f, "base") in LANG_BASES
        for body in doc.types.values()
        for f in _objects(body, "fields")
    )
    if tagged:
        text += (
            " A field of base `lang_string` or `lang_text` takes another language as"
            f" `<text>@<tag>`; without a tag it gets {code(doc.language)}."
        )
    return ["", text]


def _types(doc: _Model) -> list[str]:
    rows = []
    for curie in doc.listed:
        body = doc.types[curie]
        alias = doc.prefixes.get(curie)
        name = code(curie) + (f" ({code(format_alias(alias, 1))})" if alias else "")
        fields = [_field(f) for f in _ordered_fields(body) if f.get("kind") != "lifecycle"]
        ancestors = _strings(body.get("ancestors"))  # relations of a supertype apply too
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


def _ordered_fields(body: JsonObject) -> list[JsonObject]:
    """Names first (main, other, hidden), then required fields, then the rest; by name."""
    roles = list(ROLE_NAMES)

    def key(field: JsonObject) -> tuple[int, int, str]:
        role = field.get("role")
        rank = roles.index(role) if isinstance(role, str) and role in roles else len(roles)
        return (rank, 0 if field.get("required") is True else 1, req_str(field, "name"))

    return sorted(_objects(body, "fields"), key=key)


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
    values = _strings(field.get("values"))
    if values:
        notes.append(f"one of {codes(values)}")
    role = field.get("role")
    if isinstance(role, str) and role in ROLE_MARKS:
        notes.append(ROLE_MARKS[role])
    notes += _marked(field)
    name = code(req_str(field, "name"))
    return f"{name} ({', '.join(notes)})" if notes else name


def _marked(field: JsonObject) -> list[str]:
    """The mark of a marked field, ``marked ex:personal`` (conjunctive markings joined by
    ``+``); nothing for an unmarked one."""
    markings = _strings(field.get("markings"))
    return [f"marked {'+'.join(markings)}"] if markings else []


def _states(body: JsonObject) -> str:
    parts = []
    for lifecycle in _objects(body, "lifecycles"):
        pairs = [(req_str(t, "from"), req_str(t, "to")) for t in _objects(lifecycle, "transitions")]
        shown: list[str] = []
        one_way: dict[str, list[str]] = {}  # to -> from states, in model order
        for a, b in pairs:
            if (b, a) not in pairs:
                one_way.setdefault(b, []).append(a)
            elif (a, b) < (b, a):
                shown.append(f"{code(a)} <-> {code(b)}")
        shown += [f"{codes(froms, ' or ')} -> {code(to)}" for to, froms in one_way.items()]
        initial = codes(_strings(lifecycle.get("initial")))
        terminal = codes(_strings(lifecycle.get("terminal")))
        text = f"{code(_attr(lifecycle))}: " + ", ".join(shown)
        text += f"; initial {initial}" if initial else ""
        text += f"; terminal {terminal}" if terminal else ""
        parts.append(text)
    return "; ".join(parts) or "none"


def _attr(lifecycle: JsonObject) -> str:
    return opt_str(lifecycle, "attr") or "status"


def _relations(doc: _Model) -> list[str]:
    """One line per relation: CURIE, its ``graph query`` pattern, cardinality and notes, then
    the summary. A list, not a table: a pattern with alternative labels (``(:A|B)``) stays as
    it is typed, where a table cell would have to escape the pipe."""
    lines = []
    for curie in doc.touching():
        relation = doc.relations[curie]
        ends = (
            code(doc.patterns[curie])
            if curie in doc.patterns
            else f"{_ends(relation, 'domain')} -> {_ends(relation, 'range')}"
        )
        facts = ", ".join(
            part
            for part in (
                _cardinality(req_obj(relation, "cardinality")),
                _relation_notes(relation, inverse=False),
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


def _patterns(doc: _Model, curies: Sequence[str]) -> list[str]:
    """The relations as ``graph query`` patterns, one per line."""
    lines = [doc.patterns[curie] for curie in curies if curie in doc.patterns]
    if not lines:
        return []
    return ["", "As patterns for `graph query`:", "", "```", *lines, "```"]


def _ends(relation: JsonObject, key: str) -> str:
    ends = _strings(relation.get(key))
    return codes(ends, " or ") if ends else "any"


def _cardinality(cardinality: JsonObject) -> str:
    out, into = req_str(cardinality, "out"), req_str(cardinality, "in")
    return f"out {out}" if into == "0..n" else f"out {out}, in {into}"


_POLICY_NOTES: Final = {
    "refuse": "retracting or superseding a target is refused while active edges point at it",
    "warn": "retracting or superseding a target warns while active edges point at it",
}


def _relation_notes(relation: JsonObject, *, inverse: bool = True) -> str:
    """Characteristics, target policy, qualifiers and, unless left to level 2
    (``references/model.md``), the inverse label, which no command takes."""
    notes = list(_strings(relation.get("characteristics")))
    policy = opt_str(relation, "on_target_inactive")
    if policy in _POLICY_NOTES:
        notes.append(_POLICY_NOTES[policy])
    qualifiers = _strings(relation.get("qualifiers"))
    if qualifiers:
        notes.append(f"qualifiers {codes(qualifiers)}")
    label = opt_str(relation, "inverse_label")
    if label and inverse:
        notes.append(f"inverse {code(label)}")
    return ", ".join(notes)


def _constraints(doc: _Model) -> list[str]:
    lines = []
    for curie in doc.listed:
        for lifecycle in _objects(doc.types[curie], "lifecycles"):
            guarded: dict[tuple[str, str, str], list[str]] = {}  # (to, id, summary) -> froms
            for transition in _objects(lifecycle, "transitions"):
                for guard in _objects(transition, "guards"):
                    key = (
                        req_str(transition, "to"),
                        req_str(guard, "id"),
                        plain(opt_str(guard, "summary")),
                    )
                    guarded.setdefault(key, []).append(req_str(transition, "from"))
            for (to, guard_id, summary), froms in guarded.items():
                lines.append(
                    f"- {code(curie)} {code(_attr(lifecycle))} {codes(froms, ' or ')} ->"
                    f" {code(to)}: {summary} (guard {code(guard_id)})"
                )
    for curie in doc.listed if "types" not in doc.blocks else ():  # ``types`` marks them
        once = [
            req_str(f, "name")
            for f in _objects(doc.types[curie], "fields")
            if f.get("mutability") == "immutable"
        ]
        if once:
            lines.append(
                f"- Write-once on {code(curie)} (set at create, never changed): {codes(once)}"
            )
    rules: dict[str, tuple[JsonObject, list[str]]] = {}
    for curie in doc.listed:
        for rule in _objects(doc.types[curie], "rules"):
            rules.setdefault(req_str(rule, "id"), (rule, []))[1].append(curie)
    for rule_id, (rule, on) in sorted(rules.items()):
        when = req_str(rule, "when")
        lines.append(
            f"- Rule {code(rule_id)} on {codes(on)} ({req_str(rule, 'severity')},"
            f" {WHEN.get(when, f'when {when}')}): {term_summary(rule)}"
        )
    return lines or ["No transition guards, write-once fields or rules."]


def _names(doc: _Model) -> list[str]:
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
    labelled = _labelled(doc)
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
            lines.append(f"  - {code(curie)}: {_name_fields(fields)}")
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
        f"- Before you create an entity of {these}, search for its name and its"
        " short forms. If one is the same thing, use it and add the new name to its"
        f" {ROLE_NAMES['alt_label']} field instead of creating another. When you create one,"
        f" fill its {ROLE_NAMES['alt_label']} fields with the abbreviations and synonyms people"
        " use. A write finding `data.semantic.possible_duplicate` names entities that"
        " already carry the name or a near spelling of it."
    )
    return lines


def _labelled(doc: _Model) -> dict[str, list[JsonObject]]:
    """The name fields (label roles) of every concrete type that has any."""
    found = {
        curie: [f for f in _ordered_fields(body) if f.get("role") in ROLE_NAMES]
        for curie, body in doc.types.items()
        if body.get("abstract") is not True
    }
    return {curie: fields for curie, fields in found.items() if fields}


def _name_fields(fields: Sequence[JsonObject]) -> str:
    return ", ".join(
        f"{code(req_str(f, 'name'))} ({ROLE_NAMES[req_str(f, 'role')]})" for f in fields
    )


def _module_links(item: JsonObject) -> str:
    links = [
        f"{code(req_str(k, 'module'))} ({k['out']} out, {k['in']} in)"
        for k in _objects(item, "links")
    ]
    return ", ".join(links) or "none"


def _modules(doc: _Model) -> list[str]:
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


def _functions(doc: _Model) -> list[str]:
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


def _function_hint(doc: _Model) -> list[str]:
    return [
        "",
        f"Run one with `graph run <function> --param <name>=<value> --graph {doc.name} --json`;"
        f" `graph describe function <function> --graph {doc.name}` gives its columns and when to"
        " use it.",
    ]


def _functions_file(doc: _Model, stamp: str) -> str:
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


def _module_functions(doc: _Model, module_id: str) -> list[str]:
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


def _actions(doc: _Model) -> list[str]:
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


def _action_hint(doc: _Model) -> list[str]:
    return [
        "",
        f"Dry run first: `graph do <action> --param <name>=<value> --graph {doc.name}"
        " --dry-run`, read what it will change, then run it without `--dry-run`;"
        f" `graph describe action <action> --graph {doc.name}` gives its preconditions and when"
        " to use it.",
    ]


def _actions_file(doc: _Model, stamp: str) -> str:
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


def _module_actions(doc: _Model, module_id: str) -> list[str]:
    """The ``Actions`` section of a module page (none for a graph without actions)."""
    if not doc.actions:
        return []
    own = [a for a in doc.actions if opt_str(a, "module") == module_id]
    if not own:
        return ["", "## Actions", "", "None."]
    return ["", "## Actions", "", *_action_table(own), *_action_hint(doc)]


def _model_pointer(doc: _Model) -> list[str]:
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


_BLOCKS: Final[Mapping[str, Callable[[_Model], list[str]]]] = {
    "start": _start,
    "modules": _modules,
    "types": _types,
    "relations": _relations,
    "constraints": _constraints,
    "names": _names,
    "model": _model_pointer,
    "functions": _functions,
    "actions": _actions,
}


# ---- references/model.md -----------------------------------------------------------


def _model_file(doc: _Model, stamp: str) -> str:
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
                    _ends(relation, "domain"),
                    _ends(relation, "range"),
                    _cardinality(req_obj(relation, "cardinality")),
                    _relation_notes(relation),
                    term_summary(relation),
                ]
                for curie, relation in sorted(doc.relations.items())
            ],
        )
        lines += _patterns(doc, sorted(doc.relations))
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
    ancestors = _strings(body.get("ancestors"))
    if ancestors:
        facts.append(f"Is a {codes(ancestors)}.")
    subtypes = _strings(body.get("subtypes"))
    if subtypes:
        facts.append(f"Subtypes: {codes(subtypes)}.")
    if facts:
        lines += ["", " ".join(facts)]
    fields = _objects(body, "fields")
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
    for lifecycle in _objects(body, "lifecycles"):
        lines += ["", *_lifecycle_section(lifecycle)]
    out = _objects(body, "relations_out")
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
                        _ends(r, "range"),
                        req_str(r, "cardinality"),
                        code(str(r["inverse_label"])) if r.get("inverse_label") else "",
                        codes(_strings(r.get("qualifiers"))),
                        term_summary(r),
                    ]
                    for r in out
                ],
            ),
        ]
    into = _objects(body, "relations_in")
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
                        _ends(r, "from"),
                        req_str(r, "cardinality"),
                        code(str(r["inverse_label"])) if r.get("inverse_label") else "",
                        term_summary(r),
                    ]
                    for r in into
                ],
            ),
        ]
    rules = _objects(body, "rules")
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
    flags += _marked(field)
    return ", ".join(flags)


def _limits(field: JsonObject) -> str:
    parts = []
    values = _strings(field.get("values"))
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
        f"Lifecycle {code(_attr(lifecycle))} ({code(req_str(lifecycle, 'lifecycle'))}):"
        f" {term_summary(lifecycle)}",
        "",
    ]
    initial = set(_strings(lifecycle.get("initial")))
    terminal = set(_strings(lifecycle.get("terminal")))
    for state in _objects(lifecycle, "states"):
        name = req_str(state, "name")
        marks = [m for m, s in (("initial", initial), ("terminal", terminal)) if name in s]
        mark = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"- State {code(name)}{mark}: {plain(opt_str(state, 'summary'))}")
    for transition in _objects(lifecycle, "transitions"):
        guards = _objects(transition, "guards")
        text = f"- {code(req_str(transition, 'from'))} -> {code(req_str(transition, 'to'))}"
        if guards:
            text += ": " + "; ".join(
                f"guard {code(req_str(g, 'id'))}: {plain(opt_str(g, 'summary'))}" for g in guards
            )
        lines.append(text)
    return lines


# ---- modular graphs: references/model.md as an index, references/modules/<module>.md -----


def _page_link(doc: _Model, module_id: str) -> str:
    return f"[{module_id}.md](modules/{module_id}.md)" if module_id in doc.pages else ""


def _model_index(doc: _Model, stamp: str) -> str:
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


def _module_page(doc: _Model, module_id: str, stamp: str) -> str:
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
    types = _objects(item, "types")
    labelled = _labelled(doc)
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
                    _name_fields(labelled.get(req_str(t, "curie"), [])),
                    term_summary(t),
                ]
                for t in types
            ],
        )
        if types
        else ["None."]
    )
    lines += ["", "## Relations inside the module", ""]
    relations = _objects(item, "relations")
    lines += (
        md_table(
            ["Relation", "From", "To", "Cardinality", "Summary"],
            [
                [
                    code(req_str(r, "curie")),
                    _ends(r, "domain"),
                    _ends(r, "range"),
                    _cardinality(req_obj(r, "cardinality")),
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
        links = _objects(item, key)
        lines += ["", f"## {title}", ""]
        lines += (
            md_table(
                ["Relation", "From", "To", "Other modules", "Summary"],
                [
                    [
                        code(req_str(k, "relation")),
                        _ends(k, "from"),
                        _ends(k, "to"),
                        _link_where(k, module_id),
                        term_summary(k),
                    ]
                    for k in links
                ],
            )
            if links
            else ["None."]
        )
    rules = _objects(item, "rules")
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
