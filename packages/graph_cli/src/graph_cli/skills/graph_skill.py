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

The summary of a deprecated term starts with "Deprecated." unless the
author already wrote it that way.

Blocks (each one optional):

- ``start``: how to tell the graph is reachable, load the ``graph-cli`` skill before the
  first command, and two of its ground rules (plain calls, ``query`` for a few fields);
  for a model with ``files:File``, one line on the file workflow.
- ``modules``: the subject areas (ontology modules) of the graph as a table (module, prefix,
  summary, number of types, links to other modules; built-in modules in one line) and the
  commands to read one module (``describe module``), find a term (``describe search``) and
  read a type (``describe type``). For a modular graph it takes the place of ``types`` and
  ``relations`` in the template.
- ``types``: the entity types (types with an alias prefix; without any, every
  concrete type) with their supertypes, fields, enum values inline (F11), and
  states.
- ``relations``: the relations touching those types, and the same relations as
  ``graph query`` patterns (``(:A)-[:rel]->(:B)``, from ``describe schema``).
- ``constraints``: transition guards, write-once fields and rules.
- ``names``: the live alias format (F12), what ``graph lookup`` matches per
  type (label roles) and the rule to ask when the match is not unique (F24). On a modular
  graph it names one alias and points at the module pages and ``describe type`` for the
  prefixes and name fields, so its size does not grow with the types.
- ``model``: a pointer to ``references/model.md`` and ``graph describe``.

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

from graph_cli.kernel.canonical import JsonObject, JsonValue, canonical_bytes
from graph_cli.kernel.ids import ALIAS_DIGITS, format_alias
from graph_cli.kernel.json_access import as_list, as_object, opt_str, req_list, req_obj, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, skill_hash
from graph_cli.skills.markdown import table
from graph_cli.skills.template import MODEL_FILE, MODULES_DIR, Template, parse_template
from graph_cli.skills.tree import SKILL_FILE, Problem, SkillFiles, validate

MODEL_SCHEMA: Final = "graph_cli.model/1"
GENERATOR: Final = "graph_cli.skills"
ROLE_NAMES: Final[Mapping[str, str]] = {
    "label": "main name",
    "alt_label": "other names",
    "hidden_label": "hidden names, matched but never shown",
}
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
# Level 0 of the disclosure levels: the most bytes ``SKILL.md`` of a modular graph's skill
# should have (about 5000 tokens at 4 bytes a token, the size the Agent Skills guidance
# suggests for a skill body). A test holds it for 50 modules of any size; the blocks of a
# modular graph grow with the modules only. Not checked on publish.
L0_BUDGET: Final = 20_000
WHEN: Final[Mapping[str, str]] = {"write": "checked on every write", "batch": "checked in batch"}


def render_graph_skill(
    template: Template, model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]
) -> dict[str, str]:
    """``{relative path: content}`` of the skill; ``ValueError`` on documents it cannot read."""
    doc = _Model.of(model, graph)
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
    }


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
    bundle: str
    version: str
    prefixes: Mapping[str, str]  # type CURIE -> alias prefix, types of the model only
    types: Mapping[str, JsonObject]  # CURIE -> the ``type`` body of ``describe type``
    relations: Mapping[str, JsonObject]  # CURIE -> one item of ``describe relations``
    patterns: Mapping[str, str]  # relation CURIE -> its ``describe schema`` pattern
    modules: Sequence[JsonObject]  # the items of ``describe modules``, built-ins included
    pages: Mapping[str, JsonObject]  # module id -> ``module`` body of ``describe module``

    @staticmethod
    def of(model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]) -> _Model:
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


def _document(bundle: JsonObject, key: str) -> JsonObject:
    doc = req_obj(bundle, key)
    if doc.get("schema") != MODEL_SCHEMA:
        raise ValueError(f"{key}: expected {MODEL_SCHEMA}, got {doc.get('schema')!r}")
    return doc


def _objects(doc: JsonObject, key: str) -> list[JsonObject]:
    return [as_object(item, f"{key}[]") for item in req_list(doc, key)]


def _strings(value: JsonValue) -> list[str]:
    return [str(item) for item in as_list(value, "list")] if value is not None else []


# ---- Markdown pieces ---------------------------------------------------------------


def _code(text: str) -> str:
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _codes(names: Sequence[str], joiner: str = ", ") -> str:
    return joiner.join(_code(name) for name in names)


def _plain(text: str | None) -> str:
    return " ".join((text or "").split())


