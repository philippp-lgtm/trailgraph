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
- **Inactive targets** (write path): a touched entity that goes inactive
  (retract, supersede) while active sources in the post-state still point at
  it over a relation with ``on_target_inactive`` ``warn`` or ``refuse`` gets
  one ``target_referenced`` per relation, an error under ``refuse``.
  ``target_not_active`` is an error on a ``refuse`` relation, on both paths.
- **Neighbours** (write path): a write rule with an aggregate (``count``,
  ``sum``) also runs on the untouched active neighbours of touched
  entities: an entity at the other end of an edge of a touched entity, in
  the pre- or post-state, whose aggregate counts over that edge (one hop).
  An edge ``a -rel-> b`` makes ``b`` a neighbour for ``(rel, in)`` and ``a``
  for ``(rel, out)``.
- **Batch** (``check_graph``): every current revision under one model;
  structural, referential and semantic checks on all, every rule and
  ``cardinality_in`` on active entities only, ``target_not_active`` only
  for active sources. Lifecycle values must name a state
  (``enum_value_unknown``); on the write path the lifecycle checks judge
  them instead.
- **Rule findings** of a rule with an aggregate (``count``, ``sum``) carry
  ``actual = [{operand, value}]`` (``evaluate.explain``); others carry none.
- **Uniqueness** holds among ``active`` entities of the class family where
  the attribute is declared (``declared_in`` and its subclasses); an alias
  set explicitly must not be bound already (B1.8).
- **Names** (``possible_duplicate``, a warning): a label-role value an
  active entity gains in a write (all of a create's; on an update those no
  earlier revision had) that another active entity of the family where the
  attribute is declared (as for uniqueness) also carries in a label-role
  attribute. Main and alternative names count alike; other types may share
  a name. ``same``: equal ``search_key`` without spaces, so case, accents,
  punctuation and word breaks do not count ("E-Mail", "email"). ``similar``
  (write path only): the same words but one, and that one a typo of the
  other: at most one edit from 5 characters, two from 10, never a changed
  number, so "Part 1" and "Part 2" or "Phase I" and "Phase II" stay apart.
  Looser than ``lookup``, which resolves names: a warning should rather
  ask once too often. A pair ``unique`` already refuses (same attribute,
  same value) is not reported again. Batch reports each ``same`` name once,
  at the entity with the lowest id; a ``similar`` pair already in the graph
  is someone's decision or a ``graph search`` away.
- **Deprecated terms** (``deprecated_term``, a warning, write path only): a
  write that creates an entity of a deprecated class, sets or changes a value
  of a deprecated attribute or of one whose datatype, enum or lifecycle is
  deprecated, or adds an edge of a deprecated relation. The message carries
  the term's summary, which names the replacement. Values and edges the
  entity already had are not reported again; ``graph find`` or ``graph
  query`` list existing uses.
- **Immutable** is write-once: set while absent, never changed or removed.
- **Blobs** (write path): a new or changed ``blob`` value names a stored file
  or one the request stages (``blob_missing``); batch checks the shape only,
  storage ``verify`` covers presence.
- **A caller who cannot see everything** (write path, ``CallerView``; type read limits plan V6):
  integrity is judged on the whole graph, but what the findings say is limited to what the
  caller sees. A new edge to an entity the caller cannot see is ``target_missing``; a create
  of a type, and a new edge of a relation, outside the caller's model view are
  ``unknown_type`` and ``unknown_relation``; ``expected``, ``connecting`` and close matches
  come from that view; ``possible_duplicate`` ignores entities the caller cannot see. A
  finding that names one (a value used by it, an alias it owns, a cycle or a referrer
  through it, a count that includes its edges, a rule or guard the view does not have, a
  relation the view lacks) keeps its code and severity, its target (when that is visible)
  and nothing else: the message is ``CONCEALED`` and ``expected``, ``actual``, ``connecting``
  and ``rule_id`` are gone. The ``explain`` values of a rule are dropped when an aggregate
  counts a relation that reaches a hidden type. So a refusal can show that something hidden
  exists, never which entity, type, relation or value.
