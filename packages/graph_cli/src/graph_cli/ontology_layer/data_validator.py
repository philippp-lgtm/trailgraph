"""Data validation: ``validate_write`` (L1-L4 on the post-state) and ``check_graph`` (batch).

Contracts B3.17, B7.9, R.7; plan W5. One place judges data, so every
``data.*`` code (except ``data.temporal.*``, reported by normalize) comes
from here.

- **Targets** are ``{id, type, path?, local?}`` with ``path`` one of
  ``type``, ``attrs.<name>``, ``edges.<rel>``,
  ``edges.<rel>.qualifiers.<q>``, ``incoming.<rel>``, ``alias``; ``local``
  names the request's local binding. Findings are sorted (B7.9).
- **No cascades.** An entity of unknown or abstract type gets only that
  finding; an unknown attribute or relation is not checked further; a value
  of the wrong datatype is not facet-checked; a transition that is not
  allowed is not guard-checked; edges into an entity of unknown type are
  not range-checked.
- **Write path** (``validate_write``): structural and referential checks on
  every touched entity; uniqueness, acyclicity, write rules and guards on
  the post-state of touched active entities; lifecycle, mutability and
  unlink judgements from the recorded op effects. ``target_not_active`` is
  reported only for edges added by the request.
- **Batch** (``check_graph``): every current revision under one model;
  structural, referential and semantic checks on all, every rule and
  ``cardinality_in`` on active entities only, ``target_not_active`` only
  for active sources. Lifecycle values must name a state
  (``enum_value_unknown``); on the write path the lifecycle checks judge
  them instead.
- **Uniqueness** holds among ``active`` entities of the class family where
  the attribute is declared (``declared_in`` and its subclasses); an alias
  set explicitly must not be bound already (B1.8).
- **Immutable** is write-once: set while absent, never changed or removed.
"""

from __future__ import annotations

import difflib
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.changeset import OpEffect, contains_float
from graph_cli.kernel.clock import format_timestamp, parse_date, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EdgeId, EntityId, TermRef, parse_id
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.query_eval import strict_equal
from graph_cli.kernel.records import RECORD_STATUSES, Revision
from graph_cli.ontology_layer.evaluate import evaluate
from graph_cli.ontology_layer.model import AttributeKind, ClassDef, CompiledModel
from graph_cli.ontology_layer.values import DECIMAL_RE, DURATION_RE, check_facets

PROVENANCE_RELATIONS: Final = frozenset({"core:supersedes", "core:retracts"})
_IRI_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$")
_ALL: Final = Query(record_status=frozenset(RECORD_STATUSES), limit=sys.maxsize)
_ACTIVE: Final = frozenset({"active"})


def _close(word: str, choices: Iterable[str]) -> list[JsonValue] | None:
    found = difflib.get_close_matches(word, sorted(choices), n=3)
    return list(found) or None


def _check_scalar(base: str, value: JsonValue) -> str | None:
    """A datatype problem of one present value, or ``None``."""
    if base in ("string", "text"):
        return None if isinstance(value, str) else f"expected a string ({base})"
    if base == "iri":
        ok = isinstance(value, str) and _IRI_RE.match(value) is not None
        return None if ok else "expected an IRI with a scheme"
    if base == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return None if ok else "expected an integer"
    if base == "boolean":
        return None if isinstance(value, bool) else "expected true or false"
    if base == "decimal":
        ok = isinstance(value, str) and DECIMAL_RE.match(value) is not None
        return None if ok else 'expected a decimal string such as "12.50"'
    if base == "duration":
        ok = isinstance(value, str) and DURATION_RE.match(value) is not None
        return None if ok else "expected an ISO 8601 duration"
    if base == "date":
        try:
            ok = isinstance(value, str) and parse_date(value).isoformat() == value
        except GraphCliError:
            ok = False
        return None if ok else "expected a date YYYY-MM-DD"
    if base == "datetime":
        try:
            ok = isinstance(value, str) and format_timestamp(parse_timestamp(value)) == value
        except GraphCliError:
            ok = False
        return None if ok else "expected a timestamp YYYY-MM-DDTHH:MM:SS.sssZ"
    if base == "external_ref":
        return _check_external_ref(value)
    if base == "json":
        return None
    raise GraphCliError("internal.error", f"unknown base type {base!r}")


