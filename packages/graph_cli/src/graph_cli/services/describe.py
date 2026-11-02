"""Model description ``graph_cli.model/1`` of a graph's compiled ontology (contracts B11.5).

Projections for agents: the modules (subject areas) and one module, types, one type with
its fields, lifecycles and relations in both directions, relations, fields, lifecycles, and
the schema as ``graph query`` (Cypher) sees it, and the named functions of the graph
(``describe_functions``, ``describe_function``) and its actions (``describe_actions``,
``describe_action``); ``modules`` and ``module`` list them when the caller passes the visible
ones; the list projections take a ``module``
filter. Every term with a summary also carries its ``deprecated`` flag. Deterministic
(sorted), no timestamps. A type or lifecycle is its CURIE or its unique
local name, as for ``find`` (``query.plan.resolve_term``); unknown terms
are ``term.unknown`` with ``did_you_mean``. Functions and actions of a graph entry and its
compiled model, so the app (a read view) and graph-admin (the pinned model) describe a graph
the same way; ``describe_model`` bundles every document a graph skill is
rendered from.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kinetic.action_entries import ActionEntry, find_action
from graph_cli.kinetic.entries import FunctionEntry, find_entry
from graph_cli.ontology_layer.model import (
    AttributeDef,
    Bound,
    ClassDef,
    CompiledModel,
    LifecycleDef,
    QualifierDef,
    RelationDef,
)
from graph_cli.ontology_layer.vocabulary import BUILTIN_MODULES, SYSTEM_FIELDS
from graph_cli.query.plan import resolve_term, unknown_term

MODEL_SCHEMA: Final = "graph_cli.model/1"
ANY_END: Final = "any"  # how a link names a relation end without domain or range


@dataclass(frozen=True, slots=True)
class ModuleLink:
    """A cross-module relation as one module sees it: ``from -relation-> to``, declared by
    module ``owner``. An end without classes (any class) is ``()``."""

    relation: str
    owner: str
    domain: tuple[str, ...]
    range: tuple[str, ...]


class ModuleIndex:
    """Subject areas of a compiled model, derived at read time: a term
    belongs to the module whose prefix its CURIE carries, a relation to the module that
    declares it. Pure; nothing is stored in the compiled form.

    A relation is cross-module when its domain or range holds a class of another module
    than the relation's own, or an end is open (any class). Per module, a cross-module
    relation is ``out`` when the module holds a class of its domain, ``in`` when it holds a
    class of its range only. A relation with an open end is listed once under its own
    module, with the open end as ``any`` (and under the module of the other end). A relation
    whose classes all belong to other modules than its own (a bridge) is ``out`` of its
    own module."""

    def __init__(self, model: CompiledModel) -> None:
        self._model = model
        self._by_prefix = {m.prefix: m.id for m in model.modules}
        self.infos = {m.id: m for m in model.modules}

    def owner(self, curie: str) -> str:
        """The id of the module that owns the term (class, relation, ...) ``curie``."""
        return self._by_prefix[curie.partition(":")[0]]

    def builtin(self, module_id: str) -> bool:
        return module_id in BUILTIN_MODULES

    def resolve(self, name: str) -> str:
        """A module id from its id or prefix (``term.unknown`` with ``did_you_mean``)."""
        if name in self.infos:
            return name
        if name in self._by_prefix:
            return self._by_prefix[name]
        raise unknown_term("module", name, [*self.infos, *self._by_prefix])

    def types(self, module_id: str) -> list[str]:
        return [c for c in sorted(self._model.classes) if self.owner(c) == module_id]

    def relations(self, module_id: str) -> list[str]:
        """The relations the module declares."""
        return [r for r in sorted(self._model.relations) if self.owner(r) == module_id]

    def _ends(self, relation: str) -> tuple[RelationDef, set[str], set[str]]:
        r = self._model.relations[relation]
        return r, {self.owner(c) for c in r.domain}, {self.owner(c) for c in r.range}

    def is_cross_module(self, relation: str) -> bool:
        r, domain, range_ = self._ends(relation)
        own = self.owner(relation)
        return not r.domain or not r.range or bool((domain | range_) - {own})

    def touches(self, module_id: str, relation: str) -> bool:
        """The module declares the relation or holds a class at one of its ends."""
        _, domain, range_ = self._ends(relation)
        return self.owner(relation) == module_id or module_id in domain | range_

    def _out(self, module_id: str, relation: str) -> bool:
        r, domain, range_ = self._ends(relation)
        own = self.owner(relation)
        if not self.is_cross_module(relation):
            return False
        if own == module_id and (not r.domain or not r.range):
            return True
        if module_id in domain:
            return not r.range or bool(range_ - {module_id})
        return own == module_id and module_id not in domain | range_

    def _in(self, module_id: str, relation: str) -> bool:
        r, domain, range_ = self._ends(relation)
        if not self.is_cross_module(relation) or self._out(module_id, relation):
            return False
        return module_id in range_ and (not r.domain or bool(domain - {module_id}))

    def _link(self, relation: str) -> ModuleLink:
        r = self._model.relations[relation]
        return ModuleLink(relation, self.owner(relation), r.domain, r.range)

    def links_out(self, module_id: str) -> list[ModuleLink]:
        return [self._link(r) for r in sorted(self._model.relations) if self._out(module_id, r)]

    def links_in(self, module_id: str) -> list[ModuleLink]:
        return [self._link(r) for r in sorted(self._model.relations) if self._in(module_id, r)]

    def internal(self, module_id: str) -> list[str]:
        """The relations the module declares that stay inside it."""
        return [r for r in self.relations(module_id) if not self.is_cross_module(r)]

    def neighbours(self, module_id: str) -> list[dict[str, JsonValue]]:
        """The other modules ``module_id`` connects to, with the number of relations that
        leave it (``out``) and arrive at it (``in``)."""
        out: Counter[str] = Counter()
        into: Counter[str] = Counter()
        for link in self.links_out(module_id):
            out.update({self.owner(c) for c in link.range} - {module_id})
        for link in self.links_in(module_id):
            into.update({self.owner(c) for c in link.domain} - {module_id})
        return [
            {"module": other, "out": out[other], "in": into[other]}
            for other in sorted(set(out) | set(into))
        ]

    def filter(self, name: str | None) -> str | None:
        return None if name is None else self.resolve(name)


def _bound(bound: Bound) -> str:
    return f"{bound.min}..{'n' if bound.max is None else bound.max}"


def model_envelope(
    entry: GraphEntry, model: CompiledModel, **body: JsonValue
) -> dict[str, JsonValue]:
    return {
        "schema": MODEL_SCHEMA,
        "graph": {"id": str(entry.id), "name": entry.name, "language": entry.language},
        "ontology": {"bundle": model.bundle_id, "version": model.bundle_version},
        **body,
    }


def _class(model: CompiledModel, name: str) -> tuple[str, ClassDef]:
    curie = resolve_term("type", name, model.classes)
    return curie, model.classes[curie]


def _fields(model: CompiledModel, cls: ClassDef) -> list[JsonValue]:
    fields: list[JsonValue] = []
    for name, a in sorted(cls.attributes.items()):
        field: dict[str, JsonValue] = {
            "name": name,
            "type": a.type,
            "kind": a.kind,
            "base": model.value_base(a.kind, a.type),
            "required": a.required,
            "many": a.many,
            "unique": a.unique,
            "default": a.default,
            "mutability": a.mutability,
            "role": a.role,
            "limits": dict(model.facets(a.kind, a.type)),
            "values": list(model.enums[a.type].value_names()) if a.kind == "enum" else None,
            "declared_in": a.declared_in,
            "summary": a.doc.summary,
            "deprecated": a.doc.deprecated,
        }
        fields.append(field)
    return fields


def _lifecycle(curie: str, lifecycle: LifecycleDef, attr: str | None) -> dict[str, JsonValue]:
    return {
        "attr": attr,
        "lifecycle": curie,
        "states": [{"name": s.name, "summary": s.summary} for s in lifecycle.states],
        "initial": list(lifecycle.initial),
        "terminal": list(lifecycle.terminal),
        "transitions": [
            {
                "from": t.from_state,
                "to": t.to_state,
                "guards": [{"id": g.id, "summary": g.summary} for g in t.guards],
            }
            for t in lifecycle.transitions
        ],
        "summary": lifecycle.doc.summary,
        "deprecated": lifecycle.doc.deprecated,
    }


def _lifecycles(model: CompiledModel, cls: ClassDef) -> list[JsonValue]:
    return [
        _lifecycle(a.type, model.lifecycles[a.type], name)
        for name, a in sorted(cls.attributes.items())
        if a.kind == "lifecycle"
    ]


def _filtered(
    entry: GraphEntry, model: CompiledModel, module: str | None, **body: JsonValue
) -> dict[str, JsonValue]:
    """The envelope; a filtered answer names the module it is limited to."""
    return model_envelope(
        entry, model, **({"module": module} if module is not None else {}), **body
    )


def _type_item(curie: str, cls: ClassDef) -> dict[str, JsonValue]:
    return {
        "curie": curie,
        "abstract": cls.abstract,
        "extends": cls.extends,
        "summary": cls.doc.summary,
        "deprecated": cls.doc.deprecated,
    }


def describe_types(
    entry: GraphEntry, model: CompiledModel, *, module: str | None = None
) -> dict[str, JsonValue]:
    """The classes; with ``module`` (id or prefix) only those the module owns."""
    index = ModuleIndex(model)
    wanted = index.filter(module)
    types: list[JsonValue] = [
        _type_item(c, d)
        for c, d in sorted(model.classes.items())
        if wanted is None or index.owner(c) == wanted
    ]
    return _filtered(entry, model, wanted, types=types)


def _link(
    model: CompiledModel, index: ModuleIndex, link: ModuleLink, module_id: str
) -> dict[str, JsonValue]:
    """A cross-module link as seen from ``module_id``: ``module`` declares the relation,
    ``other_modules`` are the modules at its ends besides ``module_id`` (none for an open
    end only)."""
    r = model.relations[link.relation]
    ends = {index.owner(c) for c in (*link.domain, *link.range)}
    return {
        "relation": link.relation,
        "module": link.owner,
        "other_modules": sorted(ends - {module_id}),
        "from": list(link.domain) or [ANY_END],
        "to": list(link.range) or [ANY_END],
        "summary": r.doc.summary,
        "deprecated": r.doc.deprecated,
    }


class _Catalogued(Protocol):
    """What functions and actions share for the listings: the module and the status."""

    @property
    def module(self) -> str | None: ...

    @property
    def deprecated(self) -> bool: ...


def _active_in[Entry: _Catalogued](entries: Sequence[Entry], module_id: str) -> list[Entry]:
    return [e for e in entries if e.module == module_id and not e.deprecated]


def describe_modules(
    entry: GraphEntry,
    model: CompiledModel,
    functions: Sequence[FunctionEntry] | None = None,
    actions: Sequence[ActionEntry] | None = None,
) -> dict[str, JsonValue]:
    """Level 0: the subject areas of the graph, one item per module (sorted by id). With
    ``functions`` (the ones the caller may see; ``None``: the graph has none or they are
    hidden) every module also counts its active functions, with ``actions`` likewise."""
    index = ModuleIndex(model)
    modules: list[JsonValue] = []
    for info in sorted(model.modules, key=lambda m: m.id):
        item: dict[str, JsonValue] = {
            "id": info.id,
            "prefix": info.prefix,
            "version": info.version,
            "summary": info.summary,
            "builtin": index.builtin(info.id),
            "types": len(index.types(info.id)),
            "relations": len(index.relations(info.id)),
        }
        if functions is not None:
            item["functions"] = len(_active_in(functions, info.id))
        if actions is not None:
            item["actions"] = len(_active_in(actions, info.id))
        item["links"] = index.neighbours(info.id)
        modules.append(item)
    return model_envelope(entry, model, modules=modules)


def describe_module(
    entry: GraphEntry,
    model: CompiledModel,
    name: str,
    functions: Sequence[FunctionEntry] | None = None,
    actions: Sequence[ActionEntry] | None = None,
) -> dict[str, JsonValue]:
    """Level 1: one module (id or prefix): its types, the relations that stay inside it, the
    cross-module relations leaving (``links_out``) and arriving (``links_in``) and the rules
    whose target is one of its types. With ``functions`` or ``actions`` (see
    ``describe_modules``) also its active functions or actions."""
    index = ModuleIndex(model)
    module_id = index.resolve(name)
    info = index.infos[module_id]
    internal: list[JsonValue] = []
    for curie in index.internal(module_id):
        r = model.relations[curie]
        internal.append(
            {
                "curie": curie,
                "domain": list(r.domain),
                "range": list(r.range),
                "cardinality": {"out": _bound(r.out), "in": _bound(r.into)},
                "summary": r.doc.summary,
                "deprecated": r.doc.deprecated,
            }
        )
    body: dict[str, JsonValue] = {
        "id": info.id,
        "prefix": info.prefix,
        "version": info.version,
        "summary": info.summary,
        "builtin": index.builtin(info.id),
        "types": [_type_item(c, model.classes[c]) for c in index.types(module_id)],
        "relations": internal,
        "links_out": [_link(model, index, link, module_id) for link in index.links_out(module_id)],
        "links_in": [_link(model, index, link, module_id) for link in index.links_in(module_id)],
        "rules": [
            {
                "id": rule.id,
                "severity": rule.severity,
                "summary": rule.doc.summary,
                "deprecated": rule.doc.deprecated,
            }
            for rule in sorted(model.rules, key=lambda rule: rule.id)
            if index.owner(rule.target) == module_id
        ],
    }
    if functions is not None:
        body["functions"] = [
            {"name": f.name, "summary": f.defn.summary, "signature": f.signature}
            for f in _active_in(functions, module_id)
        ]
    if actions is not None:
        body["actions"] = [
            {"name": a.name, "summary": a.defn.summary, "signature": a.signature}
            for a in _active_in(actions, module_id)
        ]
    return model_envelope(entry, model, module=body)


def describe_functions(
    entry: GraphEntry,
    model: CompiledModel,
    functions: Sequence[FunctionEntry] | None,
    *,
    module: str | None = None,
    include_deprecated: bool = False,
) -> dict[str, JsonValue]:
    """The named functions of the graph (by name); with ``module`` (id or prefix) those the
    module owns. Deprecated ones only with ``include_deprecated``. ``None``: none visible."""
    wanted = ModuleIndex(model).filter(module)
    items: list[JsonValue] = [
        f.summary_json()
        for f in functions or ()
        if (include_deprecated or not f.deprecated) and (wanted is None or f.module == wanted)
    ]
    return _filtered(entry, model, wanted, functions=items)


def describe_function(
    entry: GraphEntry,
    model: CompiledModel,
    functions: Sequence[FunctionEntry] | None,
    name: str,
) -> dict[str, JsonValue]:
    """One function with everything its definition says, the normalized query text and its
    hash; ``function.not_found`` with ``did_you_mean`` for an unknown name."""
    found = find_entry(functions, name)
    return model_envelope(entry, model, function=found.detail_json())


def describe_actions(
    entry: GraphEntry,
    model: CompiledModel,
    actions: Sequence[ActionEntry] | None,
    *,
    module: str | None = None,
    include_deprecated: bool = False,
) -> dict[str, JsonValue]:
    """The actions of the graph (by name); with ``module`` (id or prefix) those the module
    owns. Deprecated ones only with ``include_deprecated``. ``None``: none visible."""
    wanted = ModuleIndex(model).filter(module)
    items: list[JsonValue] = [
        a.summary_json()
        for a in actions or ()
        if (include_deprecated or not a.deprecated) and (wanted is None or a.module == wanted)
    ]
    return _filtered(entry, model, wanted, actions=items)


def describe_action(
    entry: GraphEntry,
    model: CompiledModel,
    actions: Sequence[ActionEntry] | None,
    name: str,
) -> dict[str, JsonValue]:
    """One action with everything its definition says, the compiled ops, the terms it touches,
    the write rights it needs and its hash; ``action.not_found`` with ``did_you_mean`` (over
    the given actions only) for an unknown name."""
    found = find_action(actions, name)
    return model_envelope(entry, model, action=found.detail_json())


def describe_type(entry: GraphEntry, model: CompiledModel, name: str) -> dict[str, JsonValue]:
    curie, cls = _class(model, name)
    out: list[JsonValue] = []
    into: list[JsonValue] = []
    for name, r in sorted(model.relations.items()):
        if model.is_a_any(curie, r.domain) and r.domain:
            out.append(
                {
                    "curie": name,
                    "range": list(r.range),
                    "cardinality": _bound(r.out),
                    "inverse_label": r.inverse_label,
                    "qualifiers": sorted(r.qualifiers),
                    "summary": r.doc.summary,
                    "deprecated": r.doc.deprecated,
                }
            )
        if model.is_a_any(curie, r.range) and r.range:
            into.append(
                {
                    "curie": name,
                    "from": list(r.domain),
                    "cardinality": _bound(r.into),
                    "on_target_inactive": r.on_target_inactive,
                    "inverse_label": r.inverse_label,
                    "summary": r.doc.summary,
                    "deprecated": r.doc.deprecated,
                }
            )
    body: dict[str, JsonValue] = {
        "curie": curie,
        "module": ModuleIndex(model).owner(curie),
        "abstract": cls.abstract,
        "extends": cls.extends,
        "ancestors": list(cls.ancestors),
        "subtypes": [c for c in model.subtypes(curie) if c != curie],
        "alias_prefix": entry.alias_prefixes.get(curie),  # type: ignore[call-overload]
        "summary": cls.doc.summary,
        "description": cls.doc.description,
        "deprecated": cls.doc.deprecated,
        "fields": _fields(model, cls),
        "lifecycles": _lifecycles(model, cls),
        "relations_out": out,
        "relations_in": into,
        "rules": [
            {
                "id": r.id,
                "severity": r.severity,
                "when": r.when,
                "summary": r.doc.summary,
                "deprecated": r.doc.deprecated,
            }
            for r in model.rules
            if model.is_a(curie, r.target)
        ],
    }
    return model_envelope(entry, model, type=body)


def describe_relations(
    entry: GraphEntry, model: CompiledModel, *, module: str | None = None
) -> dict[str, JsonValue]:
    """The relations; with ``module`` (id or prefix) those the module declares or holds a
    class of at one end."""
    index = ModuleIndex(model)
    wanted = index.filter(module)
    relations: list[JsonValue] = [
        {
            "curie": name,
            "domain": list(r.domain),
            "range": list(r.range),
            "cardinality": {"out": _bound(r.out), "in": _bound(r.into)},
            "characteristics": list(r.characteristics),
            "on_target_inactive": r.on_target_inactive,
            "inverse_label": r.inverse_label,
            "qualifiers": sorted(r.qualifiers),
            "summary": r.doc.summary,
            "deprecated": r.doc.deprecated,
        }
        for name, r in sorted(model.relations.items())
        if wanted is None or index.touches(wanted, name)
    ]
    return _filtered(entry, model, wanted, relations=relations)


def describe_fields(entry: GraphEntry, model: CompiledModel, name: str) -> dict[str, JsonValue]:
    curie, cls = _class(model, name)
    return model_envelope(entry, model, type=curie, fields=_fields(model, cls))


def describe_lifecycle(entry: GraphEntry, model: CompiledModel, name: str) -> dict[str, JsonValue]:
    """The lifecycles of a class, or one lifecycle by its CURIE."""
    curie = resolve_term("type or lifecycle", name, {**model.classes, **model.lifecycles})
    if curie in model.lifecycles:
        return model_envelope(
            entry, model, lifecycles=[_lifecycle(curie, model.lifecycles[curie], None)]
        )
    return model_envelope(
        entry, model, type=curie, lifecycles=_lifecycles(model, model.classes[curie])
    )


def _property(name: str, base: str, many: bool) -> dict[str, JsonValue]:
    return {"name": name, "base": base, "many": many}


def describe_schema(
    entry: GraphEntry, model: CompiledModel, *, compact: bool = False, module: str | None = None
) -> dict[str, JsonValue]:
    """The model as ``graph query`` sees it: labels with their properties, relationship
    types with their ends, properties and one pattern each. Names are written as a query
    needs them: the local name when it is unique, else the CURIE in backticks.
    ``compact`` leaves out every ``properties`` list. With ``module`` (id or prefix): the
    labels of the module plus the labels at the ends of its cross-module patterns, and the
    relationships it declares or holds a class of; names are those of the whole model."""
    from graph_cli.query.cypher.resolve import EDGE_ID, cypher_names  # only this needs Cypher

    label = cypher_names(model.classes)
    rel_type = cypher_names(model.relations)
    index = ModuleIndex(model)
    wanted = index.filter(module)
    shown_relations = {r for r in model.relations if wanted is None or index.touches(wanted, r)}
    shown_labels = {c for c in model.classes if wanted is None or index.owner(c) == wanted}
    for r in shown_relations:
        if index.is_cross_module(r):
            shown_labels.update((*model.relations[r].domain, *model.relations[r].range))

    def end(curies: tuple[str, ...]) -> str:
        return "(:" + "|".join(label[c] for c in curies) + ")" if curies else "()"

    def properties(fields: Mapping[str, AttributeDef | QualifierDef]) -> dict[str, JsonValue]:
        if compact:
            return {}
        listed: list[JsonValue] = [
            _property(name, model.value_base(f.kind, f.type), f.many)
            for name, f in sorted(fields.items())
        ]
        return {"properties": listed}

    labels: list[JsonValue] = [
        {
            "label": label[curie],
            "type": curie,
            "abstract": cls.abstract,
            "supertypes": [label[c] for c in cls.ancestors],
            **properties(cls.attributes),
        }
        for curie, cls in sorted(model.classes.items())
        if curie in shown_labels
    ]
    relationships: list[JsonValue] = [
        {
            "type": rel_type[curie],
            "relation": curie,
            "pattern": f"{end(r.domain)}-[:{rel_type[curie]}]->{end(r.range)}",
            **properties(r.qualifiers),
        }
        for curie, r in sorted(model.relations.items())
        if curie in shown_relations
    ]
    return _filtered(
        entry,
        model,
        wanted,
        labels=labels,
        relationships=relationships,
        node_fields=sorted(SYSTEM_FIELDS),
        relationship_fields=[EDGE_ID],
    )


# The keys of a catalog entry a graph skill reads: names, summaries and signatures are
# schema-level text; query text, ``touches``, ``fields`` and ``hash`` need ``read.function``.
SKILL_FUNCTION_KEYS: Final = ("name", "module", "summary", "status", "featured", "params")


def skill_functions(catalog: JsonObject) -> JsonObject:
    """The compiled catalog reduced to the keys of ``SKILL_FUNCTION_KEYS`` per entry."""
    entries = catalog.get("functions")
    kept: list[JsonValue] = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                kept.append({k: entry[k] for k in SKILL_FUNCTION_KEYS if k in entry})
    return {"schema": catalog.get("schema"), "functions": kept}


def describe_model(
    entry: GraphEntry, model: CompiledModel, functions: JsonObject | None = None
) -> dict[str, JsonValue]:
    """The model documents a graph skill is rendered from (``graph_cli.skills``): what
    ``describe types``, ``describe type <T>`` for every class, ``describe relations``,
    ``describe schema``, ``describe modules`` and ``describe module <M>`` for every
    non-built-in module print, as ``{types, type: {curie: document}, relations, schema,
    modules: [item], module: {id: document}}``. ``functions`` is the compiled catalog of
    the graph (``graph_cli.functions/1``); given, it joins as the key ``functions``, reduced
    to what a skill prints (``skill_functions``): the documents are readable with
    ``read.describe`` alone, so they carry no query text (K10)."""
    index = ModuleIndex(model)
    modules = describe_modules(entry, model)["modules"]
    documents: dict[str, JsonValue] = {
        "types": describe_types(entry, model),
        "type": {curie: describe_type(entry, model, curie) for curie in sorted(model.classes)},
        "relations": describe_relations(entry, model),
        "schema": describe_schema(entry, model),
        "modules": modules,
        "module": {
            info.id: describe_module(entry, model, info.id)
            for info in sorted(model.modules, key=lambda m: m.id)
            if not index.builtin(info.id)
        },
    }
    if functions is not None:
        documents["functions"] = skill_functions(functions)
    return documents
