"""Model description ``graph_cli.model/1`` of a graph's compiled ontology (contracts B11.5).

Projections for agents: types, one type with its fields, lifecycles and
relations in both directions, relations, fields, lifecycles, and the schema
as ``graph query`` (Cypher) sees it. Every term
with a summary also carries its ``deprecated`` flag. Deterministic
(sorted), no timestamps. A type or lifecycle is its CURIE or its unique
local name, as for ``find`` (``query.plan.resolve_term``); unknown terms
are ``term.unknown`` with ``did_you_mean``. Functions of a graph entry and its compiled model, so
the app (a read view) and graph-admin (the pinned model) describe a graph
the same way; ``describe_model`` bundles every document a graph skill is
rendered from.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.ontology_layer.model import (
    AttributeDef,
    Bound,
    ClassDef,
    CompiledModel,
    LifecycleDef,
    QualifierDef,
)
from graph_cli.ontology_layer.vocabulary import SYSTEM_FIELDS
from graph_cli.query.plan import resolve_term

MODEL_SCHEMA: Final = "graph_cli.model/1"


def _bound(bound: Bound) -> str:
    return f"{bound.min}..{'n' if bound.max is None else bound.max}"


def _envelope(entry: GraphEntry, model: CompiledModel, **body: JsonValue) -> dict[str, JsonValue]:
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


def describe_types(entry: GraphEntry, model: CompiledModel) -> dict[str, JsonValue]:
    types: list[JsonValue] = [
        {
            "curie": c,
            "abstract": d.abstract,
            "extends": d.extends,
            "summary": d.doc.summary,
            "deprecated": d.doc.deprecated,
        }
        for c, d in sorted(model.classes.items())
    ]
    return _envelope(entry, model, types=types)


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
    return _envelope(entry, model, type=body)


def describe_relations(entry: GraphEntry, model: CompiledModel) -> dict[str, JsonValue]:
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
    ]
    return _envelope(entry, model, relations=relations)


def describe_fields(entry: GraphEntry, model: CompiledModel, name: str) -> dict[str, JsonValue]:
    curie, cls = _class(model, name)
    return _envelope(entry, model, type=curie, fields=_fields(model, cls))


def describe_lifecycle(entry: GraphEntry, model: CompiledModel, name: str) -> dict[str, JsonValue]:
    """The lifecycles of a class, or one lifecycle by its CURIE."""
    curie = resolve_term("type or lifecycle", name, {**model.classes, **model.lifecycles})
    if curie in model.lifecycles:
        return _envelope(
            entry, model, lifecycles=[_lifecycle(curie, model.lifecycles[curie], None)]
        )
    return _envelope(entry, model, type=curie, lifecycles=_lifecycles(model, model.classes[curie]))


def _property(name: str, base: str, many: bool) -> dict[str, JsonValue]:
    return {"name": name, "base": base, "many": many}


def describe_schema(
    entry: GraphEntry, model: CompiledModel, *, compact: bool = False
) -> dict[str, JsonValue]:
    """The model as ``graph query`` sees it: labels with their properties, relationship
    types with their ends, properties and one pattern each. Names are written as a query
    needs them: the local name when it is unique, else the CURIE in backticks.
    ``compact`` leaves out every ``properties`` list."""
    from graph_cli.query.cypher.resolve import EDGE_ID, cypher_names  # only this needs Cypher

    label = cypher_names(model.classes)
    rel_type = cypher_names(model.relations)

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
    ]
    relationships: list[JsonValue] = [
        {
            "type": rel_type[curie],
            "relation": curie,
            "pattern": f"{end(r.domain)}-[:{rel_type[curie]}]->{end(r.range)}",
            **properties(r.qualifiers),
        }
        for curie, r in sorted(model.relations.items())
    ]
    return _envelope(
        entry,
        model,
        labels=labels,
        relationships=relationships,
        node_fields=sorted(SYSTEM_FIELDS),
        relationship_fields=[EDGE_ID],
    )


def describe_model(entry: GraphEntry, model: CompiledModel) -> dict[str, JsonValue]:
    """The model documents a graph skill is rendered from (``graph_cli.skills``): what
    ``describe types``, ``describe type <T>`` for every class, ``describe relations``
    and ``describe schema`` print, as ``{types, type: {curie: document}, relations,
    schema}``."""
    return {
        "types": describe_types(entry, model),
        "type": {curie: describe_type(entry, model, curie) for curie in sorted(model.classes)},
        "relations": describe_relations(entry, model),
        "schema": describe_schema(entry, model),
    }
