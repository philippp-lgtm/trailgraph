"""Relations that connect two types, computed from the model only.

Used to answer a domain or range violation with what would have been accepted. The
answer is small and deterministic:

- ``direct``: relations that accept the pair as ``source -rel-> target`` or as
  ``target -rel-> source`` (subclassing respected through ``is_a_any``), ordered by
  ``(len(curie), curie)``. ``from``/``to`` are the two given types in the relation's
  direction.
- ``paths``: only when ``direct`` is empty, up to ``MAX_PATHS`` two-hop paths
  ``source -r1-> X -r2-> target``, each hop in its declared direction. ``X`` is the most
  general class (fewest ancestors, then shorter CURIE, then alphabetical) that both hops
  accept. Abstract classes are never used as ``X``; a path without a concrete ``X`` is
  skipped. Paths are ordered by ``(summed CURIE length, r1, r2, X)``.
- Relations with ``[]`` as domain or range (any class), which includes the core
  provenance relations, are never listed: they are not the intended answer. Deprecated
  relations are never listed either.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.model import CompiledModel, RelationDef

MAX_PATHS: Final = 3


def connecting(model: CompiledModel, source: str, target: str) -> dict[str, JsonValue]:
    """``{"direct": [hop...], "paths": [[hop, hop]...]}`` with ``hop = {relation, from, to}``."""
    closed = sorted(
        (
            (name, rel)
            for name, rel in model.relations.items()
            if rel.domain and rel.range and not rel.doc.deprecated
        ),
        key=lambda item: (len(item[0]), item[0]),
    )
    direct: list[JsonValue] = []
    for name, rel in closed:
        if _accepts(model, rel, source, target):
            direct.append(_hop(name, source, target))
        elif source != target and _accepts(model, rel, target, source):
            direct.append(_hop(name, target, source))
    paths: list[JsonValue] = []
    if not direct:
        paths = [
            [_hop(r1, source, mid), _hop(r2, mid, target)]
            for r1, mid, r2 in _paths(model, closed, source, target)
        ]
    return {"direct": direct, "paths": paths}


def _accepts(model: CompiledModel, rel: RelationDef, source: str, target: str) -> bool:
    return model.is_a_any(source, rel.domain) and model.is_a_any(target, rel.range)


def _hop(relation: str, source: str, target: str) -> JsonValue:
    return {"relation": relation, "from": source, "to": target}


def _paths(
    model: CompiledModel,
    closed: list[tuple[str, RelationDef]],
    source: str,
    target: str,
) -> list[tuple[str, str, str]]:
    first = [(n, r) for n, r in closed if model.is_a_any(source, r.domain)]
    second = [(n, r) for n, r in closed if model.is_a_any(target, r.range)]
    mids_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], str | None] = {}
    found: list[tuple[str, str, str]] = []
    for n1, r1 in first:
        for n2, r2 in second:
            key = (r1.range, r2.domain)
            if key not in mids_cache:
                mids_cache[key] = _mid(model, r1, r2)
            mid = mids_cache[key]
            if mid is not None:
                found.append((n1, mid, n2))
    found.sort(key=lambda p: (len(p[0]) + len(p[1]) + len(p[2]), p[0], p[2], p[1]))
    return found[:MAX_PATHS]


def _mid(model: CompiledModel, r1: RelationDef, r2: RelationDef) -> str | None:
    """The most general concrete class that ``r1`` leads to and ``r2`` starts from."""
    mids = [
        c
        for c, cls in model.classes.items()
        if not cls.abstract and model.is_a_any(c, r1.range) and model.is_a_any(c, r2.domain)
    ]
    return min(mids, key=lambda c: (len(model.classes[c].ancestors), len(c), c), default=None)
