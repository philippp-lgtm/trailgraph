"""Query planning: ``find`` and ``neighbors`` arguments into the resolved Query AST (B13.13).

Storage sees only resolved queries: concrete type CURIEs (subclasses
expanded), validated fields, values of the right JSON type. The flag syntax
(DECISIONS.md "P1 item 6a" Q4) has no shell metacharacters:

- ``--type T`` (repeatable): a class and its subclasses.
- ``--where attr=value`` or ``--where attr:op=value`` with ``op`` in
  ``eq ne lt le gt ge contains prefix in exists lang``, all AND-ed. The
  field is an attribute of a selected type (of any class without
  ``--type``) or a system field. Values are coerced by the attribute's base
  type, like ``--set``; ``in`` takes a comma list, ``exists`` takes
  ``true|false``. On a many-valued attribute ``=`` means membership. On a
  language-tagged attribute (``lang_string``, ``lang_text``) an untagged
  value matches the text in any language, ``"text"@tag`` text and tag
  (``Cmp.tagged``); ``lang`` takes a language range (``de``, ``*``) and is
  allowed there only.
- ``--rel rel[=target,...]`` and ``--in-rel rel[=source,...]``: an edge to
  (from) any of the listed entities; targets are resolved by the caller.
- ``--sort field[:desc]`` (repeatable), ``--limit`` (at most 500),
  ``--offset``, ``--include-inactive``.

A type or relation is its CURIE or, as in ``graph query``, its local name
when exactly one CURIE has it (``resolve_term``); an ambiguous local name is
``query.ambiguous_term`` with the candidates. Unknown names are
``term.unknown`` with ``details.did_you_mean``; bad syntax or values are
``usage.invalid_argument``.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.langtag import normalize_language_tag
from graph_cli.kernel.query_ast import (
    MAX_NEIGHBOR_DEPTH,
    And,
    Cmp,
    CmpOp,
    NeighborQuery,
    Not,
    OrderKey,
    Pred,
    Query,
    RelConstraint,
)
from graph_cli.kernel.records import RECORD_STATUSES
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.values import coerce_text
from graph_cli.ontology_layer.vocabulary import LANG_BASES, SYSTEM_FIELDS

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 500
OPS: Final[tuple[CmpOp, ...]] = (
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "contains",
    "prefix",
    "in",
    "exists",
    "lang",
)
_SYSTEM_BASES: Final = {
    "rev": "integer",
    "tx_seq": "integer",
    "created_at": "datetime",
    "recorded_at": "datetime",
}


type Operand = str | int | bool | Decimal | list[Operand]
"""The typed operand of a ``where`` entry that a bound parameter gives (``Value`` without the
graph types)."""


@dataclass(frozen=True, slots=True)
class FindSpec:
    types: tuple[str, ...] = ()
    where: tuple[str, ...] = ()
    rels: tuple[tuple[str, tuple[EntityId, ...]], ...] = ()
    in_rels: tuple[tuple[str, tuple[EntityId, ...]], ...] = ()
    include_inactive: bool = False
    sort: tuple[str, ...] = ()
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def _usage(argument: str, message: str, **details: JsonValue) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, **details},
    )


def unknown_term(kind: str, name: str, choices: Iterable[str]) -> GraphCliError:
    found = difflib.get_close_matches(name, sorted(choices), n=5)
    return GraphCliError(
        "term.unknown",
        f"unknown {kind} {name!r}",
        details={"kind": kind, "term": name, "did_you_mean": list(found)},
    )


def split_assignment(text: str, argument: str) -> tuple[str, str]:
    """``name=value`` (the value may contain ``=``)."""
    name, sep, value = text.partition("=")
    if not sep or not name:
        raise _usage(argument, f"expected name=value, got {text!r}", actual=text)
    return name, value


def parse_rel_flag(text: str, argument: str) -> tuple[str, tuple[str, ...]]:
    """``rel`` or ``rel=target[,target...]``; targets stay text for the caller to resolve."""
    rel, sep, listed = text.partition("=")
    targets = tuple(listed.split(",")) if sep else ()
    if not rel or (sep and not all(targets)):
        raise _usage(argument, f"expected rel or rel=<id>[,<id>...], got {text!r}", actual=text)
    return rel, targets


def check_paging(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise _usage("--limit", f"expected 1..{MAX_LIMIT}", actual=limit)
    if offset < 0:
        raise _usage("--offset", "expected 0 or more", actual=offset)


def resolve_term(kind: str, name: str, known: Mapping[str, object]) -> str:
    """The CURIE ``name`` means: itself if known, else the one CURIE whose local name
    it is (``query.ambiguous_term`` for several, ``term.unknown`` for none)."""
    if name in known:
        return name
    index: dict[str, list[str]] = {}
    for curie in sorted(known):
        index.setdefault(curie.partition(":")[2], []).append(curie)
    candidates = index.get(name, [])
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        listed = ", ".join(candidates)
        raise GraphCliError(
            "query.ambiguous_term",
            f"{kind} {name!r} matches {listed}; give one of them",
            details={"kind": kind, "term": name, "candidates": candidates},
        )
    raise unknown_term(kind, name, [*known, *index])


def resolve_types(model: CompiledModel, types: Iterable[str]) -> tuple[str, ...]:
    """Each ``--type`` as the CURIE of a class (``resolve_term``)."""
    return tuple(resolve_term("type", name, model.classes) for name in types)


# Entity parts ``--fields`` may name besides attributes; ``ALWAYS_SHOWN`` stay anyway.
ENTITY_PARTS: Final = ("edges",)
ALWAYS_SHOWN: Final = ("id", "alias", "type", "rev", "record_status")


def check_fields(
    model: CompiledModel, types: Iterable[str], fields: Iterable[str]
) -> tuple[str, ...]:
    """``--fields`` values (repeatable, comma lists) as names, checked against the
    attributes of the classes in scope (every class without types), the entity
    parts and the system fields; an unknown name is ``term.unknown``."""
    names = tuple(dict.fromkeys(n.strip() for text in fields for n in text.split(",")))
    names = tuple(n for n in names if n)
    if not names:
        return ()
    classes = {c for t in resolve_types(model, types) for c in model.subtypes(t)}
    classes = classes or set(model.classes)
    known = {n for c in classes for n in model.classes[c].attributes}
    known |= set(ENTITY_PARTS) | set(SYSTEM_FIELDS)
    for name in names:
        if name not in known:
            raise unknown_term("field", name, known)
    return names


class _Planner:
    def __init__(self, model: CompiledModel, types: tuple[str, ...]) -> None:
        self.model = model
        self.types = resolve_types(model, types)
        self.classes = sorted({c for t in self.types for c in model.subtypes(t)}) or sorted(
            model.classes
        )

    def attribute(self, name: str) -> AttributeDef | None:
        for cls in self.classes:
            found = self.model.classes[cls].attributes.get(name)
            if found is not None:
                return found
        return None

    def field_names(self) -> set[str]:
        names = {n for c in self.classes for n in self.model.classes[c].attributes}
        return names | set(SYSTEM_FIELDS)

    def base_of(self, name: str, argument: str) -> tuple[str, AttributeDef | None]:
        if name in SYSTEM_FIELDS:
            return _SYSTEM_BASES.get(name, "string"), None
        attribute = self.attribute(name)
        if attribute is None:
            raise unknown_term("field", name, self.field_names())
        return self.model.value_base(attribute.kind, attribute.type), attribute

    def coerce(self, base: str, text: str, argument: str) -> JsonValue:
        try:
            return coerce_text(base, text)
        except ValueError as exc:
            raise _usage(argument, str(exc), expected=base, actual=text) from None

    def where(self, text: str) -> Pred:
        left, value = split_assignment(text, "--where")
        return self._where(left, value, typed=False)

    def where_typed(self, left: str, operand: Operand) -> Pred:
        """The entry ``attr[:op]`` with an already typed ``operand`` (a bound parameter): it is
        the whole operand, never read as flag text, so a comma in a value does not split a list
        and a value shaped like ``"x"@de`` is not a language tag."""
        return self._where(left, operand, typed=True)

    def _where(self, left: str, operand: Operand, *, typed: bool) -> Pred:
        argument = "--where"
        name, _, op_text = left.partition(":")
        op = op_text or "eq"
        if op not in OPS:
            raise _usage(argument, f"unknown operator {op!r}", expected=list(OPS), actual=op)
        base, attribute = self.base_of(name, argument)
        numeric, tagged = base == "decimal", base in LANG_BASES

        def text() -> str:
            return _operand_text(operand) if typed else str(operand)

        def scalar(item: Operand) -> JsonValue:
            if typed and tagged:
                return _operand_text(item)  # the text as it is: no tag is read from it
            return self.coerce(base, _operand_text(item) if typed else str(item), argument)

        if op == "exists":
            wanted = operand if typed and isinstance(operand, bool) else text()
            if wanted not in (True, False, "true", "false"):
                raise _usage(argument, "exists takes true or false", actual=text())
            return Cmp(name, "exists", wanted in (True, "true"))
        if op == "lang":
            value = text()
            if not tagged:
                raise _usage(argument, f"{name!r} is not language-tagged", actual=name)
            if value != "*" and normalize_language_tag(value) is None:
                raise _usage(argument, "expected a language range such as de or *", actual=value)
            return Cmp(name, "lang", value)
        if attribute is not None and attribute.many:
            if op not in ("eq", "ne", "contains"):
                raise _usage(argument, f"{name!r} has several values; use =, ne, contains or lang")
            member = Cmp(name, "contains", scalar(operand), tagged=tagged)
            return Not(member) if op == "ne" else member
        if op == "in":
            if typed and not isinstance(operand, list):
                raise _usage(argument, "in takes a list", actual=text())
            parts = operand if isinstance(operand, list) else text().split(",")
            return Cmp(name, "in", [scalar(item) for item in parts], numeric=numeric, tagged=tagged)
        if op in ("contains", "prefix"):
            return Cmp(name, op, text(), tagged=tagged)
        return Cmp(name, op, scalar(operand), numeric=numeric, tagged=tagged)

    def order(self, text: str) -> OrderKey:
        name, _, direction = text.partition(":")
        if direction not in ("", "asc", "desc"):
            raise _usage("--sort", f"expected field[:desc], got {text!r}", actual=text)
        base, _ = self.base_of(name, "--sort")
        return OrderKey(
            name,
            descending=direction == "desc",
            numeric=base == "decimal",
            tagged=base in LANG_BASES,
        )

    def relation(self, name: str) -> TermRef:
        return TermRef(resolve_term("relation", name, self.model.relations))


def _operand_text(value: Operand) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, list):
        raise _usage("--where", "a list is not a single value")
    return str(value)


type Clause = str | tuple[str, Operand]
"""A ``where`` entry: flag text (``attr[:op]=value``) or ``(attr[:op], typed operand)``."""


def plan_clauses(
    model: CompiledModel,
    types: Sequence[str],
    where: Sequence[Clause] = (),
    sort: Sequence[str] = (),
) -> tuple[Pred | None, tuple[OrderKey, ...]]:
    """``where`` entries (AND-ed) and ``sort`` keys planned against ``types`` (any class when
    empty): the one definition of the ``find`` grammar that views check at publish time (with
    placeholder operands) and run with bound parameters."""
    planner = _Planner(model, tuple(types))
    preds = [
        planner.where(entry) if isinstance(entry, str) else planner.where_typed(*entry)
        for entry in where
    ]
    found = None if not preds else preds[0] if len(preds) == 1 else And(tuple(preds))
    return found, tuple(planner.order(text) for text in sort)


def plan_find(model: CompiledModel, spec: FindSpec) -> Query:
    check_paging(spec.limit, spec.offset)
    planner = _Planner(model, spec.types)
    preds = [planner.where(text) for text in spec.where]
    rels = [RelConstraint(planner.relation(rel), "out", targets) for rel, targets in spec.rels] + [
        RelConstraint(planner.relation(rel), "in", sources) for rel, sources in spec.in_rels
    ]
    return Query(
        types=tuple(TermRef(c) for c in planner.classes) if spec.types else (),
        where=None if not preds else preds[0] if len(preds) == 1 else And(tuple(preds)),
        rels=tuple(rels),
        record_status=frozenset(RECORD_STATUSES)
        if spec.include_inactive
        else frozenset({"active"}),
        order=tuple(planner.order(text) for text in spec.sort),
        limit=spec.limit,
        offset=spec.offset,
    )


def plan_neighbors(
    model: CompiledModel,
    start: EntityId,
    *,
    rels: Sequence[str] = (),
    direction: str = "both",
    depth: int = 1,
) -> NeighborQuery:
    if direction not in ("out", "in", "both"):
        raise _usage("--direction", "expected out, in or both", actual=direction)
    if not 1 <= depth <= MAX_NEIGHBOR_DEPTH:
        raise _usage("--depth", f"expected 1..{MAX_NEIGHBOR_DEPTH}", actual=depth)
    planner = _Planner(model, ())
    checked: Literal["out", "in", "both"] = direction  # type: ignore[assignment]
    return NeighborQuery(
        start,
        tuple(planner.relation(r) for r in rels) or None,
        checked,
        depth,
    )