def _summary(term: JsonObject) -> str:
    """The summary of a term, marked when the term is deprecated."""
    text = _plain(opt_str(term, "summary"))
    if term.get("deprecated") is True and not text.lower().startswith("deprecated"):
        return f"Deprecated. {text}"
    return text


def _row(cells: Sequence[str]) -> list[str]:
    """Table cells: one line each, pipes escaped (also inside code spans, GFM)."""
    return [_plain(cell).replace("|", "\\|") for cell in cells]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return table(header, [_row(row) for row in rows])


def _value(value: JsonValue) -> str:
    return _code(value if isinstance(value, str) else canonical_bytes(value).decode("utf-8"))


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
    title = f" (title: {_plain(doc.title)})" if doc.title else ""
    return [
        f"Run `graph graphs --json`. Use this skill only if an item named {_code(doc.name)} is"
        f" listed{title}. Pass `--graph {doc.name}` to every command, as the examples here do,"
        f" so they work in every directory; a project whose pointer makes {_code(doc.name)}"
        " the default may leave it out.",
        "",
        "Before your first `graph` command, load the `graph-cli` skill as well: it explains"
        " the commands, the output envelope and safe writes. This skill says what the graph"
        " holds. Two of its ground rules apply from the first call:",
        "",
        "- Run each call as a plain command that starts with `graph`: no `cd`, variables,"
        " loops, pipes or heredocs, which a restricted harness refuses. If a call is"
        " refused, run it again in that form before you give up.",
        "- For a few fields, a count or a join, use `graph query` and `RETURN` only those"
        f" columns ({_patterns_source(doc)}), or"
        " `--fields <attr>,<attr>` on `find`, `get` and `neighbors`, instead of filtering"
        " JSON with another program.",
        *(
            [
                "",
                f"The graph stores files: `graph file add <file> --graph {doc.name} --json`"
                " stores one, and entities point to it with `files:from_file` and the page;"
                " the `graph-cli` skill section Files says how.",
            ]
            if FILE_TYPE in doc.types
            else []
        ),
    ]


