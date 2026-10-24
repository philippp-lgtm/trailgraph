"""Text search: find entities whose text fields match, ranked (F51).

Search is for "is there anything about X": its answers are suggestions,
never a resolved ref (``lookup`` resolves names). Like ``lookup`` it plans
one store query without paging and matches on keys (``kernel.text``) after
it, because no backend has to know Unicode normalization; ``match_revision``
and ``rank_key`` are pure.

- Searched: every attribute whose base is ``string``, ``text``,
  ``lang_string`` or ``lang_text`` (also datatypes over them), each value
  of a many-valued one; a language-tagged value by its text, and the match
  names its tag (``match.lang``). ``--lang R`` keeps only values in a
  language the range selects, as in ``lookup``. Not enum values or
  lifecycle states (codes).
- ``--type T`` (repeatable): a class and its subclasses. ``--field A``
  (repeatable): only these attributes; a name no class in scope can
  search is ``term.unknown``. No searchable attribute in scope is
  ``term.no_text_fields``.
- Kinds, best first: ``exact`` (the whole key), ``phrase`` (the query key
  inside the value key), ``words`` (each query word inside the value key,
  any order), ``similar`` (trigram containment of at least
  ``SIMILAR_MIN``, and of at least ``WORD_MIN`` for each query word on its
  own, so a missing word never passes while a typo in a short one does;
  short text only, long ``text`` would match almost anything).

Order: the attribute's group (names, other strings, text), so a close
name comes before a phrase deep in a description; kind; the place within
the group (main, alternative, hidden label); score, highest first; shorter
value; normalized main label (missing last); id. One item per entity, its
best match; ``fields`` lists every attribute of it that matched. A hidden
label is never shown: its excerpt is null.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.query_ast import Cmp, Or, Pred, Query
from graph_cli.kernel.records import RECORD_STATUSES, Revision
from graph_cli.kernel.text import containment, normalize_label, search_key, trigrams
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import LABEL_ROLES
from graph_cli.query.lookup import check_language_range, in_language, label_text, text_values
from graph_cli.query.plan import DEFAULT_LIMIT, check_paging, resolve_types, unknown_term

SearchKind = Literal["exact", "phrase", "words", "similar"]
SEARCH_KINDS: Final[tuple[SearchKind, ...]] = ("exact", "phrase", "words", "similar")
SIMILAR_MIN: Final = 0.7
WORD_MIN: Final = 0.5
EXCERPT_CHARS: Final = 160
MIN_KEY_CHARS: Final = 2
_TEXT_BASES: Final = ("string", "text", "lang_string", "lang_text")
_LONG_BASES: Final = ("text", "lang_text")


@dataclass(frozen=True, slots=True)
class SearchSpec:
    text: str
    types: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    include_inactive: bool = False
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    lang: str | None = None  # a language range


@dataclass(frozen=True, slots=True)
class SearchField:
    attr: str
    role: str | None
    long_text: bool  # base ``text`` or ``lang_text``: no ``similar`` matches

    @property
    def group(self) -> int:
        """Names, other strings, text."""
        return 0 if self.role is not None else 1 + self.long_text

    @property
    def place(self) -> int:
        """Main, alternative, hidden label; 0 outside the names."""
        return 0 if self.role is None else LABEL_ROLES.index(self.role)


@dataclass(frozen=True, slots=True)
class SearchPlan:
    query: Query  # every candidate; the service pages after ranking
    key: str
    words: tuple[str, ...]
    trigrams: frozenset[str]
    word_trigrams: tuple[frozenset[str], ...]  # one set per word, several words only
    fields: dict[str, tuple[SearchField, ...]]  # class -> searched attributes
    labels: dict[str, str]  # class -> its main label attribute
    limit: int
    offset: int
    lang: str | None = None  # the --lang range
    language: str | None = None  # the graph language


@dataclass(frozen=True, slots=True)
class SearchMatch:
    field: SearchField
    kind: SearchKind
    score: float
    value: str  # the text as stored
    length: int  # of the value's key
    lang: str | None = None  # the tag of a language-tagged value

    def rank(self) -> tuple[int, int, int, float, int, str]:
        return (
            self.field.group,
            SEARCH_KINDS.index(self.kind),
            self.field.place,
            -self.score,
            self.length,
            self.field.attr,
        )


@dataclass(frozen=True, slots=True)
class EntityMatch:
    best: SearchMatch
    fields: tuple[str, ...]  # every matching attribute, best first

    def to_json(self, plan: SearchPlan) -> dict[str, JsonValue]:
        best = self.best
        hidden = best.field.role == "hidden_label"
        return {
            "attr": best.field.attr,
            "role": best.field.role,
            "kind": best.kind,
            "score": round(best.score * 100),  # percent: no floats in JSON
            "excerpt": None if hidden else excerpt(best.value, plan, best.kind),
            "lang": best.lang,
        }


def _usage(argument: str, message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, **details},
    )


def searchable(model: CompiledModel, class_curie: str) -> tuple[SearchField, ...]:
    """The attributes of a class that search reads, by name."""
    found = []
    for name, a in sorted(model.classes[class_curie].attributes.items()):
        if a.kind not in ("base", "datatype"):
            continue
        base = model.value_base(a.kind, a.type)
        if base in _TEXT_BASES:
            found.append(SearchField(name, a.role, base in _LONG_BASES))
    return tuple(found)


def plan_search(model: CompiledModel, spec: SearchSpec, language: str | None = None) -> SearchPlan:
    """``language`` is the graph language (``--lang`` treats plain strings as in it)."""
    check_paging(spec.limit, spec.offset)
    check_language_range(spec.lang)
    key = search_key(spec.text)
    if len(key.replace(" ", "")) < MIN_KEY_CHARS:
        raise _usage("text", f"needs at least {MIN_KEY_CHARS} letters or digits", actual=spec.text)
    types = resolve_types(model, spec.types)
    families = {t: model.subtypes(t) for t in types} or {"": tuple(sorted(model.classes))}
    every = {c: searchable(model, c) for family in families.values() for c in family}
    if spec.fields:
        known = {f.attr for found in every.values() for f in found}
        for name in spec.fields:
            if name not in known:
                raise unknown_term("text field", name, known)
        every = {c: tuple(f for f in found if f.attr in spec.fields) for c, found in every.items()}
    fields = {c: found for c, found in every.items() if found}
    bare = sorted(t for t, family in families.items() if t and not set(family) & set(fields))
    if bare or not fields:
        raise GraphCliError(
            "term.no_text_fields",
            f"no text fields on {', '.join(bare)}" if bare else "nothing to search",
            details={"types": list(bare)},
        )
    attrs = sorted({f.attr for found in fields.values() for f in found})
    where: Pred = Or(tuple(Cmp(a, "exists", True) for a in attrs))
    query = Query(
        types=tuple(TermRef(c) for c in sorted(fields)),
        where=where,
        record_status=frozenset(RECORD_STATUSES)
        if spec.include_inactive
        else frozenset({"active"}),
        limit=sys.maxsize,
    )
    return SearchPlan(
        query=query,
        key=key,
        words=tuple(key.split()),
        trigrams=trigrams(key),
        word_trigrams=tuple(trigrams(w) for w in key.split()) if " " in key else (),
        fields=fields,
        labels={
            c: attr for c in fields for attr, role in model.label_attributes(c) if role == "label"
        },
        limit=spec.limit,
        offset=spec.offset,
        lang=spec.lang,
        language=language,
    )


def _compare(plan: SearchPlan, field: SearchField, key: str) -> tuple[SearchKind, float] | None:
    if key == plan.key:
        return "exact", 1.0
    if plan.key in key:
        return "phrase", 1.0
    if len(plan.words) > 1 and all(w in key for w in plan.words):
        return "words", 1.0
    if not field.long_text:
        found = trigrams(key)
        score = containment(plan.trigrams, found)
        if score >= SIMILAR_MIN and all(
            containment(word, found) >= WORD_MIN for word in plan.word_trigrams
        ):
            return "similar", score
    return None


def match_revision(plan: SearchPlan, revision: Revision) -> EntityMatch | None:
    """The best match of an entity and every attribute that matched, or None."""
    best_per_attr: dict[str, SearchMatch] = {}
    for field in plan.fields.get(revision.type, ()):
        for value, tag in text_values(revision.attrs.get(field.attr)):
            if not in_language(plan.lang, tag, plan.language):
                continue
            key = search_key(value)
            found = _compare(plan, field, key)
            if found is None:
                continue
            match = SearchMatch(field, found[0], found[1], value, len(key), tag)
            current = best_per_attr.get(field.attr)
            if current is None or match.rank() < current.rank():
                best_per_attr[field.attr] = match
    if not best_per_attr:
        return None
    ordered = sorted(best_per_attr.values(), key=SearchMatch.rank)
    return EntityMatch(ordered[0], tuple(m.field.attr for m in ordered))


def main_label(
    plan: SearchPlan, revision: Revision, match: EntityMatch | None = None
) -> str | None:
    """The entity's main label, also when ``--field`` does not search it; a
    language-tagged one as ``lookup`` picks it."""
    attr = plan.labels.get(revision.type)
    if attr is None:
        return None
    preferred = (plan.lang, plan.language, None if match is None else match.best.lang)
    return label_text(revision.attrs.get(attr), preferred)


def rank_key(
    plan: SearchPlan, revision: Revision, match: EntityMatch
) -> tuple[int, int, int, float, int, bool, str, str]:
    label = main_label(plan, revision, match)
    group, kind, place, score, length, _ = match.best.rank()
    normalized = normalize_label(label) if label is not None else ""
    return (group, kind, place, score, length, label is None, normalized, str(revision.id))


def _flat_keys(text: str) -> tuple[str, list[int]]:
    """The search key of ``text`` built per character, with each key character's
    index in ``text``: where a hit starts, for the excerpt window."""
    chars: list[str] = []
    index: list[int] = []
    for i, c in enumerate(text):
        piece = search_key(c)
        if not piece:
            if chars and chars[-1] != " ":
                chars.append(" ")
                index.append(i)
            continue
        chars.extend(piece)
        index.extend(i for _ in piece)
    return "".join(chars), index


def excerpt(value: str, plan: SearchPlan, kind: SearchKind) -> str:
    """The value on one line, cut to about ``EXCERPT_CHARS`` around the hit."""
    flat = " ".join(value.split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    keys, index = _flat_keys(flat)
    needle = plan.key if kind in ("exact", "phrase") else plan.words[0]
    at = keys.find(needle)
    hit = index[at] if at >= 0 else 0
    start = max(0, min(hit - EXCERPT_CHARS // 3, len(flat) - EXCERPT_CHARS))
    if start > 0:
        space = flat.rfind(" ", 0, start + 1)
        start = space + 1 if space >= 0 and start - space < 20 else start
    end = start + EXCERPT_CHARS
    if end < len(flat):
        space = flat.find(" ", end - 20, end)
        end = space if space > start else end
    text = flat[start:end].strip()
    return ("…" if start > 0 else "") + text + ("…" if end < len(flat) else "")