"""

from __future__ import annotations

import difflib
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from functools import cached_property
from typing import Final

from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.changeset import OpEffect, contains_float
from graph_cli.kernel.clock import format_timestamp, parse_date, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EdgeId, EntityId, TermRef, parse_id
from graph_cli.kernel.langtag import tagged_parts, text_of
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Cmp, Or, Query
from graph_cli.kernel.query_eval import strict_equal
from graph_cli.kernel.records import RECORD_STATUSES, EdgeRecord, Revision
from graph_cli.kernel.text import edit_distance, search_key
from graph_cli.ontology_layer.connecting import connecting
from graph_cli.ontology_layer.evaluate import evaluate, explain
from graph_cli.ontology_layer.expr_ast import aggregate_relations
from graph_cli.ontology_layer.model import (
    AttributeDef,
    AttributeKind,
    ClassDef,
    CompiledModel,
    DatatypeDef,
    EnumDef,
    LifecycleDef,
    RelationDef,
    RuleDef,
    TermDoc,
)
from graph_cli.ontology_layer.values import (
    DECIMAL_RE,
    DURATION_RE,
    blob_problem,
    check_facets,
    lang_value_problem,
    local_path_problem,
)
from graph_cli.ontology_layer.vocabulary import LANG_BASES

PROVENANCE_RELATIONS: Final = frozenset({"core:supersedes", "core:retracts"})
_IRI_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$")
_ALL: Final = Query(record_status=frozenset(RECORD_STATUSES), limit=sys.maxsize)
_ACTIVE: Final = frozenset({"active"})
_MAX_CANDIDATES: Final = 5
_MAX_SOURCES: Final = 20  # source ids a target_referenced finding lists


_DIGITS: Final = re.compile(r"\d+")

UNKNOWN_RELATION: Final = "data.referential.unknown_relation"

# What a finding says that names something the caller cannot see.
CONCEALED: Final = "conflicts with data you cannot see"


@dataclass(frozen=True, slots=True)
class CallerView:
    """What the caller of a write sees: its filtered model (``services.visibility``), the
    concrete types it sees and whether it sees an entity. The validator reads the graph
    itself, never through this view, so integrity holds for the whole graph; the view only
    decides what its findings may say. ``sees`` is true for an entity the request creates."""

    model: CompiledModel
    visible: frozenset[str]
    sees: Callable[[EntityId], bool]


@dataclass(frozen=True, slots=True)
class _Name:
    """One label-role value of an entity, with its words and match key."""

    revision: Revision
    attr: str
    role: str
    value: str  # the text
    words: tuple[str, ...]
    stored: JsonValue  # the value as stored (language-tagged or not)

    @property
    def key(self) -> str:
        return "".join(self.words)


def _names(model: CompiledModel, revision: Revision | None) -> list[_Name]:
    if revision is None or revision.type not in model.classes:
        return []
    found = []
    for attr, role in model.label_attributes(revision.type):
        value = revision.attrs.get(attr)
        for item in value if isinstance(value, list) else [value]:
            text = text_of(item)
            if text is not None:
                words = tuple(search_key(text).split())
                found.append(_Name(revision, attr, role, text, words, item))
    return found


def _typo(a: str, b: str) -> bool:
    """``b`` is ``a`` misspelt: no number changed, few edits for the length."""
    if _DIGITS.findall(a) != _DIGITS.findall(b):
        return False
    shorter = min(len(a), len(b))
    limit = 0 if shorter < 5 else 1 if shorter < 10 else 2
    return 0 < edit_distance(a, b, limit) <= limit


def _similar(a: Sequence[str], b: Sequence[str]) -> bool:
    """The same words but one, and that one a typo."""
    if len(a) != len(b):
        return False
    differ = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
    return len(differ) == 1 and _typo(*differ[0])


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
    if base == "blob":
        return blob_problem(value)
    if base == "local_path":
        return local_path_problem(value)
    if base in LANG_BASES:
        return lang_value_problem(value)
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
    view: CallerView | None = None
    _bare: list[Finding] = field(default_factory=list, init=False, repr=False)
    _connecting: dict[tuple[str, str], JsonValue | None] = field(
        default_factory=dict, init=False, repr=False
    )

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
        connect: tuple[str, str] | None = None,
        names: Iterable[EntityId] = (),
        conceal: bool = False,
    ) -> None:
        """Report a finding on ``revision``. ``names`` are the other entities the message or the
        values name; for a caller who cannot see one of them (or when ``conceal`` says so, or
        the path names a relation its view lacks) the finding is ``concealed``."""
        target: dict[str, JsonValue] = {"id": str(revision.id), "type": revision.type}
        if path is not None:
            target["path"] = path
        local = self.local_names.get(revision.id)
        if local is not None:
            target["local"] = local
        finding = Finding(severity, code, target, message, expected, actual, rule_id)
        view = self.view
        if view is not None:
            own_hidden = not view.sees(revision.id)
            if (
                conceal
                or own_hidden
                or (code != UNKNOWN_RELATION and self.relation_hidden(path))
                or any(not view.sees(e) for e in names)
            ):
                finding = self.concealed(finding, path, own_hidden)
                connect = None
        if finding in self._bare:  # two ops may name the same problem
            return
        self._bare.append(finding)
        if connect is not None:
            finding = replace(finding, connecting=self.connecting(*connect))
        self.findings.append(finding)

    @property
    def answer_model(self) -> CompiledModel:
        """The model a finding names terms from: the caller's view, else the model itself."""
        return self.model if self.view is None else self.view.model

    def relation_hidden(self, path: str | None) -> bool:
        """Whether ``path`` (``edges.<rel>...``, ``incoming.<rel>``) names a relation the
        caller's model view does not have."""
        if self.view is None or path is None:
            return False
        head, _, rest = path.partition(".")
        if head not in ("edges", "incoming") or not rest:
            return False
        return rest.split(".", 1)[0] not in self.view.model.relations

    def concealed(self, finding: Finding, path: str | None, own_hidden: bool) -> Finding:
        """``finding`` as the caller may see it: code, severity and a target that names only
        what it sees (nothing at all when the entity itself is hidden)."""
        target: dict[str, JsonValue] = {}
        if not own_hidden:
            target = {k: v for k, v in finding.target.items() if k != "path"}
            if path is not None:
                target["path"] = path.split(".")[0] if self.relation_hidden(path) else path
        return Finding(finding.severity, finding.code, target, CONCEALED)

    def connecting(self, source: str, target: str) -> JsonValue | None:
        """What would connect two types, when both exist in the model (once per pair)."""
        pair = (source, target)
        if pair not in self._connecting:
            model = self.answer_model
            known = source in model.classes and target in model.classes
            self._connecting[pair] = connecting(model, source, target) if known else None
        return self._connecting[pair]

    # ---- structure -----------------------------------------------------

    def class_of(self, revision: Revision, *, created: bool = False) -> ClassDef | None:
        """The class when the entity may be checked further; reports unknown or abstract. A
        type the caller's view lacks is unknown to a create (``created``)."""
        cls = self.model.classes.get(revision.type)
        shown = self.answer_model
        if cls is None or (created and revision.type not in shown.classes):
            self.add(
                "data.structural.unknown_type",
                revision,
                "type",
                f"type {revision.type} does not exist",
                expected=_close(revision.type, shown.classes),
                actual=revision.type,
            )
            return None
        if cls.abstract:
            self.add(
                "data.structural.abstract_type",
                revision,
                "type",
                f"type {revision.type} is abstract; use one of its subclasses",
                expected=[c for c in shown.subtypes(revision.type) if c != revision.type] or None,
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
            elif attribute.role == "label" and isinstance(value, list | tuple):
                self.one_label_per_language(revision, path, value)
        for name, attribute in cls.attributes.items():
            if attribute.required and attribute.kind != "lifecycle" and name not in revision.attrs:
                self.add(
                    "data.structural.missing_required",
                    revision,
                    f"attrs.{name}",
                    f"required attribute {name!r} is missing",
                )

    def one_label_per_language(
        self, revision: Revision, path: str, values: Sequence[JsonValue]
    ) -> None:
        """A main label on ``lang_string`` holds at most one value per language (SKOS)."""
        tags = Counter(parts[1] for v in values if (parts := tagged_parts(v)) is not None)
        repeated = sorted(tag for tag, count in tags.items() if count > 1)
        if repeated:
            self.add(
                "data.structural.language_repeated",
                revision,
                path,
                f"the main label holds one value per language; {', '.join(repeated)}"
                " has more (put further names in an alt_label attribute)",
                actual=list(repeated),
            )

    # ---- references ----------------------------------------------------

    def edges(self, revision: Revision, *, new_edges: frozenset[EdgeId] | None) -> None:
        counts: Counter[str] = Counter()
        unseen: Counter[str] = Counter()  # edges into entities the caller cannot see
        for edge in revision.edges:
            if self.edge(revision, edge, new=new_edges is None or edge.edge_id in new_edges):
                counts[edge.rel] += 1
            if self.view is not None and not self.view.sees(edge.target):
                unseen[edge.rel] += 1
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
                    conceal=unseen[name] > 0,  # the count includes edges the caller lacks
                )

    def edge(self, revision: Revision, edge: EdgeRecord, *, new: bool) -> bool:
        """One edge of ``revision``; true when its relation exists (it counts)."""
        path = f"edges.{edge.rel}"
        relation = self.model.relations.get(edge.rel)
        view = self.view
        # A new edge of a relation the caller's view lacks is an unknown relation to it.
        if relation is None or (view is not None and new and edge.rel not in view.model.relations):
            self.add(
                UNKNOWN_RELATION,
                revision,
                path,
                f"relation {edge.rel} does not exist",
                expected=_close(edge.rel, self.answer_model.relations),
            )
            return False
        target = self.snapshot.get(edge.target)
        # A new edge to an entity the caller cannot see is an edge to a missing one.
        if target is None or (view is not None and new and not view.sees(edge.target)):
            self.add(
                "data.referential.target_missing",
                revision,
                path,
                f"target {edge.target} does not exist",
            )
            return True
        shown = relation if view is None else view.model.relations.get(edge.rel, relation)
        if not self.model.is_a_any(revision.type, relation.domain):
            self.add(
                "data.referential.domain_violation",
                revision,
                path,
                f"{edge.rel} does not apply to {revision.type}",
                expected=list(shown.domain),
                connect=(revision.type, target.type),
                names=(edge.target,),
            )
        # A target created by this request of a type the view lacks is of an unknown type: no
        # range check, as for a type the model does not have.
        judged = target.type in self.model.classes and (
            view is None or target.type in view.model.classes or not view.sees(edge.target)
        )
        if judged and not self.model.is_a_any(target.type, relation.range):
            self.add(
                "data.referential.range_violation",
                revision,
                path,
                f"{edge.rel} does not accept a {target.type} target",
                expected=list(shown.range),
                actual=target.type,
                connect=(revision.type, target.type),
                names=(edge.target,),
            )
        self.qualifiers(revision, edge.rel, edge.qualifiers, names=(edge.target,))
        if new and target.record_status != "active" and edge.rel not in PROVENANCE_RELATIONS:
            self.add(
                "data.referential.target_not_active",
                revision,
                path,
                f"target {edge.target} is {target.record_status}",
                severity=_target_severity(relation),
            )
        return True

    def qualifiers(
        self,
        revision: Revision,
        rel: str,
        values: Mapping[str, JsonValue],
        *,
        names: Iterable[EntityId] = (),
    ) -> None:
        relation = self.model.relations[rel]
        names = tuple(names)
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
                    names=names,
                )
                continue
            problem = self.value_problem(
                qualifier.kind, qualifier.type, qualifier.many, values[name], lifecycle_states=True
            )
            if problem is not None:
                self.add(
                    problem.code,
                    revision,
                    path,
                    problem.message,
                    expected=problem.expected,
                    names=names,
                )
        for name, qualifier in relation.qualifiers.items():
            if qualifier.required and name not in values:
                self.add(
                    "data.structural.missing_required",
                    revision,
                    f"edges.{rel}.qualifiers.{name}",
                    f"required qualifier {name!r} is missing",
                    names=names,
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

    @cached_property
    def name_index(self) -> dict[str, list[_Name]]:
        """Every label-role value of the active entities, by match key."""
        labelled = {c: self.model.label_attributes(c) for c in self.model.classes}
        types = sorted(c for c, found in labelled.items() if found)
        attrs = sorted({a for found in labelled.values() for a, _ in found})
        index: dict[str, list[_Name]] = {}
        if not types:
            return index
        query = Query(
            types=tuple(TermRef(t) for t in types),
            where=Or(tuple(Cmp(a, "exists", True) for a in attrs)),
            record_status=_ACTIVE,
            limit=sys.maxsize,
        )
        for revision in self.snapshot.query(query).items:
            for name in _names(self.model, revision):
                index.setdefault(name.key, []).append(name)
        return index

    def same_names(
        self, name: _Name, cls: ClassDef, *, similar: bool = False
    ) -> list[tuple[_Name, str]]:
        """Other active entities of the attribute's family carrying the name (``same``)
        or, with ``similar``, a near spelling of it; one each, ``same`` first."""
        attribute = cls.attributes[name.attr]
        family = set(self.model.subtypes(attribute.declared_in))
        found = [(other, "same") for other in self.name_index.get(name.key, ())]
        if similar:
            found += [
                (other, "similar")
                for key, others in self.name_index.items()
                if key != name.key
                for other in others
                if _similar(name.words, other.words)
            ]
        seen: dict[EntityId, tuple[_Name, str]] = {}
        for other, match in found:
            entity = other.revision.id
            if entity == name.revision.id or entity in seen or other.revision.type not in family:
                continue
            if self.view is not None and not self.view.sees(entity):
                continue  # a name only an entity the caller cannot see carries
            if attribute.unique and other.attr == name.attr and other.stored == name.stored:
                continue  # unique_violation reports it
            seen[entity] = (other, match)
        return sorted(
            seen.values(),
            key=lambda m: (m[1] != "same", m[0].revision.alias or "", str(m[0].revision.id)),
        )

    def duplicate_name(self, name: _Name, matches: Sequence[tuple[_Name, str]]) -> None:
        first, match = matches[0]
        relation = "also a name" if match == "same" else "close to a name"
        self.add(
            "data.semantic.possible_duplicate",
            name.revision,
            f"attrs.{name.attr}",
            f"{name.value!r} is {relation} of {first.revision.alias or first.revision.id}"
            f" ({first.revision.type}, {first.attr})",
            severity=Severity.WARNING,
            actual=[
                {
                    "id": str(o.revision.id),
                    "alias": o.revision.alias,
                    "type": o.revision.type,
                    "attr": o.attr,
                    "role": o.role,
                    "name": None if o.role == "hidden_label" else o.value,
                    "match": m,
                }
                for o, m in matches[:_MAX_CANDIDATES]
            ],
        )

    def new_names(self, revision: Revision, cls: ClassDef, previous: Revision | None) -> None:
        """Write path: names the entity did not carry before."""
        known = {n.key for n in _names(self.model, previous)}
        for name in _names(self.model, revision):
            if name.key in known:
                continue
            matches = self.same_names(name, cls, similar=True)
            if matches:
                self.duplicate_name(name, matches)

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
                    names=others[:1],
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
                    names=path,
                )

    @cached_property
    def aggregates(self) -> dict[str, frozenset[tuple[str, str]]]:
        """Rule id -> ``(rel, direction)`` of its aggregates."""
        return {rule.id: aggregate_relations(rule.expr) for rule in self.model.rules}

    def rules(
        self,
        revision: Revision,
        whens: frozenset[str],
        links: frozenset[tuple[str, str]] | None = None,
    ) -> None:
        """Rules on ``revision``; with ``links`` only those aggregating over one of them."""
        for rule in self.model.rules:
            if rule.when not in whens or not self.model.is_a(revision.type, rule.target):
                continue
            if links is not None and self.aggregates[rule.id].isdisjoint(links):
                continue
            if not evaluate(rule.expr, revision, self.snapshot, self.model):
                actual = explain(rule.expr, revision, self.snapshot, self.model)
                view = self.view
                absent = view is not None and all(r.id != rule.id for r in view.model.rules)
                if view is not None and actual is not None and self.reaches_hidden(rule):
                    actual = None  # the values count entities the caller cannot see
                self.add(
                    "data.rule.violated",
                    revision,
                    None,
                    rule.doc.summary,
                    severity=Severity(rule.severity),
                    actual=actual,
                    rule_id=rule.id,
                    conceal=absent,  # a rule the caller's view does not have
                )

    def reaches_hidden(self, rule: RuleDef) -> bool:
        """Whether an aggregate of ``rule`` counts a relation whose far end (in the model
        itself) covers a concrete type the caller does not see."""
        assert self.view is not None
        for rel, direction in self.aggregates[rule.id]:
            relation = self.model.relations.get(rel)
            if relation is None:
                continue
            ends = relation.range if direction == "out" else relation.domain
            covered = {c for end in ends for c in self.model.subtypes(end)} if ends else None
            for name, cls in self.model.classes.items():
                reached = covered is None or name in covered
                if reached and not cls.abstract and name not in self.view.visible:
                    return True
        return False

    def guard_hidden(self, guard_id: str) -> bool:
        """Whether the caller's model view lacks the guard (it names a term the view lacks)."""
        if self.view is None:
            return False
        return not any(
            g.id == guard_id
            for lifecycle in self.view.model.lifecycles.values()
            for transition in lifecycle.transitions
            for g in transition.guards
        )


