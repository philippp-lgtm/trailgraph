"""Model description ``graph_cli.model/1`` of a graph's compiled ontology (contracts B11.5).

Projections for agents: types, one type with its fields, lifecycles and
relations in both directions, relations, fields, lifecycles. Deterministic
(sorted), no timestamps; unknown terms are ``term.unknown`` with
``did_you_mean``.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.model import Bound, ClassDef, CompiledModel, LifecycleDef
from graph_cli.query.plan import unknown_term
from graph_cli.services.read import GraphRead

MODEL_SCHEMA: Final = "graph_cli.model/1"


def _bound(bound: Bound) -> str:
    return f"{bound.min}..{'n' if bound.max is None else bound.max}"


def _envelope(view: GraphRead, **body: JsonValue) -> dict[str, JsonValue]:
    return {
        "schema": MODEL_SCHEMA,
        "graph": {
            "id": str(view.entry.id),
            "name": view.entry.name,
            "language": view.entry.language,
        },
        "ontology": {"bundle": view.model.bundle_id, "version": view.model.bundle_version},
        **body,
    }


def _class(model: CompiledModel, curie: str) -> ClassDef:
    found = model.classes.get(curie)
    if found is None:
        raise unknown_term("type", curie, model.classes)
    return found


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
    }


def _lifecycles(model: CompiledModel, cls: ClassDef) -> list[JsonValue]:
    return [
        _lifecycle(a.type, model.lifecycles[a.type], name)
        for name, a in sorted(cls.attributes.items())
        if a.kind == "lifecycle"
    ]


def describe_types(view: GraphRead) -> dict[str, JsonValue]:
    model = view.model
    types: list[JsonValue] = [
        {"curie": c, "abstract": d.abstract, "extends": d.extends, "summary": d.doc.summary}
        for c, d in sorted(model.classes.items())
    ]
    return _envelope(view, types=types)


def describe_type(view: GraphRead, curie: str) -> dict[str, JsonValue]:
    model = view.model
    cls = _class(model, curie)
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
                }
            )
        if model.is_a_any(curie, r.range) and r.range:
            into.append(
                {
                    "curie": name,
                    "from": list(r.domain),
                    "cardinality": _bound(r.into),
                    "inverse_label": r.inverse_label,
                    "summary": r.doc.summary,
                }
            )
    body: dict[str, JsonValue] = {
        "curie": curie,
        "abstract": cls.abstract,
        "extends": cls.extends,
        "ancestors": list(cls.ancestors),
        "subtypes": [c for c in model.subtypes(curie) if c != curie],
        "alias_prefix": view.entry.alias_prefixes.get(curie),  # type: ignore[call-overload]
        "summary": cls.doc.summary,
        "description": cls.doc.description,
        "fields": _fields(model, cls),
        "lifecycles": _lifecycles(model, cls),
        "relations_out": out,
        "relations_in": into,
        "rules": [
            {"id": r.id, "severity": r.severity, "when": r.when, "summary": r.doc.summary}
            for r in model.rules
            if model.is_a(curie, r.target)
        ],
    }
    return _envelope(view, type=body)


def describe_relations(view: GraphRead) -> dict[str, JsonValue]:
    model = view.model
    relations: list[JsonValue] = [
        {
            "curie": name,
            "domain": list(r.domain),
            "range": list(r.range),
            "cardinality": {"out": _bound(r.out), "in": _bound(r.into)},
            "characteristics": list(r.characteristics),
            "inverse_label": r.inverse_label,
            "qualifiers": sorted(r.qualifiers),
            "summary": r.doc.summary,
        }
        for name, r in sorted(model.relations.items())
    ]
    return _envelope(view, relations=relations)


def describe_fields(view: GraphRead, curie: str) -> dict[str, JsonValue]:
    cls = _class(view.model, curie)
    return _envelope(view, type=curie, fields=_fields(view.model, cls))


def describe_lifecycle(view: GraphRead, curie: str) -> dict[str, JsonValue]:
    """The lifecycles of a class, or one lifecycle by its CURIE."""
    model = view.model
    if curie in model.lifecycles:
        return _envelope(view, lifecycles=[_lifecycle(curie, model.lifecycles[curie], None)])
    if curie not in model.classes:
        raise unknown_term("type or lifecycle", curie, (*model.classes, *model.lifecycles))
    return _envelope(view, type=curie, lifecycles=_lifecycles(model, model.classes[curie]))
