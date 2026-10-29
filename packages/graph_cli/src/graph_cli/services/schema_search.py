"""``graph describe search``: find terms of the compiled model by text. Reads
the model only, never graph data.

Searched: types, relations, attributes (once, under the class that declares them), enum values
and lifecycle states, by local name, CURIE, summary and description, and the named functions
of the graph (``functions``, when the caller may see them), by name, summary and, in tier 5,
what they are for. Matching works on
``kernel.text.search_key`` like ``graph search``: case, accents and punctuation do not
matter; a name is also read as words (``CostCenter`` and ``reports_to`` have two).

Order, best first (``rank``): by how the text matches
  0. the whole name, or (text with a ``:``) the whole CURIE, equals the text (spaces
     ignored in names),
  1. a name or such a CURIE starts with the text,
  2. every word of the text starts a word of the name,
  3. the name contains the text,
  4. the summary contains the text (as a phrase or every word of it),
  5. the description does (a function: its ``use_when`` text, example questions or parameter
     names, field ``use_when``; or the local name, inverse label or summary of a type or
     relation its query touches, field ``touches``),
then shorter names (CURIEs for a CURIE match), then the kind (type, relation, attribute,
enum value, state) and the CURIE.
A hit names the field and the way it matched. The ``curie`` of a function hit is its name.
The CURIE of an attribute, enum value or state is
its owner's CURIE, a dot and its name (``ppl:Person.email``); ``of`` names the owner.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.text import search_key
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.plan import DEFAULT_LIMIT, check_paging
from graph_cli.query.search import MIN_KEY_CHARS
from graph_cli.services.describe import ModuleIndex, model_envelope

SEARCH_TERM_KINDS: Final = ("type", "relation", "attribute", "enum_value", "state", "function")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class _Term:
    kind: str
    curie: str
    name: str
    owner: str | None  # the term an attribute, enum value or state belongs to
    module: str
    summary: str | None
    description: str | None
    deprecated: bool
    deeper: tuple[tuple[str, str], ...] = ()  # (field, text) pairs that only match in tier 5


@dataclass(frozen=True, slots=True)
class _Query:
    key: str
    compact: str
    words: tuple[str, ...]
    curie: bool  # the text has a ``:``, so it can name a CURIE (a bare word never matches one)


def _terms(model: CompiledModel, index: ModuleIndex) -> list[_Term]:
    found: list[_Term] = []
    for curie, cls in model.classes.items():
        found.append(
            _Term(
                "type",
                curie,
                curie.partition(":")[2],
                None,
                index.owner(curie),
                cls.doc.summary,
                cls.doc.description,
                cls.doc.deprecated,
            )
        )
        for name, a in cls.attributes.items():
            if a.declared_in == curie:
                found.append(
                    _Term(
                        "attribute",
                        f"{curie}.{name}",
                        name,
                        curie,
                        index.owner(curie),
                        a.doc.summary,
                        a.doc.description,
                        a.doc.deprecated,
                    )
                )
    for curie, r in model.relations.items():
        found.append(
            _Term(
                "relation",
                curie,
                curie.partition(":")[2],
                None,
                index.owner(curie),
                r.doc.summary,
                r.doc.description,
                r.doc.deprecated,
            )
        )
    named = [("enum_value", c, e.values, e.doc.deprecated) for c, e in model.enums.items()]
    named += [("state", c, lc.states, lc.doc.deprecated) for c, lc in model.lifecycles.items()]
    for kind, curie, values, deprecated in named:
        for v in values:
            found.append(
                _Term(
                    kind,
                    f"{curie}.{v.name}",
                    v.name,
                    curie,
                    index.owner(curie),
                    v.summary,
                    None,
                    deprecated,
                )
            )
    return found


def _function_terms(
    functions: Sequence[FunctionEntry], model: CompiledModel, index: ModuleIndex
) -> list[_Term]:
    """A function reads as its name and summary; what it is for and the terms its query
    touches (the ontology enriches the function's own text) match in tier 5."""
    found: list[_Term] = []
    for f in functions:
        deeper = [("use_when", f.defn.use_when)]
        deeper += [("use_when", e.question) for e in f.defn.examples]
        deeper += [("use_when", p.name) for p in f.defn.params]
        for t in f.types:
            if t.curie in model.classes:
                deeper.append(("touches", _CAMEL.sub(" ", t.curie.partition(":")[2])))
                deeper.append(("touches", model.classes[t.curie].doc.summary or ""))
        for t in f.relations:
            if t.curie in model.relations:
                r = model.relations[t.curie]
                deeper.append(("touches", _CAMEL.sub(" ", t.curie.partition(":")[2])))
                deeper.append(("touches", r.inverse_label or ""))
                deeper.append(("touches", r.doc.summary or ""))
        found.append(
            _Term(
                "function",
                f.name,
                f.name,
                None,
                f.module or "",
                f.defn.summary,
                None,
                f.deprecated,
                tuple(deeper),
            )
        )
    return found


def _name_key(name: str) -> str:
    return search_key(_CAMEL.sub(" ", name))


def _contains(query: _Query, text: str | None) -> str | None:
    """``phrase`` or ``words`` when the text holds the query."""
    if not text:
        return None
    key = search_key(text)
    if query.key in key:
        return "phrase"
    if len(query.words) > 1 and all(w in key for w in query.words):
        return "words"
    return None


def _match(query: _Query, term: _Term) -> tuple[int, str, str] | None:
    """``(tier, field, how)`` of the best way the term matches, or None."""
    name_key = _name_key(term.name)
    compact = name_key.replace(" ", "")
    curie_key = search_key(term.curie)
    if compact == query.compact:
        return 0, "name", "exact"
    if query.curie and curie_key == query.key:
        return 0, "curie", "exact"
    if compact.startswith(query.compact):
        return 1, "name", "prefix"
    if query.curie and curie_key.startswith(query.key):
        return 1, "curie", "prefix"
    words = name_key.split()
    if all(any(w.startswith(q) for w in words) for q in query.words):
        return 2, "name", "word"
    if query.compact in compact:
        return 3, "name", "contains"
    if query.curie:
        return None  # text with a ``:`` names a CURIE; prose would only match by accident
    how = _contains(query, term.summary)
    if how:
        return 4, "summary", how
    how = _contains(query, term.description)
    if how:
        return 5, "description", how
    for field, text in term.deeper:
        how = _contains(query, text)
        if how:
            return 5, field, how
    return None


def search_model(
    entry: GraphEntry,
    model: CompiledModel,
    text: str,
    *,
    kinds: tuple[str, ...] = (),
    module: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    functions: Sequence[FunctionEntry] | None = None,
) -> tuple[dict[str, JsonValue], int]:
    """One page of the hits and the total number of hits. ``kinds`` (any of
    ``SEARCH_TERM_KINDS``; none means all) and ``module`` (id or prefix) narrow the search.
    ``functions`` are the ones the caller may see (``None``: none)."""
    check_paging(limit, offset)
    key = search_key(text)
    if len(key.replace(" ", "")) < MIN_KEY_CHARS:
        raise GraphCliError(
            "usage.invalid_argument",
            f"text: needs at least {MIN_KEY_CHARS} letters or digits",
            details={"argument": "text", "actual": text},
        )
    for kind in kinds:
        if kind not in SEARCH_TERM_KINDS:
            raise GraphCliError(
                "usage.invalid_argument",
                f"--kind: expected one of {', '.join(SEARCH_TERM_KINDS)}",
                details={"argument": "--kind", "actual": kind},
            )
    index = ModuleIndex(model)
    wanted = index.filter(module)
    query = _Query(key, key.replace(" ", ""), tuple(key.split()), ":" in text)
    hits: list[tuple[tuple[int, int, int, str], _Term, str, str]] = []
    for term in [*_terms(model, index), *_function_terms(functions or (), model, index)]:
        if (kinds and term.kind not in kinds) or (wanted and term.module != wanted):
            continue
        found = _match(query, term)
        if found is not None:
            tier, field, how = found
            shown = search_key(term.curie) if field == "curie" else _name_key(term.name)
            rank = (
                tier,
                len(shown.replace(" ", "")),
                SEARCH_TERM_KINDS.index(term.kind),
                term.curie,
            )
            hits.append((rank, term, field, how))
    hits.sort(key=lambda hit: hit[0])
    items: list[JsonValue] = [
        {
            "kind": term.kind,
            "curie": term.curie,
            "of": term.owner,
            "module": term.module,
            "summary": term.summary,
            "deprecated": term.deprecated,
            "match": {"field": field, "how": how},
        }
        for _, term, field, how in hits[offset : offset + limit]
    ]
    body = model_envelope(
        entry,
        model,
        **({"module": wanted} if wanted is not None else {}),
        text=text,
        items=items,
    )
    return body, len(hits)