def blob_values(model: CompiledModel, revision: Revision) -> list[str]:
    """The well-formed values of ``blob`` attributes of a revision, sorted and unique."""
    cls = model.classes.get(revision.type)
    if cls is None:
        return []
    found: set[str] = set()
    for name, attribute in cls.attributes.items():
        if model.value_base(attribute.kind, attribute.type) != "blob":
            continue
        value = revision.attrs.get(name)
        items = value if isinstance(value, list | tuple) else [value]
        found.update(v for v in items if isinstance(v, str) and blob_problem(v) is None)
    return sorted(found)


def _stored_blobs(
    checker: _Checker,
    previous: Revision | None,
    revision: Revision,
    cls: ClassDef,
    blob_exists: Callable[[str], bool],
) -> None:
    """A new or changed ``blob`` value names a stored (or staged) blob."""
    model = checker.model
    for name, attribute in sorted(cls.attributes.items()):
        if model.value_base(attribute.kind, attribute.type) != "blob" or name not in revision.attrs:
            continue
        value = revision.attrs[name]
        if previous is not None and previous.attrs.get(name) == value:
            continue
        items = value if isinstance(value, list | tuple) else [value]
        for item in items:
            if isinstance(item, str) and blob_problem(item) is None and not blob_exists(item):
                checker.add(
                    "data.referential.blob_missing",
                    revision,
                    f"attrs.{name}",
                    f"no stored file has the hash {item}",
                    actual=item,
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
    blob_exists: Callable[[str], bool] | None = None,
    view: CallerView | None = None,
) -> list[Finding]:
    """L1-L4 on the post-state of every touched entity (B3.17, W.11).

    ``blob_exists`` tells whether a blob is stored or staged by the request;
    without it (batch) ``blob`` values are checked for their shape only. ``view`` limits what
    the findings say to what the caller sees (see the module docstring); the judgment itself
    is the same.
    """
    checker = _Checker(model, after, dict(local_names or {}), view=view)
    checkable: dict[EntityId, tuple[Revision, ClassDef]] = {}
    for entity in touched:
        revision = after.get(entity)
        if revision is None:
            raise GraphCliError("internal.error", f"touched entity {entity} has no revision")
        previous = before.get(entity)
        cls = checker.class_of(revision, created=previous is None)
        if cls is None:
            continue
        checkable[entity] = (revision, cls)
        checker.attributes(revision, cls, lifecycle_states=False)
        old_edges = frozenset(e.edge_id for e in previous.edges) if previous else frozenset()
        new_edges = frozenset(e.edge_id for e in revision.edges) - old_edges
        checker.edges(revision, new_edges=new_edges)
        _deprecated(checker, previous, revision, cls, new_edges)
        if previous is not None:
            _immutables(checker, previous, revision, cls)
        if blob_exists is not None:
            _stored_blobs(checker, previous, revision, cls, blob_exists)
    active = [rev for rev, _ in checkable.values() if rev.record_status == "active"]
    for revision in active:
        checker.unique_values(revision, checkable[revision.id][1])
        checker.new_names(revision, checkable[revision.id][1], before.get(revision.id))
        checker.rules(revision, frozenset({"write"}))
    _neighbours(checker, before, after, touched)
    _retired_targets(checker, before, after, touched)
    checker.cycles(rev for rev, _ in checkable.values())
    for effect in effects:
        if effect.entity in checkable:
            _judge_effect(checker, before, effect, *checkable[effect.entity])
        elif effect.entity not in touched:  # an op that could not apply touched nothing
            revision = after.get(effect.entity)
            cls = None if revision is None else model.classes.get(revision.type)
            if revision is not None and cls is not None and not cls.abstract:
                _judge_effect(checker, before, effect, revision, cls)
    _aliases(checker, before, after, effects, alias_allocations)
    return sort_findings(checker.findings)