def _types(doc: _Model) -> list[str]:
    rows = []
    for curie in doc.listed:
        body = doc.types[curie]
        alias = doc.prefixes.get(curie)
        name = _code(curie) + (f" ({_code(format_alias(alias, 1))})" if alias else "")
        fields = [_field(f) for f in _ordered_fields(body) if f.get("kind") != "lifecycle"]
        ancestors = _strings(body.get("ancestors"))  # relations of a supertype apply too
        what = _summary(body)
        what += f" Is a {_codes(ancestors, ' and ')}." if ancestors else ""
        rows.append(
            [
                name,
                what.strip(),
                "; ".join(fields) or "none",
                _states(body),
            ]
        )
    lines = _table(["Type", "What it is", "Fields", "States"], rows)
    others = [curie for curie in doc.types if curie not in doc.listed]
    if others:
        lines += [
            "",
            f"Other types of the model, described in `{MODEL_FILE}`: {_codes(others)}.",
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
        notes.append(f"default {_value(field['default'])}")
    values = _strings(field.get("values"))
    if values:
        notes.append(f"one of {_codes(values)}")
    name = _code(req_str(field, "name"))
    return f"{name} ({', '.join(notes)})" if notes else name


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
                shown.append(f"{_code(a)} <-> {_code(b)}")
        shown += [f"{_codes(froms, ' or ')} -> {_code(to)}" for to, froms in one_way.items()]
        initial = _codes(_strings(lifecycle.get("initial")))
        terminal = _codes(_strings(lifecycle.get("terminal")))
        text = f"{_code(_attr(lifecycle))}: " + ", ".join(shown)
        text += f"; initial {initial}" if initial else ""
        text += f"; terminal {terminal}" if terminal else ""
        parts.append(text)
    return "; ".join(parts) or "none"


def _attr(lifecycle: JsonObject) -> str:
    return opt_str(lifecycle, "attr") or "status"


def _relations(doc: _Model) -> list[str]:
    rows = []
    for curie in doc.touching():
        relation = doc.relations[curie]
        rows.append(
            [
                _code(curie),
                f"{_ends(relation, 'domain')} -> {_ends(relation, 'range')}",
                _cardinality(req_obj(relation, "cardinality")),
                _relation_notes(relation),
                _summary(relation),
            ]
        )
    if not rows:
        return ["No relations."]
    table = _table(["Relation", "From -> to", "Cardinality", "Notes", "Summary"], rows)
    return [*table, *_patterns(doc, doc.touching())]


def _patterns(doc: _Model, curies: Sequence[str]) -> list[str]:
    """The relations as ``graph query`` patterns, one per line."""
    lines = [doc.patterns[curie] for curie in curies if curie in doc.patterns]
    if not lines:
        return []
    return ["", "As patterns for `graph query`:", "", "```", *lines, "```"]


def _ends(relation: JsonObject, key: str) -> str:
    ends = _strings(relation.get(key))
    return _codes(ends, " or ") if ends else "any"


def _cardinality(cardinality: JsonObject) -> str:
    out, into = req_str(cardinality, "out"), req_str(cardinality, "in")
    return f"out {out}" if into == "0..n" else f"out {out}, in {into}"


_POLICY_NOTES: Final = {
    "refuse": "retracting or superseding a target is refused while active edges point at it",
    "warn": "retracting or superseding a target warns while active edges point at it",
}


def _relation_notes(relation: JsonObject) -> str:
    notes = list(_strings(relation.get("characteristics")))
    policy = opt_str(relation, "on_target_inactive")
    if policy in _POLICY_NOTES:
        notes.append(_POLICY_NOTES[policy])
    qualifiers = _strings(relation.get("qualifiers"))
    if qualifiers:
        notes.append(f"qualifiers {_codes(qualifiers)}")
    inverse = opt_str(relation, "inverse_label")
    if inverse:
        notes.append(f"inverse {_code(inverse)}")
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
                        _plain(opt_str(guard, "summary")),
                    )
                    guarded.setdefault(key, []).append(req_str(transition, "from"))
            for (to, guard_id, summary), froms in guarded.items():
                lines.append(
                    f"- {_code(curie)} {_code(_attr(lifecycle))} {_codes(froms, ' or ')} ->"
                    f" {_code(to)}: {summary} (guard {_code(guard_id)})"
                )
    for curie in doc.listed:
        once = [
            req_str(f, "name")
            for f in _objects(doc.types[curie], "fields")
            if f.get("mutability") == "immutable"
        ]
        if once:
            lines.append(
                f"- Write-once on {_code(curie)} (set at create, never changed): {_codes(once)}"
            )
    rules: dict[str, tuple[JsonObject, list[str]]] = {}
    for curie in doc.listed:
        for rule in _objects(doc.types[curie], "rules"):
            rules.setdefault(req_str(rule, "id"), (rule, []))[1].append(curie)
    for rule_id, (rule, on) in sorted(rules.items()):
        when = req_str(rule, "when")
        lines.append(
            f"- Rule {_code(rule_id)} on {_codes(on)} ({req_str(rule, 'severity')},"
            f" {WHEN.get(when, f'when {when}')}): {_summary(rule)}"
        )
    return lines or ["No transition guards, write-once fields or rules."]


def _names(doc: _Model) -> list[str]:
    """Aliases and what ``lookup`` matches. A modular graph gets one example alias and the
    places that list the rest, so the block does not grow with the types (L0 budget)."""
    lines = []
    if doc.prefixes:
        prefix = next(iter(doc.prefixes.values()))
        first = _code(format_alias(prefix, 1))
        padding = (
            f"The number is zero-padded to {ALIAS_DIGITS} digits ({first}, never"
            f" {_code(prefix + '-1')}) and grows past 9999. A full id works everywhere; on"
            " the command line 8 or more trailing hex digits of it work too."
        )
        if doc.modular:
            lines.append(
                f"- Aliases such as {first} ({_code(next(iter(doc.prefixes)))}) start with the"
                " prefix of the entity's type; the module pages list the prefix of every type,"
                f" `graph describe type <type> --graph {doc.name} --json` names it"
                f" (`alias_prefix`). {padding}"
            )
        else:
            examples = ", ".join(
                f"{_code(curie)} {_code(format_alias(prefix, 1))}"
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
    if doc.modular:
        roles = "; ".join(f"{_code(role)}: {text}" for role, text in ROLE_NAMES.items())
        lines.append(
            f"{lookup} the name fields of a type, not other text. The module pages list them"
            f" per type; `graph describe type <type> --graph {doc.name} --json` marks them with"
            f" `role` ({roles})."
        )
    else:
        lines.append(f"{lookup} these fields, not other text:")
        for curie, fields in labelled.items():
            lines.append(f"  - {_code(curie)}: {_name_fields(fields)}")
    lines.append(
        "- Names are not unique, so `lookup` returns candidates. Use the returned alias or id"
        " only when exactly one candidate matches exactly (`match.kind` is `exact`). With"
        " several candidates, or only prefix matches, do not answer about one of them first:"
        " list them with their names and ask which one is meant, then answer."
    )
    lines.append(search)
    these = "a type with names" if doc.modular else "one of these types"
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
        f"{_code(req_str(f, 'name'))} ({ROLE_NAMES[req_str(f, 'role')]})" for f in fields
    )


