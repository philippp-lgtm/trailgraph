"""Compiled ontology ``graph_cli.compiled_ontology/1``: typed view and JSON form (B7.5-B7.7).

The runtime reads only this form. Every documented key is always present;
``null`` is meaningful where it appears (``extends``, ``inverse_label``,
``max`` = unbounded, ``default`` = none, ``description``, ``renamed_from``,
``display_prefix``). ``artifact_hash`` is the C.2 hash of the JSON document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from graph_cli.kernel.canonical import CanonicalError, JsonObject, JsonValue, hash_value, parse_json
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import (
    JsonShapeError,
    as_object,
    opt_int,
    opt_str,
    req,
    req_bool,
    req_int,
    req_list,
    req_obj,
    req_str,
    str_list,
)
from graph_cli.ontology_layer.vocabulary import COMPILED_SCHEMA

AttributeKind = Literal["base", "datatype", "enum", "lifecycle"]


@dataclass(frozen=True, slots=True)
class TermDoc:
    summary: str
    description: str | None
    deprecated: bool
    annotations: Mapping[str, JsonValue]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "summary": self.summary,
            "description": self.description,
            "deprecated": self.deprecated,
            "annotations": dict(self.annotations),
        }

    @classmethod
    def from_json(cls, data: JsonObject, path: str) -> TermDoc:
        return cls(
            summary=req_str(data, "summary", path),
            description=opt_str(data, "description", path),
            deprecated=req_bool(data, "deprecated", path),
            annotations=req_obj(data, "annotations", path),
        )


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    id: str
    prefix: str
    namespace: str
    version: str
    summary: str
    extensions: Mapping[str, JsonValue]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "prefix": self.prefix,
            "namespace": self.namespace,
            "version": self.version,
            "summary": self.summary,
            "extensions": dict(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class DatatypeDef:
    base: str
    facets: Mapping[str, JsonValue]
    doc: TermDoc


@dataclass(frozen=True, slots=True)
class NamedValue:
    """An enum value or lifecycle state."""

    name: str
    summary: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class EnumDef:
    values: tuple[NamedValue, ...]
    doc: TermDoc

    def value_names(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.values)


@dataclass(frozen=True, slots=True)
class GuardDef:
    id: str
    summary: str
    expr: JsonValue


@dataclass(frozen=True, slots=True)
class TransitionDef:
    from_state: str
    to_state: str
    summary: str | None
    guards: tuple[GuardDef, ...]


@dataclass(frozen=True, slots=True)
class LifecycleDef:
    states: tuple[NamedValue, ...]
    initial: tuple[str, ...]
    terminal: tuple[str, ...]
    transitions: tuple[TransitionDef, ...]
    doc: TermDoc

    def state_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.states)


@dataclass(frozen=True, slots=True)
class AttributeDef:
    type: str  # base type name or CURIE
    kind: AttributeKind
    required: bool
    many: bool
    unique: bool
    default: JsonValue  # None = no default
    mutability: str
    declared_in: str
    renamed_from: str | None
    doc: TermDoc


@dataclass(frozen=True, slots=True)
class ClassDef:
    abstract: bool
    extends: str | None
    ancestors: tuple[str, ...]  # nearest first
    attributes: Mapping[str, AttributeDef]  # effective, including inherited
    display_prefix: str | None
    renamed_from: str | None
    mappings: Mapping[str, tuple[str, ...]]
    doc: TermDoc


@dataclass(frozen=True, slots=True)
class Bound:
    min: int
    max: int | None


@dataclass(frozen=True, slots=True)
class QualifierDef:
    type: str
    kind: AttributeKind
    required: bool
    many: bool
    summary: str


@dataclass(frozen=True, slots=True)
class RelationDef:
    domain: tuple[str, ...]  # () = any class
    range: tuple[str, ...]
    out: Bound
    into: Bound
    characteristics: tuple[str, ...]
    inverse_label: str | None
    qualifiers: Mapping[str, QualifierDef]
    renamed_from: str | None
    mappings: Mapping[str, tuple[str, ...]]
    doc: TermDoc


@dataclass(frozen=True, slots=True)
class RuleDef:
    id: str
    severity: str
    when: str
    target: str
    expr: JsonValue
    doc: TermDoc


@dataclass(frozen=True, slots=True)
class CompiledModel:
    bundle_id: str
    bundle_version: str
    modules: tuple[ModuleInfo, ...]
    prefixes: Mapping[str, str]
    datatypes: Mapping[str, DatatypeDef]
    enums: Mapping[str, EnumDef]
    lifecycles: Mapping[str, LifecycleDef]
    classes: Mapping[str, ClassDef]
    relations: Mapping[str, RelationDef]
    rules: tuple[RuleDef, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema": COMPILED_SCHEMA,
            "bundle": {"id": self.bundle_id, "version": self.bundle_version},
            "modules": [m.to_json() for m in self.modules],
            "prefixes": dict(self.prefixes),
            "datatypes": {k: _datatype_json(v) for k, v in self.datatypes.items()},
            "enums": {k: _enum_json(v) for k, v in self.enums.items()},
            "lifecycles": {k: _lifecycle_json(v) for k, v in self.lifecycles.items()},
            "classes": {k: _class_json(v) for k, v in self.classes.items()},
            "relations": {k: _relation_json(v) for k, v in self.relations.items()},
            "rules": [_rule_json(r) for r in self.rules],
        }

    @property
    def artifact_hash(self) -> str:
        """B7.5: C.2 hash of the compiled document."""
        return hash_value(self.to_json())

    def term_curies(self) -> frozenset[str]:
        return frozenset(
            (*self.datatypes, *self.enums, *self.lifecycles, *self.classes, *self.relations)
        )


def _mappings_json(mappings: Mapping[str, tuple[str, ...]]) -> dict[str, JsonValue]:
    return {kind: list(values) for kind, values in mappings.items()}


def _datatype_json(d: DatatypeDef) -> dict[str, JsonValue]:
    return {"base": d.base, "facets": dict(d.facets), **d.doc.to_json()}


def _enum_json(e: EnumDef) -> dict[str, JsonValue]:
    return {"values": [v.to_json() for v in e.values], **e.doc.to_json()}


def _lifecycle_json(lc: LifecycleDef) -> dict[str, JsonValue]:
    return {
        "states": [s.to_json() for s in lc.states],
        "initial": list(lc.initial),
        "terminal": list(lc.terminal),
        "transitions": [
            {
                "from": t.from_state,
                "to": t.to_state,
                "summary": t.summary,
                "guards": [{"id": g.id, "summary": g.summary, "expr": g.expr} for g in t.guards],
            }
            for t in lc.transitions
        ],
        **lc.doc.to_json(),
    }


def _attribute_json(a: AttributeDef) -> dict[str, JsonValue]:
    return {
        "type": a.type,
        "kind": a.kind,
        "required": a.required,
        "many": a.many,
        "unique": a.unique,
        "default": a.default,
        "mutability": a.mutability,
        "declared_in": a.declared_in,
        "renamed_from": a.renamed_from,
        **a.doc.to_json(),
    }


def _class_json(c: ClassDef) -> dict[str, JsonValue]:
    return {
        "abstract": c.abstract,
        "extends": c.extends,
        "ancestors": list(c.ancestors),
        "attributes": {k: _attribute_json(v) for k, v in c.attributes.items()},
        "display_prefix": c.display_prefix,
        "renamed_from": c.renamed_from,
        "mappings": _mappings_json(c.mappings),
        **c.doc.to_json(),
    }


def _relation_json(r: RelationDef) -> dict[str, JsonValue]:
    return {
        "domain": list(r.domain),
        "range": list(r.range),
        "cardinality": {
            "out": {"min": r.out.min, "max": r.out.max},
            "in": {"min": r.into.min, "max": r.into.max},
        },
        "characteristics": list(r.characteristics),
        "inverse_label": r.inverse_label,
        "qualifiers": {
            k: {
                "type": q.type,
                "kind": q.kind,
                "required": q.required,
                "many": q.many,
                "summary": q.summary,
            }
            for k, q in r.qualifiers.items()
        },
        "renamed_from": r.renamed_from,
        "mappings": _mappings_json(r.mappings),
        **r.doc.to_json(),
    }


def _rule_json(r: RuleDef) -> dict[str, JsonValue]:
    return {
        "id": r.id,
        "severity": r.severity,
        "when": r.when,
        "target": r.target,
        "expr": r.expr,
        **r.doc.to_json(),
    }


# ---- reading -------------------------------------------------------------------------------


def _objects(data: JsonObject, key: str) -> dict[str, JsonObject]:
    section = req_obj(data, key)
    return {name: as_object(value, f"{key}.{name}") for name, value in section.items()}


_KINDS: dict[str, AttributeKind] = {
    "base": "base",
    "datatype": "datatype",
    "enum": "enum",
    "lifecycle": "lifecycle",
}


def _kind(value: str, path: str) -> AttributeKind:
    try:
        return _KINDS[value]
    except KeyError:
        raise JsonShapeError(f"{path}: unknown kind {value!r}") from None


def _named_values(data: JsonObject, key: str, path: str) -> tuple[NamedValue, ...]:
    result = []
    for i, item in enumerate(req_list(data, key, path)):
        entry = as_object(item, f"{path}{key}[{i}]")
        result.append(NamedValue(req_str(entry, "name"), opt_str(entry, "summary")))
    return tuple(result)


def _mappings(data: JsonObject, path: str) -> dict[str, tuple[str, ...]]:
    section = req_obj(data, "mappings", path)
    return {kind: str_list(section, kind, f"{path}mappings.") for kind in section}


def _bound(data: JsonObject, path: str) -> Bound:
    return Bound(req_int(data, "min", path), opt_int(data, "max", path))


def model_from_json(data: JsonObject) -> CompiledModel:
    """Typed view of a compiled document. Shape errors raise ``JsonShapeError``."""
    schema = req_str(data, "schema")
    if schema != COMPILED_SCHEMA:
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported compiled ontology schema {schema!r}",
            details={"expected": COMPILED_SCHEMA, "actual": schema},
        )
    bundle = req_obj(data, "bundle")
    modules = tuple(
        ModuleInfo(
            id=req_str(m, "id"),
            prefix=req_str(m, "prefix"),
            namespace=req_str(m, "namespace"),
            version=req_str(m, "version"),
            summary=req_str(m, "summary"),
            extensions=req_obj(m, "extensions"),
        )
        for m in (as_object(item, "modules[]") for item in req_list(data, "modules"))
    )
    datatypes = {
        k: DatatypeDef(req_str(v, "base"), req_obj(v, "facets"), TermDoc.from_json(v, f"{k}."))
        for k, v in _objects(data, "datatypes").items()
    }
    enums = {
        k: EnumDef(_named_values(v, "values", f"{k}."), TermDoc.from_json(v, f"{k}."))
        for k, v in _objects(data, "enums").items()
    }
    lifecycles = {}
    for k, v in _objects(data, "lifecycles").items():
        transitions = []
        for item in req_list(v, "transitions", f"{k}."):
            t = as_object(item, f"{k}.transitions[]")
            guards = tuple(
                GuardDef(req_str(g, "id"), req_str(g, "summary"), req(g, "expr"))
                for g in (as_object(x, f"{k}.guards[]") for x in req_list(t, "guards"))
            )
            transitions.append(
                TransitionDef(req_str(t, "from"), req_str(t, "to"), opt_str(t, "summary"), guards)
            )
        lifecycles[k] = LifecycleDef(
            states=_named_values(v, "states", f"{k}."),
            initial=str_list(v, "initial", f"{k}."),
            terminal=str_list(v, "terminal", f"{k}."),
            transitions=tuple(transitions),
            doc=TermDoc.from_json(v, f"{k}."),
        )
    classes = {}
    for k, v in _objects(data, "classes").items():
        attributes = {}
        for name, a in _objects(v, "attributes").items():
            path = f"{k}.attributes.{name}."
            attributes[name] = AttributeDef(
                type=req_str(a, "type", path),
                kind=_kind(req_str(a, "kind", path), path),
                required=req_bool(a, "required", path),
                many=req_bool(a, "many", path),
                unique=req_bool(a, "unique", path),
                default=req(a, "default", path),
                mutability=req_str(a, "mutability", path),
                declared_in=req_str(a, "declared_in", path),
                renamed_from=opt_str(a, "renamed_from", path),
                doc=TermDoc.from_json(a, path),
            )
        classes[k] = ClassDef(
            abstract=req_bool(v, "abstract", f"{k}."),
            extends=opt_str(v, "extends", f"{k}."),
            ancestors=str_list(v, "ancestors", f"{k}."),
            attributes=attributes,
            display_prefix=opt_str(v, "display_prefix", f"{k}."),
            renamed_from=opt_str(v, "renamed_from", f"{k}."),
            mappings=_mappings(v, f"{k}."),
            doc=TermDoc.from_json(v, f"{k}."),
        )
    relations = {}
    for k, v in _objects(data, "relations").items():
        cardinality = req_obj(v, "cardinality", f"{k}.")
        qualifiers = {
            name: QualifierDef(
                type=req_str(q, "type"),
                kind=_kind(req_str(q, "kind"), f"{k}.qualifiers.{name}"),
                required=req_bool(q, "required"),
                many=req_bool(q, "many"),
                summary=req_str(q, "summary"),
            )
            for name, q in _objects(v, "qualifiers").items()
        }
        relations[k] = RelationDef(
            domain=str_list(v, "domain", f"{k}."),
            range=str_list(v, "range", f"{k}."),
            out=_bound(req_obj(cardinality, "out"), f"{k}.cardinality.out."),
            into=_bound(req_obj(cardinality, "in"), f"{k}.cardinality.in."),
            characteristics=str_list(v, "characteristics", f"{k}."),
            inverse_label=opt_str(v, "inverse_label", f"{k}."),
            qualifiers=qualifiers,
            renamed_from=opt_str(v, "renamed_from", f"{k}."),
            mappings=_mappings(v, f"{k}."),
            doc=TermDoc.from_json(v, f"{k}."),
        )
    rules = tuple(
        RuleDef(
            id=req_str(r, "id"),
            severity=req_str(r, "severity"),
            when=req_str(r, "when"),
            target=req_str(r, "target"),
            expr=req(r, "expr"),
            doc=TermDoc.from_json(r, "rules[]."),
        )
        for r in (as_object(item, "rules[]") for item in req_list(data, "rules"))
    )
    prefixes = req_obj(data, "prefixes")
    return CompiledModel(
        bundle_id=req_str(bundle, "id", "bundle."),
        bundle_version=req_str(bundle, "version", "bundle."),
        modules=modules,
        prefixes={k: str(v) for k, v in prefixes.items()},
        datatypes=datatypes,
        enums=enums,
        lifecycles=lifecycles,
        classes=classes,
        relations=relations,
        rules=rules,
    )


def load_compiled(data: bytes, *, expected_hash: str) -> CompiledModel:
    """Load ``compiled.json`` and verify it against the pinned artifact hash (B7.6)."""
    try:
        document = as_object(parse_json(data), "")
    except (CanonicalError, JsonShapeError) as exc:
        raise GraphCliError(
            "ontology.pin_mismatch", f"compiled ontology is unreadable: {exc}"
        ) from exc
    actual = hash_value(document)
    if actual != expected_hash:
        raise GraphCliError(
            "ontology.pin_mismatch",
            "compiled ontology does not match the pinned artifact hash",
            details={"expected": expected_hash, "actual": actual},
        )
    try:
        return model_from_json(document)
    except JsonShapeError as exc:
        raise GraphCliError("internal.error", f"compiled ontology has a bad shape: {exc}") from exc