def _neighbours(
    checker: _Checker, before: Snapshot, after: Snapshot, touched: Sequence[EntityId]
) -> None:
    """Aggregate write rules on untouched active neighbours of touched entities."""
    wanted = {
        rel
        for rule in checker.model.rules
        if rule.when == "write"
        for rel, _ in checker.aggregates[rule.id]
    }
    if not wanted:
        return
    links: dict[EntityId, set[tuple[str, str]]] = {}
    for entity in touched:
        for snapshot in (before, after):
            revision = snapshot.get(entity)
            for edge in () if revision is None else revision.edges:
                if edge.rel in wanted:
                    links.setdefault(edge.target, set()).add((edge.rel, "in"))
            for rel in sorted(wanted):
                for source, _ in snapshot.incoming(entity, TermRef(rel)):
                    links.setdefault(source, set()).add((rel, "out"))
    done = set(touched)
    for entity in sorted(links):
        revision = after.get(entity)
        if entity in done or revision is None or revision.record_status != "active":
            continue
        cls = checker.model.classes.get(revision.type)
        if cls is None or cls.abstract:
            continue
        checker.rules(revision, frozenset({"write"}), frozenset(links[entity]))


def _target_severity(relation: RelationDef) -> Severity:
    """How an active edge to an inactive target is reported under the relation's policy."""
    return Severity.ERROR if relation.on_target_inactive == "refuse" else Severity.WARNING


