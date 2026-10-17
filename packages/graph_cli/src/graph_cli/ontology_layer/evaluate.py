"""Evaluator for the compiled expression AST: guards and rules (contracts B7.15).

Semantics follow the reference query evaluator (``kernel.query_eval``):

- **Operands.** ``{"attr": n}`` reads ``attrs`` (missing and JSON ``null``
  are absent), ``{"self": "record_status"}`` the system field,
  ``{"const": v}`` a constant, ``{"count": {...}}`` an integer,
  ``{"sum": {...}}`` an integer or a decimal string.
- **Comparisons** are type-strict; an absent operand matches nothing, so
  ``ne`` on an absent value is false. A comparison with an aggregate
  (``count``, ``sum``) or a ``decimal`` attribute is numeric: decimal
  strings and integers compare by value.
- **``exists``**: the operand is present. **``nonempty``**: present, and a
  non-empty string, array or object (other present values count as
  non-empty).
- **``count{rel, direction, where?}``** counts current edges: ``out`` over
  the entity's own edges, ``in`` over ``snapshot.incoming``.
  ``where.record_status`` and ``where.type`` filter the other end; ``type``
  includes subclasses; a missing other end never matches a filter.
- **``sum{rel, direction, attr, where?}``** adds ``attr`` over the same
  ends as ``count`` (one addend per edge). The sum is absent when an end is
  missing or its value is absent or not a number; the empty set sums to
  ``0``. Exact: an ``int`` when every addend is one, else a decimal string
  in fixed notation whose scale is the largest addend scale.
- **``explain``** lists the values of the aggregates and of the operands
  compared against them, for rule findings (B7.9).

The compiler emits only known nodes, so an unknown node is a bug
(``internal.error``).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from decimal import Context, Decimal, Inexact, localcontext
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue, canonical_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_eval import order_values, values_equal
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import DECIMAL_RE

_COMPARISONS: Final = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in"})
_TESTS: Final = frozenset({"exists", "nonempty"})
TRUTH_NODES: Final = frozenset({"and", "or", "not", "implies", *_TESTS, *_COMPARISONS})
AGGREGATES: Final = frozenset({"count", "sum"})
OPERAND_NODES: Final = frozenset({"attr", "const", "self", *AGGREGATES})


def _bad(node: JsonValue) -> GraphCliError:
    return GraphCliError("internal.error", f"unknown expression node: {node!r}")


class _Evaluator:
    def __init__(self, entity: Revision, snapshot: Snapshot, model: CompiledModel) -> None:
        self.entity = entity
        self.snapshot = snapshot
        self.model = model
        cls = model.classes.get(entity.type)
        self.attributes = {} if cls is None else cls.attributes

    def truth(self, node: JsonValue) -> bool:
        if not isinstance(node, Mapping) or len(node) != 1:
            raise _bad(node)
        ((kind, arg),) = node.items()
        match kind:
            case "and":
                return all(self.truth(item) for item in _items(arg, node))
            case "or":
                return any(self.truth(item) for item in _items(arg, node))
            case "not":
                return not self.truth(arg)
            case "implies":
                premise, conclusion = _pair(arg, node)
                return not self.truth(premise) or self.truth(conclusion)
            case "exists":
                return self.operand(arg) is not None
            case "nonempty":
                value = self.operand(arg)
                if isinstance(value, str | Mapping | list | tuple):
                    return len(value) > 0
                return value is not None
        if kind in _COMPARISONS:
            return self.compare(kind, *_pair(arg, node))
        raise _bad(node)

    def compare(self, op: str, left_node: JsonValue, right_node: JsonValue) -> bool:
        left, right = self.operand(left_node), self.operand(right_node)
        if left is None or right is None:
            return False
        numeric = self.is_numeric(left_node) or self.is_numeric(right_node)
        if op == "eq":
            return values_equal(left, right, numeric)
        if op == "ne":
            return not values_equal(left, right, numeric)
        if op == "in":
            if not isinstance(right, list | tuple):
                raise _bad({op: [left_node, right_node]})
            return any(values_equal(left, item, numeric) for item in right)
        result = order_values(left, right, numeric)
        if result is None:
            return False
        return {"lt": result < 0, "le": result <= 0, "gt": result > 0, "ge": result >= 0}[op]

    def is_numeric(self, node: JsonValue) -> bool:
        return _is_aggregate(node) or self.is_decimal(node)

    def is_decimal(self, node: JsonValue) -> bool:
        if not isinstance(node, Mapping) or "attr" not in node:
            return False
        attribute = self.attributes.get(str(node["attr"]))
        return (
            attribute is not None
            and self.model.value_base(attribute.kind, attribute.type) == "decimal"
        )

    def operand(self, node: JsonValue) -> JsonValue:
        if not isinstance(node, Mapping) or len(node) != 1:
            raise _bad(node)
        ((kind, arg),) = node.items()
        match kind:
            case "attr":
                return self.entity.attrs.get(str(arg))
            case "const":
                return arg
            case "self" if arg == "record_status":
                return self.entity.record_status
            case "count" if isinstance(arg, Mapping):
                return len(self.ends(kind, arg))
            case "sum" if isinstance(arg, Mapping):
                return self.sum(arg)
        raise _bad(node)

    def ends(self, kind: str, spec: Mapping[str, JsonValue]) -> list[EntityId]:
        """The other ends of an aggregate, one per current edge, after ``where``."""
        rel = TermRef(str(spec.get("rel")))
        direction = spec.get("direction", "out")
        if direction == "out":
            ends = [edge.target for edge in self.entity.edges if edge.rel == rel]
        elif direction == "in":
            ends = [source for source, _ in self.snapshot.incoming(self.entity.id, rel)]
        else:
            raise _bad({kind: dict(spec)})
        where = spec.get("where")
        if where is None:
            return ends
        if not isinstance(where, Mapping):
            raise _bad({kind: dict(spec)})
        statuses = where.get("record_status")
        types = where.get("type")
        return [end for end in ends if self.matches(end, statuses, types)]

    def sum(self, spec: Mapping[str, JsonValue]) -> JsonValue:
        attr = spec.get("attr")
        if not isinstance(attr, str):
            raise _bad({"sum": dict(spec)})
        addends: list[JsonValue] = []
        for end in self.ends("sum", spec):
            other = self.snapshot.get(end)
            if other is None:
                return None
            addends.append(other.attrs.get(attr))
        return exact_sum(addends)

    def matches(self, end: EntityId, statuses: JsonValue, types: JsonValue) -> bool:
        other = self.snapshot.get(end)
        if other is None:
            return False
        if isinstance(statuses, list | tuple) and other.record_status not in statuses:
            return False
        if isinstance(types, list | tuple):
            return any(self.model.is_a(other.type, str(t)) for t in types)
        return True


def exact_sum(values: Sequence[JsonValue]) -> JsonValue:
    """The exact sum of integers and decimal strings; ``None`` if one is absent or not a number."""
    numbers: list[Decimal] = []
    for value in values:
        number = _number(value)
        if number is None:
            return None
        numbers.append(number)
    whole = max((n.adjusted() + 1 for n in numbers if n), default=1)
    scale = max((-int(n.as_tuple().exponent) for n in numbers), default=0)
    context = Context(prec=max(whole, 1) + scale + len(str(len(numbers))) + 1)
    context.traps[Inexact] = True  # never rounds; a trap here is a bug in the precision
    with localcontext(context):
        total = sum(numbers, Decimal(0))
    if all(isinstance(value, int) for value in values):
        return int(total)
    return format(total, "f")


def _number(value: JsonValue) -> Decimal | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str) and DECIMAL_RE.match(value):
        return Decimal(value)
    return None


def _is_aggregate(node: JsonValue) -> bool:
    return isinstance(node, Mapping) and len(node) == 1 and next(iter(node)) in AGGREGATES


def _walk(node: JsonValue, truth: bool = True) -> Iterator[tuple[str, JsonValue, bool]]:
    """``(kind, arg, is_truth_node)`` of every node, outermost first; skips bad shapes."""
    if not isinstance(node, Mapping) or len(node) != 1:
        return
    ((kind, arg),) = node.items()
    yield kind, arg, truth
    if not truth:
        return
    if kind == "not":
        yield from _walk(arg)
    elif kind in ("and", "or", "implies") and isinstance(arg, list | tuple):
        for item in arg:
            yield from _walk(item)
    elif kind in _TESTS:
        yield from _walk(arg, False)
    elif kind in _COMPARISONS and isinstance(arg, list | tuple):
        for item in arg:
            yield from _walk(item, False)


def unknown_nodes(expr: JsonValue) -> list[str]:
    """Node kinds this evaluator does not know, in AST order."""
    return [
        kind
        for kind, _, truth in _walk(expr)
        if kind not in (TRUTH_NODES if truth else OPERAND_NODES)
    ]


def aggregate_relations(expr: JsonValue) -> frozenset[tuple[str, str]]:
    """``(rel, direction)`` of every aggregate operand in ``expr``."""
    return frozenset(
        (str(arg.get("rel")), str(arg.get("direction", "out")))
        for kind, arg, truth in _walk(expr)
        if not truth and kind in AGGREGATES and isinstance(arg, Mapping)
    )


def _explained_operands(expr: JsonValue) -> list[JsonValue]:
    found: dict[bytes, JsonValue] = {}
    for kind, arg, truth in _walk(expr):
        if not truth:
            continue
        if kind in _TESTS and _is_aggregate(arg):
            operands: Sequence[JsonValue] = [arg]
        elif kind in _COMPARISONS and isinstance(arg, list | tuple):
            operands = arg if any(_is_aggregate(item) for item in arg) else []
        else:
            continue
        for operand in operands:
            found.setdefault(canonical_bytes(operand), operand)
    return list(found.values())


def _items(arg: JsonValue, node: JsonValue) -> Sequence[JsonValue]:
    if not isinstance(arg, list | tuple):
        raise _bad(node)
    return arg


def _pair(arg: JsonValue, node: JsonValue) -> tuple[JsonValue, JsonValue]:
    items = _items(arg, node)
    if len(items) != 2:
        raise _bad(node)
    return items[0], items[1]


def evaluate(expr: JsonValue, entity: Revision, snapshot: Snapshot, model: CompiledModel) -> bool:
    """Whether ``expr`` holds for ``entity`` in ``snapshot``."""
    return _Evaluator(entity, snapshot, model).truth(expr)


def explain(
    expr: JsonValue, entity: Revision, snapshot: Snapshot, model: CompiledModel
) -> list[JsonValue] | None:
    """``[{operand, value}]`` for the aggregates in ``expr`` and the operands compared with them.

    ``None`` when ``expr`` has no aggregate. An absent value is ``null``; an
    integer outside the C.1 range is given as a decimal string.
    """
    operands = _explained_operands(expr)
    if not operands:
        return None
    evaluator = _Evaluator(entity, snapshot, model)
    result: list[JsonValue] = []
    for operand in operands:
        value = evaluator.operand(operand)
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_SAFE_INT:
            value = str(value)
        result.append({"operand": operand, "value": value})
    return result
