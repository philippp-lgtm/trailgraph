"""Reference semantics of ``Query`` and ``NeighborQuery`` (contracts B13.5, B13.13).

Every backend must return exactly what this module returns for the same
state. It lives in the kernel because storage adapters and the write
pipeline's working state (``temporal_knowledge``) both evaluate queries and
are sibling layers. The rules:

- **Fields.** A reserved system field (B7.8) reads the revision; any other
  name reads ``attrs``. A JSON ``null`` counts as missing.
- **Missing values** match no comparison, only ``exists: false`` and
  ``Not(...)``; ``ne`` needs a present value too.
- **Type-strict comparisons.** ``eq``/``ne``/``in`` compare JSON values
  structurally (``true`` is not ``1``). ``lt``/``le``/``gt``/``ge`` compare
  integers with integers and strings with strings (code point order); other
  pairs never match. String operations are case-sensitive.
- **``numeric``** (set by the planner for decimal attributes) compares
  decimal strings and integers as ``Decimal``; unparseable values never
  match and sort as missing.
- **``contains``**: substring for strings, element membership for arrays.
  **``prefix``**: strings only.
- **``tagged``** (set by the planner for ``lang_string`` and ``lang_text``
  attributes): a language-tagged value ``{lang, value}`` compares by its
  text with a plain string operand (any language) and structurally with a
  tagged one (text and tag); ``contains`` on one value, ``prefix`` and the
  ordering operators use the text; ``contains`` on a list is membership by
  the same equality. **``lang``** takes a language range and matches a
  value (or any list element) whose tag it selects (RFC 4647 basic
  filtering, ``*`` for any tag).
- **Order.** Keys in order, then ``id`` ascending. Missing values sort last
  in both directions; mixed types sort booleans, numbers, strings, arrays,
  objects; a ``tagged`` key orders by text, then tag. ``total`` is exact;
  paging is ``offset``/``limit`` over that order.
- **``fields``** is a rendering hint for services; whole revisions are
  returned so ``content_hash`` stays true.
- **Neighbors** walk breadth-first up to ``depth`` hops; each entity is
  reported once, at its smallest depth, reached by the smallest
  ``(neighbor id, edge id)`` candidate; the start is never reported.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from typing import Any, Final, Protocol

from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.langtag import language_matches, tagged_parts, text_of
from graph_cli.kernel.query_ast import (
    And,
    Cmp,
    NeighborQuery,
    Not,
    Or,
    OrderKey,
    Pred,
    Query,
    QueryPage,
    RelConstraint,
)
from graph_cli.kernel.records import EdgeRecord, Revision

_ORDERING_OPS: Final = frozenset({"lt", "le", "gt", "ge"})
_NUMERIC_OPS: Final = _ORDERING_OPS | {"eq", "ne", "in"}
_ALL_OPS: Final = _NUMERIC_OPS | {"contains", "prefix", "exists", "lang"}


class Universe(Protocol):
    """The state a query runs against (a replay view at one seq)."""

    def current(self, entity: EntityId) -> Revision | None: ...

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterable[Revision]:
        """Current revisions in id order; non-empty ``types`` is a hint that callers only
        want those types, so a backend may skip decoding the rest. Callers still filter."""
        ...

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]: ...


def _invalid(message: str) -> GraphCliError:
    return GraphCliError("internal.error", f"invalid query: {message}")


def field_value(revision: Revision, name: str) -> JsonValue:
    """The value of a system field or attribute; ``None`` when missing."""
    match name:
        case "id":
            return str(revision.id)
        case "type":
            return revision.type
        case "rev":
            return revision.rev
        case "record_status":
            return revision.record_status
        case "alias":
            return revision.alias
        case "created_at":
            return format_timestamp(revision.created_at)
        case "recorded_at":
            return format_timestamp(revision.recorded_at)
        case "tx_id":
            return str(revision.tx_id)
        case "tx_seq":
            return revision.tx_seq
        case "valid_from":
            return revision.valid_from
        case "valid_to":
            return revision.valid_to
        case "content_hash":
            return revision.content_hash
        case "ontology_version":
            return revision.ontology_version
    return revision.attrs.get(name)


def _decimal(value: JsonValue) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def strict_equal(a: JsonValue, b: JsonValue) -> bool:
    """Structural JSON equality where booleans never equal integers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, Mapping) or isinstance(b, Mapping):
        if not (isinstance(a, Mapping) and isinstance(b, Mapping)) or a.keys() != b.keys():
            return False
        return all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) or isinstance(b, list | tuple):
        if not (isinstance(a, list | tuple) and isinstance(b, list | tuple)) or len(a) != len(b):
            return False
        return all(strict_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def values_equal(a: JsonValue, b: JsonValue, numeric: bool = False) -> bool:
    """``eq`` semantics; ``numeric`` compares decimal strings and integers as numbers."""
    if numeric:
        left, right = _decimal(a), _decimal(b)
        return left is not None and right is not None and left == right
    return strict_equal(a, b)


def order_values(a: JsonValue, b: JsonValue, numeric: bool = False) -> int | None:
    """-1/0/1 for comparable values, ``None`` when the pair has no order."""
    left: object
    right: object
    if numeric:
        left, right = _decimal(a), _decimal(b)
        if left is None or right is None:
            return None
    elif isinstance(a, bool) or isinstance(b, bool):
        return None
    elif (isinstance(a, int) and isinstance(b, int)) or (isinstance(a, str) and isinstance(b, str)):
        left, right = a, b
    else:
        return None
    return (left > right) - (left < right)  # type: ignore[operator]


def _check_cmp(cmp: Cmp) -> None:
    if cmp.op not in _ALL_OPS:
        raise _invalid(f"unknown operator {cmp.op!r}")
    if cmp.numeric and cmp.op not in _NUMERIC_OPS:
        raise _invalid(f"numeric does not apply to {cmp.op!r}")
    if cmp.op == "in" and not isinstance(cmp.value, list | tuple):
        raise _invalid("'in' needs an array value")
    if cmp.op in ("prefix", "lang") and not isinstance(cmp.value, str):
        raise _invalid(f"{cmp.op!r} needs a string value")
    if cmp.numeric and cmp.tagged:
        raise _invalid("numeric and tagged exclude each other")
    if cmp.op == "exists" and cmp.value is not None and not isinstance(cmp.value, bool):
        raise _invalid("'exists' takes true, false or no value")


def _match_cmp(revision: Revision, cmp: Cmp) -> bool:
    _check_cmp(cmp)
    value = field_value(revision, cmp.field)
    if cmp.op == "exists":
        return (value is not None) == (cmp.value is not False)
    if value is None:
        return False
    if cmp.op == "lang":
        items = value if isinstance(value, list | tuple) else [value]
        return any(
            (parts := tagged_parts(item)) is not None and language_matches(parts[1], str(cmp.value))
            for item in items
        )
    if cmp.tagged:
        return _match_tagged(value, cmp)
    match cmp.op:
        case "eq":
            return values_equal(value, cmp.value, cmp.numeric)
        case "ne":
            return not values_equal(value, cmp.value, cmp.numeric)
        case "in":
            assert isinstance(cmp.value, list | tuple)
            return any(values_equal(value, item, cmp.numeric) for item in cmp.value)
        case "contains":
            if isinstance(value, str):
                return isinstance(cmp.value, str) and cmp.value in value
            if isinstance(value, list | tuple):
                return any(strict_equal(item, cmp.value) for item in value)
            return False
        case "prefix":
            return isinstance(value, str) and value.startswith(str(cmp.value))
    result = order_values(value, cmp.value, cmp.numeric)
    if result is None:
        return False
    return {"lt": result < 0, "le": result <= 0, "gt": result > 0, "ge": result >= 0}[cmp.op]


def tagged_equal(value: JsonValue, operand: JsonValue) -> bool:
    """``eq`` for a language-tagged value: by text with a plain string operand,
    structurally (text and tag) otherwise."""
    if isinstance(operand, str):
        return text_of(value) == operand
    return strict_equal(value, operand)


def _match_tagged(value: JsonValue, cmp: Cmp) -> bool:
    if isinstance(value, list | tuple):
        if cmp.op == "contains":
            return any(tagged_equal(item, cmp.value) for item in value)
        return cmp.op == "ne" and not strict_equal(value, cmp.value)
    match cmp.op:
        case "eq":
            return tagged_equal(value, cmp.value)
        case "ne":
            return not tagged_equal(value, cmp.value)
        case "in":
            assert isinstance(cmp.value, list | tuple)
            return any(tagged_equal(value, item) for item in cmp.value)
    text, operand = text_of(value), text_of(cmp.value)
    if text is None or operand is None:
        return False
    match cmp.op:
        case "contains":
            return operand in text
        case "prefix":
            return text.startswith(operand)
    result = order_values(text, operand)
    if result is None:
        return False
    return {"lt": result < 0, "le": result <= 0, "gt": result > 0, "ge": result >= 0}[cmp.op]


def matches(revision: Revision, pred: Pred) -> bool:
    match pred:
        case Cmp():
            return _match_cmp(revision, pred)
        case And(items):
            return all(matches(revision, item) for item in items)
        case Or(items):
            return any(matches(revision, item) for item in items)
        case Not(item):
            return not matches(revision, item)
    raise _invalid(f"unknown predicate {type(pred).__name__}")


def _match_rel(universe: Universe, revision: Revision, rel: RelConstraint) -> bool:
    if rel.direction == "out":
        found = any(
            edge.rel == rel.rel and (not rel.targets or edge.target in rel.targets)
            for edge in revision.edges
        )
    elif rel.direction == "in":
        found = any(
            not rel.targets or source in rel.targets
            for source, _ in universe.incoming(revision.id, rel.rel)
        )
    else:
        raise _invalid(f"unknown direction {rel.direction!r}")
    return found == rel.exists


_SortKey = tuple[int, object] | None
_Row = tuple[Revision, list[_SortKey]]
_TYPE_RANK: Final = {bool: 0, int: 1, Decimal: 1, str: 2, list: 3, tuple: 3}


def _sort_value(value: JsonValue, numeric: bool, tagged: bool = False) -> _SortKey:
    """A totally ordered key for one present value; ``None`` sorts as missing."""
    if value is None:
        return None
    if tagged and (parts := tagged_parts(value)) is not None:
        return 2, f"{parts[0]}\x00{parts[1]}"  # text, then tag
    if numeric:
        number = _decimal(value)
        return None if number is None else (1, number)
    if isinstance(value, Mapping):
        return 4, canonical_bytes(value)
    if isinstance(value, list | tuple):
        return 3, canonical_bytes(value)
    return _TYPE_RANK[type(value)], value


def _row_key(keys: Sequence[OrderKey]) -> Callable[[_Row], Any]:
    def compare(a: _Row, b: _Row) -> int:
        for key, left, right in zip(keys, a[1], b[1], strict=True):
            if left is None or right is None:
                if left is None and right is None:
                    continue
                return 1 if left is None else -1  # missing last in both directions
            result = (left > right) - (left < right)
            if result:
                return -result if key.descending else result
        return (a[0].id > b[0].id) - (a[0].id < b[0].id)

    return cmp_to_key(compare)


def run_query(universe: Universe, query: Query) -> QueryPage:
    if query.as_of is not None:
        raise _invalid("as_of must be None at the port (B8.8)")
    types = frozenset(query.types)
    found = []
    for revision in universe.current_revisions(types):
        if types and revision.type not in types:
            continue
        if revision.record_status not in query.record_status:
            continue
        if query.changed_since_seq is not None and revision.tx_seq <= query.changed_since_seq:
            continue
        if query.where is not None and not matches(revision, query.where):
            continue
        if not all(_match_rel(universe, revision, rel) for rel in query.rels):
            continue
        found.append(revision)
    rows = [
        (
            revision,
            [_sort_value(field_value(revision, k.field), k.numeric, k.tagged) for k in query.order],
        )
        for revision in found
    ]
    rows.sort(key=_row_key(query.order))
    page = tuple(row[0] for row in rows[query.offset : query.offset + query.limit])
    return QueryPage(items=page, total=len(rows), limit=query.limit, offset=query.offset)


def run_neighbors(
    universe: Universe, query: NeighborQuery
) -> list[tuple[EntityId, EdgeRecord, Revision]]:
    if universe.current(query.start) is None:
        return []
    rels = None if query.rels is None else frozenset(query.rels)
    visited = {query.start}
    frontier = [query.start]
    result: list[tuple[EntityId, EdgeRecord, Revision]] = []
    for _ in range(query.depth):
        candidates: list[tuple[EntityId, EdgeRecord]] = []
        for node in frontier:
            revision = universe.current(node)
            assert revision is not None
            if query.direction in ("out", "both"):
                candidates += [
                    (edge.target, edge)
                    for edge in revision.edges
                    if rels is None or edge.rel in rels
                ]
            if query.direction in ("in", "both"):
                candidates += [
                    (source, edge)
                    for source, edge in universe.incoming(node)
                    if rels is None or edge.rel in rels
                ]
        level: list[tuple[EntityId, EdgeRecord, Revision]] = []
        for neighbor, edge in sorted(candidates, key=lambda c: (c[0], c[1].edge_id)):
            if neighbor in visited:
                continue
            target = universe.current(neighbor)
            if target is None:
                continue
            visited.add(neighbor)
            level.append((neighbor, edge, target))
        result += level
        frontier = [neighbor for neighbor, _, _ in level]
        if not frontier:
            break
    return result