def _retired_targets(
    checker: _Checker, before: Snapshot, after: Snapshot, touched: Sequence[EntityId]
) -> None:
    """Touched entities that go inactive while active sources still point at them.

    Only relations whose ``on_target_inactive`` is ``warn`` or ``refuse`` count. The
    after-state decides, so a change set that relinks or retracts the sources passes.
    """
    guarded = sorted(
        (name, relation)
        for name, relation in checker.model.relations.items()
        if relation.on_target_inactive != "allow" and name not in PROVENANCE_RELATIONS
    )
    if not guarded:
        return
    for entity in touched:
        previous, revision = before.get(entity), after.get(entity)
        if previous is None or revision is None:
            continue
        if previous.record_status != "active" or revision.record_status == "active":
            continue
        for name, relation in guarded:
            sources = sorted(
                {
                    source
                    for source, _ in after.incoming(entity, TermRef(name))
                    if (current := after.get(source)) is not None
                    and current.record_status == "active"
                },
                key=str,
            )
            if not sources:
                continue
            count = len(sources)
            checker.add(
                "data.referential.target_referenced",
                revision,
                f"incoming.{name}",
                f"{count} active {'entity points' if count == 1 else 'entities point'} here"
                f" over {name}; relink or retract {'it' if count == 1 else 'them'} in the"
                " same change set",
                severity=_target_severity(relation),
                actual={"count": count, "sources": [str(s) for s in sources[:_MAX_SOURCES]]},
                names=sources,
            )


