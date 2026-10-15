"""Generated graph skills: a curated template rendered with the graph's live model.

``render_graph_skill`` is a pure function of the template
(``graph_cli.skills.template``), the ``graph_cli.model/1`` documents of the
graph (``{types, type: {curie: document}, relations}``, what ``graph
describe`` prints) and the graph block ``{id, name, title, language,
alias_prefixes}``. Each block marker in ``SKILL.md`` becomes a generated
block; ``references/model.md`` describes every type of the model. Only the
model and the manifest go in, never graph data (no counts, no records).

A stamp after the frontmatter names the graph, the ontology release and the
template hash, never the program version or a time, so a new program with
the same render installs the same files.

Blocks (each one optional):

- ``start``: how to tell the graph is reachable, read the ``graph-cli`` skill first.
- ``types``: the entity types (types with an alias prefix; without any, every
  concrete type) with their supertypes, fields, enum values inline (F11), and
  states.
- ``relations``: the relations touching those types.
- ``constraints``: transition guards, write-once fields and rules.
- ``names``: the live alias format (F12), what ``graph lookup`` matches per
  type (label roles) and the rule to ask when the match is not unique (F24).
- ``model``: a pointer to ``references/model.md`` and ``graph describe``.
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
from graph_cli.skills.template import MODEL_FILE, Template, parse_template
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
    return {**template.files, SKILL_FILE: "\n".join(lines), MODEL_FILE: _model_file(doc, stamp)}


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

    @staticmethod
    def of(model: Mapping[str, JsonValue], graph: Mapping[str, JsonValue]) -> _Model:
        bundle = as_object(dict(model), "model")
        listing = _document(bundle, "types")
        relations = _document(bundle, "relations")
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
        )

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


def _start(doc: _Model) -> list[str]:
    title = f" (title: {_plain(doc.title)})" if doc.title else ""
    return [
        f"Run `graph graphs --json`. Use this skill only if an item named {_code(doc.name)} is"
        f" listed{title}. Pass `--graph {doc.name}` to every command, as the examples here do,"
        f" so they work in every directory; a project whose pointer makes {_code(doc.name)}"
        " the default may leave it out.",
        "",
        "Read the `graph-cli` skill first: it explains the commands, the output envelope and"
        " safe writes. This skill says what the graph holds.",
    ]


def _types(doc: _Model) -> list[str]:
    rows = []
    for curie in doc.listed:
        body = doc.types[curie]
        alias = doc.prefixes.get(curie)
        name = _code(curie) + (f" ({_code(format_alias(alias, 1))})" if alias else "")
        fields = [_field(f) for f in _ordered_fields(body) if f.get("kind") != "lifecycle"]
        ancestors = _strings(body.get("ancestors"))  # relations of a supertype apply too
        what = _plain(opt_str(body, "summary"))
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
                _plain(opt_str(relation, "summary")),
            ]
        )
    if not rows:
        return ["No relations."]
    return _table(["Relation", "From -> to", "Cardinality", "Notes", "Summary"], rows)


def _ends(relation: JsonObject, key: str) -> str:
    ends = _strings(relation.get(key))
    return _codes(ends, " or ") if ends else "any"


def _cardinality(cardinality: JsonObject) -> str:
    out, into = req_str(cardinality, "out"), req_str(cardinality, "in")
    return f"out {out}" if into == "0..n" else f"out {out}, in {into}"


def _relation_notes(relation: JsonObject) -> str:
    notes = list(_strings(relation.get("characteristics")))
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
            f" {WHEN.get(when, f'when {when}')}): {_plain(opt_str(rule, 'summary'))}"
        )
    return lines or ["No transition guards, write-once fields or rules."]


def _names(doc: _Model) -> list[str]:
    lines = []
    if doc.prefixes:
        examples = ", ".join(
            f"{_code(curie)} {_code(format_alias(prefix, 1))}"
            for curie, prefix in doc.prefixes.items()
        )
        prefix = next(iter(doc.prefixes.values()))
        lines.append(
            f"- Aliases: {examples}. The number is zero-padded to"
            f" {ALIAS_DIGITS} digits ({_code(format_alias(prefix, 1))}, never"
            f" {_code(prefix + '-1')}) and grows past 9999. A full id works everywhere; on"
            " the command line 8 or more trailing hex digits of it work too."
        )
    labelled = [
        (curie, [f for f in _ordered_fields(body) if f.get("role") in ROLE_NAMES])
        for curie, body in doc.types.items()
        if body.get("abstract") is not True
    ]
    labelled = [(curie, fields) for curie, fields in labelled if fields]
    if not labelled:
        lines.append(
            "- No type of this graph has names (label roles), so `graph lookup` finds nothing;"
            f" use `graph find --graph {doc.name}`."
        )
        return lines
    lines.append(
        f"- `graph lookup <value> --graph {doc.name} --json` finds entities by name, ignoring"
        " case; it matches these fields, not other text:"
    )
    for curie, fields in labelled:
        matched = ", ".join(
            f"{_code(req_str(f, 'name'))} ({ROLE_NAMES[req_str(f, 'role')]})" for f in fields
        )
        lines.append(f"  - {_code(curie)}: {matched}")
    lines.append(
        "- Names are not unique, so `lookup` returns candidates. Use the returned alias or id"
        " only when exactly one candidate matches exactly (`match.kind` is `exact`). With"
        " several candidates, or only prefix matches, list them with their names and ask"
        " which one is meant; never answer about the closest one on your own."
    )
    return lines


def _model_pointer(doc: _Model) -> list[str]:
    return [
        f"Every type with all its fields, lifecycles, relations and rules: [{MODEL_FILE}]"
        f"({MODEL_FILE}). The live source, which wins when they differ:"
        f" `graph describe type <type> --graph {doc.name} --json`."
    ]


_BLOCKS: Final[Mapping[str, Callable[[_Model], list[str]]]] = {
    "start": _start,
    "types": _types,
    "relations": _relations,
    "constraints": _constraints,
    "names": _names,
    "model": _model_pointer,
}


# ---- references/model.md -----------------------------------------------------------


def _model_file(doc: _Model, stamp: str) -> str:
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
                    _plain(opt_str(body, "summary")),
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
                    _plain(opt_str(relation, "summary")),
                ]
                for curie, relation in sorted(doc.relations.items())
            ],
        )
    else:
        lines.append("No relations.")
    for curie, body in doc.types.items():
        lines += ["", *_type_section(curie, body)]
    return "\n".join(lines) + "\n"


def _type_section(curie: str, body: JsonObject) -> list[str]:
    lines = [f"## `{curie}`", "", _plain(opt_str(body, "summary")) or "No summary."]
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
                    _plain(opt_str(f, "summary")),
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
                        _plain(opt_str(r, "summary")),
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
                        _plain(opt_str(r, "summary")),
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
                        _plain(opt_str(r, "summary")),
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
    limits = req_obj(field, "limits")
    parts += [f"{name} {_value(limits[name])}" for name in sorted(limits)]
    return ", ".join(parts)


def _lifecycle_section(lifecycle: JsonObject) -> list[str]:
    lines = [
        f"Lifecycle {_code(_attr(lifecycle))} ({_code(req_str(lifecycle, 'lifecycle'))}):"
        f" {_plain(opt_str(lifecycle, 'summary'))}",
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