def _check_external_ref(value: JsonValue) -> str | None:
    problem = 'expected {"graph": <uuid>, "entity": <uuid>} in canonical form'
    if not isinstance(value, Mapping) or set(value) != {"graph", "entity"}:
        return problem
    for key in ("graph", "entity"):
        text = value[key]
        try:
            if not isinstance(text, str) or str(parse_id(text)) != text:
                return problem
        except GraphCliError:
            return problem
    return None


@dataclass(frozen=True, slots=True)
class _Problem:
    code: str
    message: str
    expected: JsonValue | None = None


@dataclass
class _Checker:
    model: CompiledModel
    snapshot: Snapshot
    local_names: Mapping[EntityId, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    # ---- reporting -----------------------------------------------------

    def add(
        self,
        code: str,
        revision: Revision,
        path: str | None,
        message: str,
        *,
        severity: Severity = Severity.ERROR,
        expected: JsonValue | None = None,
        actual: JsonValue | None = None,
        rule_id: str | None = None,
    ) -> None:
        target: dict[str, JsonValue] = {"id": str(revision.id), "type": revision.type}
        if path is not None:
            target["path"] = path
        local = self.local_names.get(revision.id)
        if local is not None:
            target["local"] = local
        self.findings.append(Finding(severity, code, target, message, expected, actual, rule_id))

    # ---- structure -----------------------------------------------------

    def class_of(self, revision: Revision) -> ClassDef | None:
        """The class when the entity may be checked further; reports unknown or abstract."""
        cls = self.model.classes.get(revision.type)
        if cls is None:
            self.add(
                "data.structural.unknown_type",
                revision,
                "type",
                f"type {revision.type} does not exist",
                expected=_close(revision.type, self.model.classes),
                actual=revision.type,
            )
            return None
        if cls.abstract:
            self.add(
                "data.structural.abstract_type",
                revision,
                "type",
                f"type {revision.type} is abstract; use one of its subclasses",
                expected=[c for c in self.model.subtypes(revision.type) if c != revision.type]
                or None,
            )
            return None
        return cls

    def value_problem(
        self,
        kind: AttributeKind,
        type_ref: str,
        many: bool,
        value: JsonValue,
        *,
        lifecycle_states: bool,
    ) -> _Problem | None:
        if contains_float(value):
            return _Problem("data.structural.float_not_allowed", "floats are not allowed (C.1)")
        if value is None:
            return _Problem(
                "data.structural.datatype_mismatch", "null is not a value; unset it instead"
            )
        base = self.model.value_base(kind, type_ref)
        is_list = isinstance(value, list | tuple)
        if base != "json" and is_list != many:
            message = "expected a list of values" if many else "expected a single value"
            return _Problem("data.structural.cardinality_many", message)
        items: Sequence[JsonValue] = value if many and isinstance(value, list | tuple) else [value]
        for item in items:
            problem = self.item_problem(kind, type_ref, base, item, lifecycle_states)
            if problem is not None:
                return problem
        return None

    def item_problem(
        self, kind: AttributeKind, type_ref: str, base: str, item: JsonValue, states: bool
    ) -> _Problem | None:
        if kind == "enum":
            names = self.model.enums[type_ref].value_names()
            if not isinstance(item, str):
                return _Problem("data.structural.datatype_mismatch", "expected an enum value")
            if item not in names:
                return _Problem(
                    "data.structural.enum_value_unknown",
                    f"{item!r} is not in {type_ref}",
                    list(names),
                )
            return None
        if kind == "lifecycle":
            names = self.model.lifecycles[type_ref].state_names()
            if not isinstance(item, str):
                return _Problem("data.structural.datatype_mismatch", "expected a state name")
            if states and item not in names:
                return _Problem(
                    "data.structural.enum_value_unknown", f"{item!r} is not a state", list(names)
                )
            return None
        message = _check_scalar(base, item)
        if message is not None:
            return _Problem("data.structural.datatype_mismatch", message, base)
        violation = check_facets(item, self.model.facets(kind, type_ref))
        if violation is not None:
            return _Problem("data.structural.facet_violation", violation)
        return None

    def attributes(self, revision: Revision, cls: ClassDef, *, lifecycle_states: bool) -> None:
        for name in sorted(revision.attrs):
            value = revision.attrs[name]
            attribute = cls.attributes.get(name)
            path = f"attrs.{name}"
            if attribute is None:
                self.add(
                    "data.structural.unknown_attribute",
                    revision,
                    path,
                    f"{revision.type} has no attribute {name!r}",
                    expected=_close(name, cls.attributes),
                )
                continue
            problem = self.value_problem(
                attribute.kind,
                attribute.type,
                attribute.many,
                value,
                lifecycle_states=lifecycle_states,
            )
            if problem is not None:
                self.add(problem.code, revision, path, problem.message, expected=problem.expected)
        for name, attribute in cls.attributes.items():
            if attribute.required and attribute.kind != "lifecycle" and name not in revision.attrs:
                self.add(
                    "data.structural.missing_required",
                    revision,
                    f"attrs.{name}",
                    f"required attribute {name!r} is missing",
                )

    # ---- references ----------------------------------------------------

    def edges(self, revision: Revision, *, new_edges: frozenset[EdgeId] | None) -> None:
        counts: Counter[str] = Counter()
        for edge in revision.edges:
            path = f"edges.{edge.rel}"
            relation = self.model.relations.get(edge.rel)
            if relation is None:
                self.add(
                    "data.referential.unknown_relation",
                    revision,
                    path,
                    f"relation {edge.rel} does not exist",
                    expected=_close(edge.rel, self.model.relations),
                )
                continue
            counts[edge.rel] += 1
            target = self.snapshot.get(edge.target)
            if target is None:
                self.add(
                    "data.referential.target_missing",
                    revision,
                    path,
                    f"target {edge.target} does not exist",
                )
                continue
            if not self.model.is_a_any(revision.type, relation.domain):
                self.add(
                    "data.referential.domain_violation",
                    revision,
                    path,
                    f"{edge.rel} does not apply to {revision.type}",
                    expected=list(relation.domain),
                )
            if target.type in self.model.classes and not self.model.is_a_any(
                target.type, relation.range
            ):
                self.add(
                    "data.referential.range_violation",
                    revision,
                    path,
                    f"{edge.rel} does not accept a {target.type} target",
                    expected=list(relation.range),
                    actual=target.type,
                )
            self.qualifiers(revision, edge.rel, edge.qualifiers)
            new = new_edges is None or edge.edge_id in new_edges
            if new and target.record_status != "active" and edge.rel not in PROVENANCE_RELATIONS:
                self.add(
                    "data.referential.target_not_active",
                    revision,
                    path,
                    f"target {edge.target} is {target.record_status}",
                    severity=Severity.WARNING,
                )
        for name, relation in self.model.relations.items():
            if not self.model.is_a_any(revision.type, relation.domain):
                continue
            count = counts[name]
            if count < relation.out.min or (
                relation.out.max is not None and count > relation.out.max
            ):
                bound = (
                    f"{relation.out.min}..{'n' if relation.out.max is None else relation.out.max}"
                )
                self.add(
                    "data.referential.cardinality_out",
                    revision,
                    f"edges.{name}",
                    f"{count} {name} edges, expected {bound}",
                    expected=bound,
                    actual=count,
                )

    def qualifiers(self, revision: Revision, rel: str, values: Mapping[str, JsonValue]) -> None:
        relation = self.model.relations[rel]
        for name in sorted(values):
            path = f"edges.{rel}.qualifiers.{name}"
            qualifier = relation.qualifiers.get(name)
            if qualifier is None:
                self.add(
                    "data.referential.qualifier_unknown",
                    revision,
                    path,
                    f"{rel} has no qualifier {name!r}",
                    expected=_close(name, relation.qualifiers),
                )
                continue
            problem = self.value_problem(
                qualifier.kind, qualifier.type, qualifier.many, values[name], lifecycle_states=True
            )
            if problem is not None:
                self.add(problem.code, revision, path, problem.message, expected=problem.expected)
        for name, qualifier in relation.qualifiers.items():
            if qualifier.required and name not in values:
                self.add(
                    "data.structural.missing_required",
                    revision,
                    f"edges.{rel}.qualifiers.{name}",
                    f"required qualifier {name!r} is missing",
                )

    def incoming_cardinality(self, revision: Revision) -> None:
        for name, relation in self.model.relations.items():
            if not self.model.is_a_any(revision.type, relation.range):
                continue
            bound = relation.into
            if bound.min == 0 and bound.max is None:
                continue
            count = len(self.snapshot.incoming(revision.id, TermRef(name)))
            if count < bound.min or (bound.max is not None and count > bound.max):
                text = f"{bound.min}..{'n' if bound.max is None else bound.max}"
                self.add(
                    "data.referential.cardinality_in",
                    revision,
                    f"incoming.{name}",
                    f"{count} incoming {name} edges, expected {text}",
                    expected=text,
                    actual=count,
                )

    # ---- semantics -----------------------------------------------------

    def unique_values(self, revision: Revision, cls: ClassDef) -> None:
        for name, attribute in cls.attributes.items():
            value = revision.attrs.get(name)
            if not attribute.unique or value is None:
                continue
            numeric = self.model.value_base(attribute.kind, attribute.type) == "decimal"
            query = Query(
                types=tuple(TermRef(t) for t in self.model.subtypes(attribute.declared_in)),
                where=Cmp(name, "eq", value, numeric=numeric),
                record_status=_ACTIVE,
                limit=11,
            )
            others = [r.id for r in self.snapshot.query(query).items if r.id != revision.id]
            if others:
                self.add(
                    "data.semantic.unique_violation",
                    revision,
                    f"attrs.{name}",
                    f"the value of {name!r} is already used by {others[0]}",
                )

    def cycle(self, start: EntityId, rel: str) -> list[EntityId] | None:
        """A path ``start -> ... -> start`` over current ``rel`` edges, or ``None``."""
        parents: dict[EntityId, EntityId | None] = {start: None}
        stack = [start]
        while stack:
            node = stack.pop()
            revision = self.snapshot.get(node)
            if revision is None:
                continue
            targets = sorted({e.target for e in revision.edges if e.rel == rel}, reverse=True)
            for target in targets:
                if target == start:
                    path = [node]
                    while (parent := parents[path[-1]]) is not None:
                        path.append(parent)
                    return [*reversed(path), start]
                if target not in parents:
                    parents[target] = node
                    stack.append(target)
        return None

    def cycles(self, starts: Iterable[Revision]) -> None:
        acyclic = [n for n, r in self.model.relations.items() if "acyclic" in r.characteristics]
        seen: set[tuple[str, frozenset[EntityId]]] = set()
        for revision in starts:
            for rel in acyclic:
                if not any(e.rel == rel for e in revision.edges):
                    continue
                path = self.cycle(revision.id, rel)
                if path is None:
                    continue
                key = (rel, frozenset(path))
                if key in seen:
                    continue
                seen.add(key)
                self.add(
                    "data.semantic.cycle",
                    revision,
                    f"edges.{rel}",
                    f"{rel} forms a cycle of {len(path) - 1} entities",
                    actual=[str(e) for e in path],
                )

    def rules(self, revision: Revision, whens: frozenset[str]) -> None:
        for rule in self.model.rules:
            if rule.when not in whens or not self.model.is_a(revision.type, rule.target):
                continue
            if not evaluate(rule.expr, revision, self.snapshot, self.model):
                self.add(
                    "data.rule.violated",
                    revision,
                    None,
                    rule.doc.summary,
                    severity=Severity(rule.severity),
                    rule_id=rule.id,
                )


def validate_write(
    model: CompiledModel,
    before: Snapshot,
    after: Snapshot,
    *,
    touched: Sequence[EntityId],
    effects: Sequence[OpEffect],
    alias_allocations: Sequence[tuple[EntityId, str]] = (),
    local_names: Mapping[EntityId, str] | None = None,
) -> list[Finding]:
    """L1-L4 on the post-state of every touched entity (B3.17, W.11)."""
    checker = _Checker(model, after, dict(local_names or {}))
    checkable: dict[EntityId, tuple[Revision, ClassDef]] = {}
    for entity in touched:
        revision = after.get(entity)
        if revision is None:
            raise GraphCliError("internal.error", f"touched entity {entity} has no revision")
        cls = checker.class_of(revision)
        if cls is None:
            continue
        checkable[entity] = (revision, cls)
        checker.attributes(revision, cls, lifecycle_states=False)
        previous = before.get(entity)
        old_edges = frozenset(e.edge_id for e in previous.edges) if previous else frozenset()
        checker.edges(revision, new_edges=frozenset(e.edge_id for e in revision.edges) - old_edges)
        if previous is not None:
            _immutables(checker, previous, revision, cls)
    active = [rev for rev, _ in checkable.values() if rev.record_status == "active"]
    for revision in active:
        checker.unique_values(revision, checkable[revision.id][1])
        checker.rules(revision, frozenset({"write"}))
    checker.cycles(rev for rev, _ in checkable.values())
    for effect in effects:
        if effect.entity in checkable:
            _judge_effect(checker, effect, *checkable[effect.entity])
        elif effect.entity not in touched:  # an op that could not apply touched nothing
            revision = after.get(effect.entity)
            cls = None if revision is None else model.classes.get(revision.type)
            if revision is not None and cls is not None and not cls.abstract:
                _judge_effect(checker, effect, revision, cls)
    _aliases(checker, before, after, effects, alias_allocations)
    return sort_findings(checker.findings)


def _immutables(checker: _Checker, previous: Revision, revision: Revision, cls: ClassDef) -> None:
    for name, attribute in cls.attributes.items():
        if attribute.mutability != "immutable":
            continue
        old = previous.attrs.get(name)
        if old is not None and not strict_equal(old, revision.attrs.get(name)):
            checker.add(
                "data.mutability.immutable_changed",
                revision,
                f"attrs.{name}",
                f"{name!r} is immutable once set",
            )


def _judge_effect(checker: _Checker, effect: OpEffect, revision: Revision, cls: ClassDef) -> None:
    model = checker.model
    if effect.op == "create":
        for name, value in effect.initial:
            lifecycle = model.lifecycles[cls.attributes[name].type]
            if value not in lifecycle.initial:
                checker.add(
                    "data.lifecycle.invalid_initial_state",
                    revision,
                    f"attrs.{name}",
                    "no initial state given and the lifecycle has several"
                    if value is None
                    else f"{value!r} is not an initial state",
                    expected=list(lifecycle.initial),
                    actual=value,
                )
    elif effect.op == "update" and effect.applied:
        for name in effect.names:
            attribute = cls.attributes.get(name)
            if attribute is None and name not in revision.attrs:  # an unset; a set is reported
                checker.add(
                    "data.structural.unknown_attribute",
                    revision,
                    f"attrs.{name}",
                    f"{revision.type} has no attribute {name!r}",
                    expected=_close(name, cls.attributes),
                )
            elif attribute is not None and attribute.kind == "lifecycle":
                checker.add(
                    "data.lifecycle.direct_update_of_lifecycle_attribute",
                    revision,
                    f"attrs.{name}",
                    f"{name!r} changes only through a transition",
                )
    elif effect.op == "transition":
        _judge_transition(checker, effect, revision, cls)
    elif effect.op == "unlink" and effect.problem is not None:
        checker.add(
            "data.referential.edge_missing",
            revision,
            "edges" if effect.rel is None else f"edges.{effect.rel}",
            f"unlink found {effect.problem}",
        )


def _judge_transition(
    checker: _Checker, effect: OpEffect, revision: Revision, cls: ClassDef
) -> None:
    path = "attrs" if effect.attr is None else f"attrs.{effect.attr}"
    if effect.problem is not None:
        checker.add("data.lifecycle.transition_not_allowed", revision, path, effect.problem)
        return
    if not effect.applied or effect.attr is None:
        return
    lifecycle = checker.model.lifecycles[cls.attributes[effect.attr].type]
    allowed = [t for t in lifecycle.transitions if t.from_state == effect.from_state]
    match = next((t for t in allowed if t.to_state == effect.to_state), None)
    if match is None:
        checker.add(
            "data.lifecycle.transition_not_allowed",
            revision,
            path,
            f"no transition from {effect.from_state!r} to {effect.to_state!r}",
            expected=sorted({t.to_state for t in allowed}) or None,
            actual=effect.to_state,
        )
        return
    for guard in match.guards:
        if not evaluate(guard.expr, revision, checker.snapshot, checker.model):
            checker.add(
                "data.lifecycle.guard_failed",
                revision,
                path,
                guard.summary,
                rule_id=guard.id,
            )


def _aliases(
    checker: _Checker,
    before: Snapshot,
    after: Snapshot,
    effects: Sequence[OpEffect],
    allocations: Sequence[tuple[EntityId, str]],
) -> None:
    explicit = {(e.entity, e.alias) for e in effects if e.op == "set_alias" and e.applied}
    taken: dict[str, EntityId] = {}
    for entity, alias in allocations:
        revision = after.get(entity)
        assert revision is not None
        bound = before.resolve_alias(alias) if (entity, alias) in explicit else None
        if bound is not None or alias in taken:
            owner = bound[0] if bound is not None else taken[alias]
            checker.add(
                "data.semantic.unique_violation",
                revision,
                "alias",
                f"alias {alias} is already bound to {owner}; aliases are never reused",
            )
        taken.setdefault(alias, entity)


def check_graph(model: CompiledModel, snapshot: Snapshot) -> list[Finding]:
    """Every current revision under ``model`` (batch, R.7); used by B7.14."""
    checker = _Checker(model, snapshot)
    revisions = list(snapshot.query(_ALL).items)
    checkable: list[tuple[Revision, ClassDef]] = []
    for revision in revisions:
        cls = checker.class_of(revision)
        if cls is None:
            continue
        checkable.append((revision, cls))
        checker.attributes(revision, cls, lifecycle_states=True)
        active_source = revision.record_status == "active"
        checker.edges(revision, new_edges=None if active_source else frozenset())
    groups: dict[tuple[str, str, bytes], list[Revision]] = {}
    for revision, cls in checkable:
        if revision.record_status != "active":
            continue
        for name, attribute in cls.attributes.items():
            value = revision.attrs.get(name)
            if attribute.unique and value is not None:
                key = (
                    attribute.declared_in,
                    name,
                    _unique_key(model, attribute.kind, attribute.type, value),
                )
                groups.setdefault(key, []).append(revision)
        checker.rules(revision, frozenset({"write", "batch"}))
        checker.incoming_cardinality(revision)
    for (_, name, _), members in groups.items():
        if len(members) < 2:
            continue
        for revision in members:
            other = next(m.id for m in members if m.id != revision.id)
            checker.add(
                "data.semantic.unique_violation",
                revision,
                f"attrs.{name}",
                f"the value of {name!r} is also used by {other}",
            )
    checker.cycles(rev for rev, _ in checkable)
    return sort_findings(checker.findings)


def _unique_key(
    model: CompiledModel, kind: AttributeKind, type_ref: str, value: JsonValue
) -> bytes:
    if model.value_base(kind, type_ref) == "decimal" and isinstance(value, str):
        try:
            return str(Decimal(value).normalize()).encode()
        except InvalidOperation:
            pass
    return canonical_bytes(value)