def _deprecated(
    checker: _Checker,
    previous: Revision | None,
    revision: Revision,
    cls: ClassDef,
    new_edges: frozenset[EdgeId],
) -> None:
    """Deprecated terms the write puts to use: a class created, values set, edges added."""
    model = checker.model

    def warn(path: str, term: str, doc: TermDoc) -> None:
        checker.add(
            "data.structural.deprecated_term",
            revision,
            path,
            f"{term} is deprecated: {doc.summary}",
            severity=Severity.WARNING,
            actual=term,
        )

    if previous is None and cls.doc.deprecated:
        warn("type", revision.type, cls.doc)
    for name, attribute in cls.attributes.items():
        value = revision.attrs.get(name)
        if value is None or (
            previous is not None and strict_equal(previous.attrs.get(name), value)
        ):
            continue
        if attribute.doc.deprecated:
            warn(f"attrs.{name}", name, attribute.doc)
        elif (doc := _type_doc(model, attribute)) is not None and doc.deprecated:
            warn(f"attrs.{name}", attribute.type, doc)
    for edge in revision.edges:
        relation = model.relations.get(edge.rel)
        if (
            edge.edge_id in new_edges
            and relation is not None
            and relation.doc.deprecated
            and (checker.view is None or edge.rel in checker.view.model.relations)
        ):
            warn(f"edges.{edge.rel}", edge.rel, relation.doc)


