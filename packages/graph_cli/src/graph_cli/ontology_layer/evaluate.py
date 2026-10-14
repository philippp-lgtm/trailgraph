"""Evaluator for the compiled expression AST: guards and rules (contracts B7.15).

Semantics follow the reference query evaluator (``kernel.query_eval``):

- **Operands.** ``{"attr": n}`` reads ``attrs`` (missing and JSON ``null``
  are absent), ``{"self": "record_status"}`` the system field,
  ``{"const": v}`` a constant, ``{"count": {...}}`` an integer.
- **Comparisons** are type-strict; an absent operand matches nothing, so
  ``ne`` on an absent value is false. A ``decimal`` attribute compares
  numerically with decimal strings and integers.
- **``exists``**: the operand is present. **``nonempty``**: present, and a
  non-empty string, array or object (other present values count as
  non-empty).
- **``count{rel, direction, where?}``** counts current edges: ``out`` over
  the entity's own edges, ``in`` over ``snapshot.incoming``.
  ``where.record_status`` and ``where.type`` filter the other end; ``type``
  includes subclasses; a missing other end never matches a filter.

The compiler emits only known nodes, so an unknown node is a bug
(``internal.error``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_eval import order_values, values_equal
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.model import CompiledModel

_COMPARISONS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in"})


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
        numeric = self.is_decimal(left_node) or self.is_decimal(right_node)
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
                return self.count(arg)
        raise _bad(node)

    def count(self, spec: Mapping[str, JsonValue]) -> int:
        rel = TermRef(str(spec.get("rel")))
        direction = spec.get("direction", "out")
        if direction == "out":
            ends = [edge.target for edge in self.entity.edges if edge.rel == rel]
        elif direction == "in":
            ends = [source for source, _ in self.snapshot.incoming(self.entity.id, rel)]
        else:
            raise _bad({"count": dict(spec)})
        where = spec.get("where")
        if where is None:
            return len(ends)
        if not isinstance(where, Mapping):
            raise _bad({"count": dict(spec)})
        statuses = where.get("record_status")
        types = where.get("type")
        return sum(1 for end in ends if self.matches(end, statuses, types))

    def matches(self, end: EntityId, statuses: JsonValue, types: JsonValue) -> bool:
        other = self.snapshot.get(end)
        if other is None:
            return False
        if isinstance(statuses, list | tuple) and other.record_status not in statuses:
            return False
        if isinstance(types, list | tuple):
            return any(self.model.is_a(other.type, str(t)) for t in types)
        return True


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
