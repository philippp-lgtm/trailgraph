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
- ``--for USE`` / ``--for-source RELATION``: the candidate must be able to fill a slot.
  The caller resolves the use to the classes that fit (``fit_classes`` for a relation:
  the range for ``--for``, the domain for ``--for-source``, each with its subclasses;
  a function ``ref`` parameter gives its class) and passes them as ``LookupSpec.fit``.
  The query still covers every label-bearing class of ``--type``; the plan only says
  which classes fit (``fits``), so the service can count the matches that do not.
  Fit classes that no label-bearing class of the ``--type`` scope is in are
  ``usage.invalid_argument`` (``--for``), with the fit in ``details.types``.
- ``--match exact|prefix``: the whole normalized value, or its start.
- ``--ignore-accents``: also match with combining marks dropped; such
  matches rank after unfolded ones.
- Language-tagged labels (``lang_string``) match by their text; the match
  names its tag (``match.lang``, null for a plain string). ``--lang R``
  keeps only matches in a language the range selects (RFC 4647 basic
  filtering); a plain string counts as the graph language, and with no
  graph language it is kept. The shown main label (``label_text``) is the
  one in ``--lang``, else the graph language, else the matched value's
  language, else the smallest tag.

Order: exact before prefix, unfolded before folded, main label before
alternative before hidden label, shorter matched value, normalized main
label (missing last), id. A hidden label is never shown: its match value
is null.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.langtag import language_matches, normalize_language_tag, tagged_parts
from graph_cli.kernel.query_ast import Cmp, Or, Pred, Query
from graph_cli.kernel.records import RECORD_STATUSES, Revision
from graph_cli.kernel.text import normalize_label
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import LABEL_ROLES
from graph_cli.query.plan import DEFAULT_LIMIT, check_paging, resolve_types

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
    lang: str | None = None  # a language range
    fit: tuple[str, ...] | None = None  # classes allowed by the use; None = any class


@dataclass(frozen=True, slots=True)
class LookupPlan:
    query: Query  # every candidate; the service pages after ranking
    key: str
    folded_key: str | None  # set with ignore_accents
    match: MatchKind
    labels: Mapping[str, tuple[tuple[str, str], ...]]  # class -> (attribute, role)
    limit: int
    offset: int
    lang: str | None = None  # the --lang range
    language: str | None = None  # the graph language
    fit: frozenset[str] | None = None  # classes allowed by the use; None = any class

    def fits(self, type_: str) -> bool:
        return self.fit is None or type_ in self.fit

    def in_language(self, tag: str | None) -> bool:
        return in_language(self.lang, tag, self.language)


@dataclass(frozen=True, slots=True)
class LabelMatch:
    attr: str
    role: str
    kind: MatchKind
    folded: bool
    value: str  # the text as stored
    normalized: str
    lang: str | None = None  # the tag of a language-tagged value

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
            "lang": self.lang,
        }


def _usage(argument: str, message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, **details},
    )


def fit_classes(
    model: CompiledModel, relation: str, side: Literal["source", "target"]
) -> tuple[str, ...] | None:
    """The classes that can take ``side`` of ``relation`` (CURIE): its domain for the source,
    its range for the target, each with its subclasses; sorted. None: any class (an empty
    domain or range), so nothing is filtered."""
    declared = model.relations[relation]
    listed = declared.domain if side == "source" else declared.range
    if not listed:
        return None
    return tuple(sorted({c for name in listed for c in model.subtypes(name)}))


def check_language_range(language_range: str | None) -> None:
    valid = language_range in (None, "*") or normalize_language_tag(str(language_range))
    if not valid:
        raise _usage("--lang", "expected a language range such as de or *", actual=language_range)


def in_language(language_range: str | None, tag: str | None, graph_language: str | None) -> bool:
    """A value with ``tag`` (None: a plain string, in the graph language) is in the range."""
    tag = tag or graph_language
    return language_range is None or tag is None or language_matches(tag, language_range)


def text_values(value: JsonValue) -> list[tuple[str, str | None]]:
    """``(text, tag)`` of each plain or language-tagged string in a value or list."""
    items = value if isinstance(value, list) else [value]
    found: list[tuple[str, str | None]] = []
    for item in items:
        if isinstance(item, str):
            found.append((item, None))
        elif (parts := tagged_parts(item)) is not None:
            found.append(parts)
    return found


def label_text(value: JsonValue, preferred: Sequence[str | None]) -> str | None:
    """The text of a main label: a plain string, or the tagged value in the first
    preferred language range that has one, else the one with the smallest tag."""
    found = sorted(text_values(value), key=lambda pair: (pair[1] or "", pair[0]))
    if not found:
        return None
    for language_range in preferred:
        for text, tag in found:
            if (
                language_range is not None
                and tag is not None
                and language_matches(tag, language_range)
            ):
                return text
    return found[0][0]


def plan_lookup(model: CompiledModel, spec: LookupSpec, language: str | None = None) -> LookupPlan:
    """``language`` is the graph language (``--lang`` treats plain strings as in it)."""
    check_paging(spec.limit, spec.offset)
    check_language_range(spec.lang)
    if spec.match not in MATCH_KINDS:
        raise _usage("--match", "expected exact or prefix", actual=spec.match)
    key = normalize_label(spec.text)
    if not key:
        raise _usage("text", "the text is blank", actual=spec.text)
    types = resolve_types(model, spec.types)
    families = {t: model.subtypes(t) for t in types} or {"": tuple(sorted(model.classes))}
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
    fit = None if spec.fit is None else frozenset(spec.fit)
    if fit is not None and not fit & set(labels):
        raise _usage(
            "--for",
            "no type fits both the use and --type"
            if types
            else "no label-bearing type fits the use",
            types=sorted(fit),
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
        lang=spec.lang,
        language=language,
        fit=fit,
    )


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
        for value, tag in text_values(revision.attrs.get(attr)):
            if not plan.in_language(tag):
                continue
            keys = [(plan.key, False)]
            if plan.folded_key is not None:
                keys.append((plan.folded_key, True))
            for key, folded in keys:
                normalized = normalize_label(value, fold_accents=folded)
                kind = _compare(plan, key, normalized)
                if kind is None:
                    continue
                found = LabelMatch(attr, role, kind, folded, value, normalized, tag)
                if best is None or found.rank() < best.rank():
                    best = found
    return best


def main_label(plan: LookupPlan, revision: Revision, match: LabelMatch | None = None) -> str | None:
    """The main label to show, in the language ``label_text`` prefers."""
    preferred = (plan.lang, plan.language, None if match is None else match.lang)
    for attr, role in plan.labels.get(revision.type, ()):
        if role == "label":
            return label_text(revision.attrs.get(attr), preferred)
    return None


def rank_key(
    plan: LookupPlan, revision: Revision, match: LabelMatch
) -> tuple[int, bool, int, int, bool, str, str]:
    label = main_label(plan, revision, match)
    kind, folded, role, length, _ = match.rank()
    normalized = normalize_label(label) if label is not None else ""
    return (kind, folded, role, length, label is None, normalized, str(revision.id))