def _type_doc(model: CompiledModel, attribute: AttributeDef) -> TermDoc | None:
    """The doc of an attribute's datatype, enum or lifecycle; None for a base type."""
    found: DatatypeDef | EnumDef | LifecycleDef | None = None
    if attribute.kind == "datatype":
        found = model.datatypes.get(attribute.type)
    elif attribute.kind == "enum":
        found = model.enums.get(attribute.type)
    elif attribute.kind == "lifecycle":
        found = model.lifecycles.get(attribute.type)
    return None if found is None else found.doc


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


def _judge_effect(
    checker: _Checker, before: Snapshot, effect: OpEffect, revision: Revision, cls: ClassDef
) -> None:
    """Names an op carries are judged even when the post-state no longer shows them."""
    model = checker.model
    for name in effect.array_texts if effect.applied else ():
        checker.add(
            "data.structural.cardinality_many",
            revision,
            f"attrs.{name}",
            f"the text given for {name!r} is a JSON array and would be stored as one value;"
            " give the array as JSON (--set-json, set) or each value on its own (--add, add_text)",
        )
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
            elif attribute is not None and name in effect.lists and not attribute.many:
                checker.add(
                    "data.structural.cardinality_many",
                    revision,
                    f"attrs.{name}",
                    f"add and remove need a list attribute (many: true); {name!r} holds one value",
                )
    elif effect.op == "transition":
        _judge_transition(checker, effect, revision, cls)
    elif effect.op == "unlink":
        _judge_unlink(checker, before, effect, revision)


