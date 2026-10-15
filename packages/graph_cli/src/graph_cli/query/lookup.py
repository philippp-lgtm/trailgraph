"""Label lookup: find entities by name and rank the candidates (B13.13).

Labels are not identifiers, so a lookup returns candidates, never a
resolved ref. The match runs on normalized text (``kernel.text``) after
one store query, because no backend has to know Unicode normalization: the
planned query selects the label-bearing classes, an ``exists`` on any label
attribute and the record status, without paging. ``match_revision`` and
``rank_key`` are pure.

- ``--type T`` (repeatable): a class and its subclasses, as in ``find``.
  Classes without label roles are skipped; a named family without any is
  ``term.no_labels``, and so is a model without label roles.
- ``--match exact|prefix``: the whole normalized value, or its start.
- ``--ignore-accents``: also match with combining marks dropped; such
  matches rank after unfolded ones.

Order: exact before prefix, unfolded before folded, main label before
alternative before hidden label, shorter matched value, normalized main
label (missing last), id. A hidden label is never shown: its match value
is null.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.query_ast import Cmp, Or, Pred, Query
from graph_cli.kernel.records import RECORD_STATUSES, Revision
from graph_cli.kernel.text import normalize_label
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import LABEL_ROLES
from graph_cli.query.plan import DEFAULT_LIMIT, check_paging, check_types

MatchKind = Literal["exact", "prefix"]
MATCH_KINDS: Final[tuple[MatchKind, ...]] = ("exact", "prefix")


@dataclass(frozen=True, slots=True)
class LookupSpec:
    text: str
    types: tuple[str, ...] = ()
    match: str = "exact"
    ignore_accents: bool = False
    include_inactive: bool = False
    limit: int = DEFAULT_LIMIT
    offset: int = 0


@dataclass(frozen=True, slots=True)
class LookupPlan:
    query: Query  # every candidate; the service pages after ranking
    key: str
    folded_key: str | None  # set with ignore_accents
    match: MatchKind
    labels: Mapping[str, tuple[tuple[str, str], ...]]  # class -> (attribute, role)
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class LabelMatch:
    attr: str
    role: str
    kind: MatchKind
    folded: bool
    value: str  # as stored
    normalized: str

    def rank(self) -> tuple[int, bool, int, int, str]:
        return (
            MATCH_KINDS.index(self.kind),
            self.folded,
            LABEL_ROLES.index(self.role),
            len(self.normalized),
            self.attr,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "attr": self.attr,
            "role": self.role,
            "kind": self.kind,
            "value": None if self.role == "hidden_label" else self.value,
        }


def _usage(argument: str, message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, **details},
    )


def plan_lookup(model: CompiledModel, spec: LookupSpec) -> LookupPlan:
    check_paging(spec.limit, spec.offset)
    if spec.match not in MATCH_KINDS:
        raise _usage("--match", "expected exact or prefix", actual=spec.match)
    key = normalize_label(spec.text)
    if not key:
        raise _usage("text", "the text is blank", actual=spec.text)
    check_types(model, spec.types)
    families = {t: model.subtypes(t) for t in spec.types} or {"": tuple(sorted(model.classes))}
    labels = {
        c: found
        for family in families.values()
        for c in family
        if (found := model.label_attributes(c))
    }
    bare = sorted(t for t, family in families.items() if t and not set(family) & set(labels))
    if bare or not labels:
        raise GraphCliError(
            "term.no_labels",
            f"no label attributes on {', '.join(bare)}" if bare else "the model has no labels",
            details={"types": list(bare)},
        )
    attrs = sorted({a for found in labels.values() for a, _ in found})
    where: Pred = Or(tuple(Cmp(a, "exists", True) for a in attrs))
    query = Query(
        types=tuple(TermRef(c) for c in sorted(labels)),
        where=where,
        record_status=frozenset(RECORD_STATUSES)
        if spec.include_inactive
        else frozenset({"active"}),
        limit=sys.maxsize,
    )
    folded = normalize_label(spec.text, fold_accents=True) if spec.ignore_accents else None
    return LookupPlan(
        query=query,
        key=key,
        folded_key=folded,
        match=spec.match,
        labels=labels,
        limit=spec.limit,
        offset=spec.offset,
    )


def _values(value: JsonValue) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [v for v in items if isinstance(v, str)]


def _compare(plan: LookupPlan, key: str, candidate: str) -> MatchKind | None:
    if candidate == key:
        return "exact"
    if plan.match == "prefix" and candidate.startswith(key):
        return "prefix"
    return None


def match_revision(plan: LookupPlan, revision: Revision) -> LabelMatch | None:
    """The best label match of an entity, or None."""
    best: LabelMatch | None = None
    for attr, role in plan.labels.get(revision.type, ()):
        for value in _values(revision.attrs.get(attr)):
            keys = [(plan.key, False)]
            if plan.folded_key is not None:
                keys.append((plan.folded_key, True))
            for key, folded in keys:
                normalized = normalize_label(value, fold_accents=folded)
                kind = _compare(plan, key, normalized)
                if kind is None:
                    continue
                found = LabelMatch(attr, role, kind, folded, value, normalized)
                if best is None or found.rank() < best.rank():
                    best = found
    return best


def main_label(plan: LookupPlan, revision: Revision) -> str | None:
    for attr, role in plan.labels.get(revision.type, ()):
        value = revision.attrs.get(attr)
        if role == "label" and isinstance(value, str):
            return value
    return None


def rank_key(
    plan: LookupPlan, revision: Revision, match: LabelMatch
) -> tuple[int, bool, int, int, bool, str, str]:
    label = main_label(plan, revision)
    kind, folded, role, length, _ = match.rank()
    normalized = normalize_label(label) if label is not None else ""
    return (kind, folded, role, length, label is None, normalized, str(revision.id))