def _module_links(item: JsonObject) -> str:
    links = [
        f"{_code(req_str(k, 'module'))} ({k['out']} out, {k['in']} in)"
        for k in _objects(item, "links")
    ]
    return ", ".join(links) or "none"


def _modules(doc: _Model) -> list[str]:
    """Level 0: the subject areas, then how to read one. Built-in modules (shared by every
    graph) get one line, not a row."""
    shown = [m for m in doc.modules if m.get("builtin") is not True]
    built = [req_str(m, "id") for m in doc.modules if m.get("builtin") is True]
    lines = _table(
        ["Module", "Prefix", "What it holds", "Types", "Links to other modules"],
        [
            [
                _code(req_str(m, "id")),
                _code(req_str(m, "prefix")),
                _plain(opt_str(m, "summary")),
                str(m["types"]),
                _module_links(m),
            ]
            for m in shown
        ],
    )
    if built:
        lines += ["", f"Built-in modules, shared by every graph: {_codes(built)}."]
    name = doc.name
    lines += [
        "",
        f"- Before you work in a module, read it: `graph describe module <module> --graph {name}"
        " --json` lists its types, its relations, its links to and from other modules (with the"
        " other module named) and its rules.",
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
        *_table(
            ["Type", "Alias", "Abstract", "Extends", "Summary"],
            [
                [
                    _code(curie),
                    _code(format_alias(doc.prefixes[curie], 1)) if curie in doc.prefixes else "",
                    "yes" if body.get("abstract") is True else "no",
                    _code(str(body["extends"])) if body.get("extends") else "",
                    _summary(body),
                ]
                for curie, body in doc.types.items()
            ],
        ),
        "",
        "## Relations",
        "",
    ]
    if doc.relations:
        lines += _table(
            ["Relation", "From", "To", "Cardinality", "Notes", "Summary"],
            [
                [
                    _code(curie),
                    _ends(relation, "domain"),
                    _ends(relation, "range"),
                    _cardinality(req_obj(relation, "cardinality")),
                    _relation_notes(relation),
                    _summary(relation),
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
    lines = [f"## `{curie}`", "", _summary(body) or "No summary."]
    description = opt_str(body, "description")
    if description:
        lines += ["", description.strip()]
    facts = []
    if body.get("abstract") is True:
        facts.append("Abstract (no entities of its own).")
    ancestors = _strings(body.get("ancestors"))
    if ancestors:
        facts.append(f"Is a {_codes(ancestors)}.")
    subtypes = _strings(body.get("subtypes"))
    if subtypes:
        facts.append(f"Subtypes: {_codes(subtypes)}.")
    if facts:
        lines += ["", " ".join(facts)]
    fields = _objects(body, "fields")
    lines += ["", "Fields:", ""]
    if fields:
        lines += _table(
            FIELD_COLUMNS,
            [
                [
                    _code(req_str(f, "name")),
                    f"{req_str(f, 'kind')} {_code(req_str(f, 'type'))}",
                    req_str(f, "base"),
                    _flags(f),
                    _value(f["default"]) if f.get("default") is not None else "",
                    req_str(f, "mutability"),
                    _limits(f),
                    _code(req_str(f, "declared_in")),
                    _summary(f),
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
            *_table(
                ["Relation", "To", "Cardinality", "Inverse", "Qualifiers", "Summary"],
                [
                    [
                        _code(req_str(r, "curie")),
                        _ends(r, "range"),
                        req_str(r, "cardinality"),
                        _code(str(r["inverse_label"])) if r.get("inverse_label") else "",
                        _codes(_strings(r.get("qualifiers"))),
                        _summary(r),
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
            *_table(
                ["Relation", "From", "Cardinality in", "Inverse", "Summary"],
                [
                    [
                        _code(req_str(r, "curie")),
                        _ends(r, "from"),
                        req_str(r, "cardinality"),
                        _code(str(r["inverse_label"])) if r.get("inverse_label") else "",
                        _summary(r),
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
            *_table(
                ["Rule", "Severity", "When", "Summary"],
                [
                    [
                        _code(req_str(r, "id")),
                        req_str(r, "severity"),
                        req_str(r, "when"),
                        _summary(r),
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
    return ", ".join(flags)


def _limits(field: JsonObject) -> str:
    parts = []
    values = _strings(field.get("values"))
    if values:
        parts.append(f"one of {_codes(values)}")
    shape = VALUE_SHAPES.get(req_str(field, "base"))
    if shape is not None:
        parts.append(shape)
    limits = req_obj(field, "limits")
    parts += [f"{name} {_value(limits[name])}" for name in sorted(limits)]
    return ", ".join(parts)


def _lifecycle_section(lifecycle: JsonObject) -> list[str]:
    lines = [
        f"Lifecycle {_code(_attr(lifecycle))} ({_code(req_str(lifecycle, 'lifecycle'))}):"
        f" {_summary(lifecycle)}",
        "",
    ]
    initial = set(_strings(lifecycle.get("initial")))
    terminal = set(_strings(lifecycle.get("terminal")))
    for state in _objects(lifecycle, "states"):
        name = req_str(state, "name")
        marks = [m for m, s in (("initial", initial), ("terminal", terminal)) if name in s]
        mark = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"- State {_code(name)}{mark}: {_plain(opt_str(state, 'summary'))}")
    for transition in _objects(lifecycle, "transitions"):
        guards = _objects(transition, "guards")
        text = f"- {_code(req_str(transition, 'from'))} -> {_code(req_str(transition, 'to'))}"
        if guards:
            text += ": " + "; ".join(
                f"guard {_code(req_str(g, 'id'))}: {_plain(opt_str(g, 'summary'))}" for g in guards
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
        *_table(
            ["Module", "Prefix", "Page", "Summary"],
            [
                [
                    _code(req_str(m, "id")),
                    _code(req_str(m, "prefix")),
                    _page_link(doc, req_str(m, "id")),
                    _plain(opt_str(m, "summary")),
                ]
                for m in doc.modules
            ],
        ),
        "",
        "## Types",
        "",
        *_table(
            ["Type", "Module", "Page", "Alias", "Abstract", "Extends", "Summary"],
            [
                [
                    _code(curie),
                    _code(req_str(body, "module")),
                    _page_link(doc, req_str(body, "module")),
                    _code(format_alias(doc.prefixes[curie], 1)) if curie in doc.prefixes else "",
                    "yes" if body.get("abstract") is True else "no",
                    _code(str(body["extends"])) if body.get("extends") else "",
                    _summary(body),
                ]
                for curie, body in doc.types.items()
            ],
        ),
    ]
    return "\n".join(lines) + "\n"


def _link_where(link: JsonObject, module_id: str) -> str:
    """The other modules at the ends; the declaring module too when it is neither end."""
    others = [str(m) for m in req_list(link, "other_modules")]
    where = _codes(others) or "any"
    owner = req_str(link, "module")
    if owner != module_id and owner not in others:
        where += f"; declared in {_code(owner)}"
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
        + _plain(opt_str(item, "summary")),
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
        _table(
            ["Type", "Alias", "Abstract", "Extends", "Names", "Summary"],
            [
                [
                    _code(req_str(t, "curie")),
                    _code(format_alias(doc.prefixes[req_str(t, "curie")], 1))
                    if req_str(t, "curie") in doc.prefixes
                    else "",
                    "yes" if t.get("abstract") is True else "no",
                    _code(str(t["extends"])) if t.get("extends") else "",
                    _name_fields(labelled.get(req_str(t, "curie"), [])),
                    _summary(t),
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
        _table(
            ["Relation", "From", "To", "Cardinality", "Summary"],
            [
                [
                    _code(req_str(r, "curie")),
                    _ends(r, "domain"),
                    _ends(r, "range"),
                    _cardinality(req_obj(r, "cardinality")),
                    _summary(r),
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
            _table(
                ["Relation", "From", "To", "Other modules", "Summary"],
                [
                    [
                        _code(req_str(k, "relation")),
                        _ends(k, "from"),
                        _ends(k, "to"),
                        _link_where(k, module_id),
                        _summary(k),
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
        _table(
            ["Rule", "Severity", "Summary"],
            [[_code(req_str(r, "id")), req_str(r, "severity"), _summary(r)] for r in rules],
        )
        if rules
        else ["None."]
    )
    return "\n".join(lines) + "\n"