def _judge_unlink(
    checker: _Checker, before: Snapshot, effect: OpEffect, revision: Revision
) -> None:
    if effect.edge is not None:
        previous = before.get(effect.entity)
        if previous is None or all(e.edge_id != effect.edge.edge_id for e in previous.edges):
            checker.edge(revision, effect.edge, new=True)  # added and removed in this write
        return
    if effect.problem is None:
        return
    path = "edges" if effect.rel is None else f"edges.{effect.rel}"
    shown = checker.answer_model  # what the caller may be told about the relation
    relation = None if effect.rel is None else shown.relations.get(effect.rel)
    if effect.rel is not None and relation is None:
        checker.add(
            UNKNOWN_RELATION,
            revision,
            path,
            f"relation {effect.rel} does not exist",
            expected=_close(effect.rel, shown.relations),
        )
    elif relation is not None and not shown.is_a_any(revision.type, relation.domain):
        checker.add(
            "data.referential.domain_violation",
            revision,
            path,
            f"{effect.rel} does not apply to {revision.type}",
            expected=list(relation.domain),
        )
    else:
        checker.add(
            "data.referential.edge_missing", revision, path, f"unlink found {effect.problem}"
        )


def _judge_transition(
    checker: _Checker, effect: OpEffect, revision: Revision, cls: ClassDef
) -> None:
    path = "attrs" if effect.attr is None else f"attrs.{effect.attr}"
    if effect.attr is not None and effect.attr not in cls.attributes:
        checker.add(
            "data.structural.unknown_attribute",
            revision,
            path,
            f"{revision.type} has no attribute {effect.attr!r}",
            expected=_close(effect.attr, cls.attributes),
        )
        return
    if effect.problem is not None:
        checker.add("data.lifecycle.transition_not_allowed", revision, path, effect.problem)
        return
    if not effect.applied or effect.attr is None:
        return
    lifecycle_ref = cls.attributes[effect.attr].type
    lifecycle = checker.model.lifecycles[lifecycle_ref]
    allowed = [t for t in lifecycle.transitions if t.from_state == effect.from_state]
    match = next((t for t in allowed if t.to_state == effect.to_state), None)
    if match is None:
        checker.add(
            "data.lifecycle.transition_not_allowed",
            revision,
            path,
            f"no transition from {effect.from_state!r} to {effect.to_state!r}"
            if effect.to_state in lifecycle.state_names()
            else f"{effect.to_state!r} is not a state of {lifecycle_ref}",
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
                conceal=checker.guard_hidden(guard.id),
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
                names=(owner,),
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
        for label in _names(model, revision):
            matches = checker.same_names(label, cls)
            if matches and str(revision.id) < min(str(o.revision.id) for o, _ in matches):
                checker.duplicate_name(label, matches)
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
