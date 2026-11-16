"""Compiled ontology ``graph_cli.compiled_ontology/1`` and ``/2``: typed view and JSON form
(B7.5-B7.7).

The runtime reads only this form. Every documented key is always present;
``null`` is meaningful where it appears (``extends``, ``inverse_label``,
``max`` = unbounded, ``default`` = none, ``description``, ``renamed_from``,
``display_prefix``, ``role`` = no label role). A document without ``role``
(written before label roles) reads as ``null``. ``artifact_hash`` is the C.2
hash of the JSON document. A relation's ``on_target_inactive`` is written
only when it is not the default ``allow`` and reads as ``allow`` when absent.
An expression node kind, a relation key or an ``on_target_inactive`` value
this program does not know (a model released for a newer ``graph``) is
refused with ``format.unsupported_version``.

A model that declares markings is written as ``/2``: the top-level ``requires`` lists the
features the document uses (``["markings"]``), ``markings`` holds the declarations and an
attribute with markings carries ``markings`` (sorted CURIEs). Every program before ``/2``
refuses it by its schema check, so no older program shows marked attributes. A model without
markings stays ``/1`` with the same bytes and artifact hash. The reader refuses a feature it
does not know (details ``feature``), a ``/2`` document that uses no feature, ``markings`` in a
``/1`` document and any attribute key it does not know (``format.unsupported_version``): the
attribute keys are an allow list, so a later security-bearing key is never ignored. No key was
ever removed from the compiled attribute form, so the list reads every installed model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

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
from graph_cli.ontology_layer.expr_ast import unknown_nodes
from graph_cli.ontology_layer.vocabulary import (
    COMPILED_FEATURES,
    COMPILED_SCHEMA,
    COMPILED_SCHEMA_V2,
    LABEL_ROLES,
    MARKINGS_FEATURE,
    TARGET_INACTIVE_POLICIES,
)

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
    role: str | None  # one of LABEL_ROLES
    declared_in: str
    renamed_from: str | None
    doc: TermDoc
    # Marking CURIEs, sorted; a caller must hold every one to see the attribute. ``()`` =
    # unmarked (the attribute key is then not written, so ``/1`` documents keep their bytes).
    markings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MarkingDef:
    """A declared marking (``markings.<curie>``)."""

    summary: str
    description: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {"summary": self.summary, "description": self.description}


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
    on_target_inactive: str  # one of TARGET_INACTIVE_POLICIES
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
    markings: Mapping[str, MarkingDef] = field(default_factory=dict)

    def features(self) -> tuple[str, ...]:
        """The features of ``/2`` this model uses, sorted (``()``: it is written as ``/1``)."""
        marked = bool(self.markings) or any(
            a.markings for c in self.classes.values() for a in c.attributes.values()
        )
        return (MARKINGS_FEATURE,) if marked else ()

    def used_markings(self) -> frozenset[str]:
        """The markings some attribute carries (a declared marking no attribute names hides
        nothing)."""
        return frozenset(
            m for c in self.classes.values() for a in c.attributes.values() for m in a.markings
        )

    def to_json(self) -> dict[str, JsonValue]:
        features = self.features()
        extra: dict[str, JsonValue] = {}
        if features:
            extra = {
                "requires": list(features),
                "markings": {k: v.to_json() for k, v in self.markings.items()},
            }
        return {
            "schema": COMPILED_SCHEMA_V2 if features else COMPILED_SCHEMA,
            **extra,
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
            (
                *self.datatypes,
                *self.enums,
                *self.lifecycles,
                *self.classes,
                *self.relations,
                *self.markings,
            )
        )

    def is_a(self, class_curie: str, ancestor: str) -> bool:
        """``class_curie`` is ``ancestor`` or one of its subclasses."""
        if class_curie == ancestor:
            return True
        found = self.classes.get(class_curie)
        return found is not None and ancestor in found.ancestors

    def is_a_any(self, class_curie: str, ancestors: tuple[str, ...]) -> bool:
        """``ancestors == ()`` means any class (relation domain and range)."""
        return not ancestors or any(self.is_a(class_curie, a) for a in ancestors)

    def subtypes(self, class_curie: str) -> tuple[str, ...]:
        """The class and every subclass, sorted."""
        return tuple(sorted(c for c in self.classes if self.is_a(c, class_curie)))

    def lifecycle_attribute(self, class_curie: str) -> str | None:
        """The one lifecycle attribute of the class (what a ``transition`` without ``attr``
        moves); ``None`` for an unknown class, none or several."""
        found = self.classes.get(class_curie)
        names = (
            []
            if found is None
            else [n for n, a in found.attributes.items() if a.kind == "lifecycle"]
        )
        return names[0] if len(names) == 1 else None

    def value_base(self, kind: AttributeKind, type_ref: str) -> str:
        """The base type of a value: enum values and lifecycle states are strings."""
        if kind == "datatype":
            return self.datatypes[type_ref].base
        return type_ref if kind == "base" else "string"

    def facets(self, kind: AttributeKind, type_ref: str) -> Mapping[str, JsonValue]:
        return self.datatypes[type_ref].facets if kind == "datatype" else {}

    def label_attributes(self, class_curie: str) -> tuple[tuple[str, str], ...]:
        """``(attribute, role)`` of a class's effective label-role attributes.

        Ordered main label, alternative labels, hidden labels, then by name.
        """
        attributes = self.classes[class_curie].attributes
        return tuple(
            sorted(
                ((name, a.role) for name, a in attributes.items() if a.role is not None),
                key=lambda pair: (LABEL_ROLES.index(pair[1]), pair[0]),
            )
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
        "role": a.role,
        "declared_in": a.declared_in,
        "renamed_from": a.renamed_from,
        **({"markings": list(a.markings)} if a.markings else {}),
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
    # The default policy is left out, so models that never name one keep their bytes and hash.
    policy: dict[str, JsonValue] = (
        {}
        if r.on_target_inactive == TARGET_INACTIVE_POLICIES[0]
        else {"on_target_inactive": r.on_target_inactive}
    )
    return {
        "domain": list(r.domain),
        "range": list(r.range),
        "cardinality": {
            "out": {"min": r.out.min, "max": r.out.max},
            "in": {"min": r.into.min, "max": r.into.max},
        },
        "characteristics": list(r.characteristics),
        **policy,
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


def _role(value: str | None, path: str) -> str | None:
    if value is not None and value not in LABEL_ROLES:
        raise JsonShapeError(f"{path}role: unknown label role {value!r}")
    return value


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


_RELATION_KEYS = frozenset(
    {
        "domain",
        "range",
        "cardinality",
        "characteristics",
        "on_target_inactive",
        "inverse_label",
        "qualifiers",
        "renamed_from",
        "mappings",
        "summary",
        "description",
        "deprecated",
        "annotations",
    }
)


# Every key the compiled attribute form ever had (git history: ``role`` came with label roles
# and may be absent); ``markings`` only in ``/2``. A key outside the list is refused.
_ATTRIBUTE_KEYS: Final = frozenset(
    {
        "type",
        "kind",
        "required",
        "many",
        "unique",
        "default",
        "mutability",
        "role",
        "declared_in",
        "renamed_from",
        "summary",
        "description",
        "deprecated",
        "annotations",
    }
)
_ATTRIBUTE_KEYS_V2: Final = _ATTRIBUTE_KEYS | {"markings"}


def _features(data: JsonObject, schema: str) -> bool:
    """Whether the document uses markings. ``/1`` carries no ``requires`` and no
    ``markings``; ``/2`` names in ``requires`` every feature it uses, only features this
    program knows, and uses each."""
    if schema == COMPILED_SCHEMA:
        for key in ("requires", "markings"):
            if key in data:
                raise _newer("the compiled ontology", f"the key {key!r}")
        return False
    features = str_list(data, "requires")
    for feature in features:
        if feature not in COMPILED_FEATURES:
            raise GraphCliError(
                "format.unsupported_version",
                f"the compiled ontology requires the feature {feature!r}, which this graph"
                " program does not know; the ontology was released for a newer graph, update"
                " graph",
                details={"feature": feature},
            )
    if list(features) != sorted(set(features)) or MARKINGS_FEATURE not in features:
        raise JsonShapeError(f"requires: expected {[MARKINGS_FEATURE]}, got {list(features)}")
    if not req_obj(data, "markings"):
        raise JsonShapeError("markings: a document that requires markings declares some")
    return True


def _markings(data: JsonObject) -> dict[str, MarkingDef]:
    return {
        k: MarkingDef(req_str(v, "summary", f"markings.{k}."), opt_str(v, "description"))
        for k, v in _objects(data, "markings").items()
    }


def _attribute_markings(
    data: JsonObject, path: str, declared: Mapping[str, MarkingDef]
) -> tuple[str, ...]:
    if "markings" not in data:
        return ()
    found = str_list(data, "markings", path)
    if not found or list(found) != sorted(set(found)):
        raise JsonShapeError(f"{path}markings: expected sorted, distinct markings")
    undeclared = [m for m in found if m not in declared]
    if undeclared:
        raise JsonShapeError(f"{path}markings: {undeclared[0]!r} is not declared")
    return found


def _newer(owner: str, what: str) -> GraphCliError:
    return GraphCliError(
        "format.unsupported_version",
        f"{owner} uses {what}, which this graph program does not know; the ontology was "
        "released for a newer graph, update graph",
        details={"owner": owner, "unknown": what},
    )


def _target_policy(data: JsonObject, relation: str) -> str:
    """``on_target_inactive``; absent is the default. Unknown keys and values are refused."""
    unknown = sorted(data.keys() - _RELATION_KEYS)
    if unknown:
        raise _newer(f"relation {relation}", f"the key {unknown[0]!r}")
    policy = opt_str(data, "on_target_inactive", f"{relation}.")
    if policy is None:
        return TARGET_INACTIVE_POLICIES[0]
    if policy not in TARGET_INACTIVE_POLICIES:
        raise _newer(f"relation {relation}", f"on_target_inactive {policy!r}")
    return policy


def _expr(data: JsonObject, owner: str) -> JsonValue:
    expr = req(data, "expr")
    unknown = unknown_nodes(expr)
    if unknown:
        raise GraphCliError(
            "format.unsupported_version",
            f"{owner} uses the expression node {unknown[0]!r}, which this graph program does "
            "not know; the ontology was released for a newer graph, update graph",
            details={"node": unknown[0], "owner": owner},
        )
    return expr


# What the raw bytes of a document with markings contain: the schema string or a key, spelled
# plainly, or a JSON escape that could spell one (a canonical document escapes neither ``/``
# nor printable characters, so its bytes hold none unless they hold a control character).
_TRACE_WORDS: Final = (
    COMPILED_SCHEMA_V2.encode(),
    b'"requires"',
    b'"markings"',
    b"\\u",
    b"\\/",
)


def traces_markings(raw: bytes) -> bool:
    """Whether the raw bytes of a compiled document show markings: the ``/2`` schema or a
    ``requires`` / ``markings`` key. Bytes that are no JSON object are searched for the schema
    string. Markings need a program that writes ``/2``, so a document without any of these
    traces holds none. Bytes that spell none of them plainly and hold no escape that could
    (``_TRACE_WORDS``) are answered without parsing them."""
    if not any(word in raw for word in _TRACE_WORDS):
        return False
    try:
        document = parse_json(raw)
    except CanonicalError:
        return COMPILED_SCHEMA_V2.encode() in raw
    if not isinstance(document, Mapping):
        return False
    return (
        document.get("schema") == COMPILED_SCHEMA_V2
        or "requires" in document
        or "markings" in document
    )


def model_from_json(data: JsonObject) -> CompiledModel:
    """Typed view of a compiled document. Shape errors raise ``JsonShapeError``."""
    schema = req_str(data, "schema")
    if schema not in (COMPILED_SCHEMA, COMPILED_SCHEMA_V2):
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported compiled ontology schema {schema!r}",
            details={"expected": [COMPILED_SCHEMA, COMPILED_SCHEMA_V2], "actual": schema},
        )
    marked = _features(data, schema)
    markings = _markings(data) if marked else {}
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
                GuardDef(req_str(g, "id"), req_str(g, "summary"), _expr(g, req_str(g, "id")))
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
            unknown = sorted(a.keys() - (_ATTRIBUTE_KEYS_V2 if marked else _ATTRIBUTE_KEYS))
            if unknown:
                raise _newer(f"attribute {k}.{name}", f"the key {unknown[0]!r}")
            attributes[name] = AttributeDef(
                type=req_str(a, "type", path),
                kind=_kind(req_str(a, "kind", path), path),
                required=req_bool(a, "required", path),
                many=req_bool(a, "many", path),
                unique=req_bool(a, "unique", path),
                default=req(a, "default", path),
                mutability=req_str(a, "mutability", path),
                role=_role(opt_str(a, "role", path), path),
                declared_in=req_str(a, "declared_in", path),
                renamed_from=opt_str(a, "renamed_from", path),
                doc=TermDoc.from_json(a, path),
                markings=_attribute_markings(a, path, markings),
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
            on_target_inactive=_target_policy(v, k),
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
            expr=_expr(r, req_str(r, "id")),
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
        markings=markings,
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
