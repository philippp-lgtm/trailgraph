"""Runtime values of the Cypher subset and their openCypher comparisons.

A value is ``None`` (null), a boolean, an integer, an exact ``Decimal``, a
string, a list, a map, a ``Node`` (the current revision of an entity) or a
``Rel`` (one edge with its source). Decimal attributes are read as
``Decimal``, so they compare and add as numbers; other attribute values are
the stored JSON.

- ``equals`` is ``=``: null when either side is null (or a list or map
  holds a null where the other side does not differ), false across types.
- ``compare`` orders numbers, strings, booleans and lists (element by
  element, then by length); null for anything else, including two values of
  different types.
- ``order_key`` is the total order of ``ORDER BY``, ``min`` and ``max``:
  maps, nodes, relationships, lists, strings, booleans, numbers, then null.
- ``group_key`` makes ``DISTINCT`` and grouping keys hashable; null equals
  null there, and ``1`` equals ``1.0``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import EdgeRecord, Revision

MIN_INTEGER: Final = -(2**63)
MAX_INTEGER: Final = 2**63 - 1


@dataclass(frozen=True, slots=True)
class Node:
    revision: Revision

    @property
    def id(self) -> EntityId:
        return self.revision.id


@dataclass(frozen=True, slots=True)
class Rel:
    source: EntityId
    edge: EdgeRecord

    @property
    def target(self) -> EntityId:
        return self.edge.target


type Value = bool | int | Decimal | str | list[Value] | dict[str, Value] | Node | Rel | None
Number = int | Decimal


def as_number(value: Value) -> Number | None:
    """The value if it is a number (booleans are not), else None."""
    if isinstance(value, int | Decimal) and not isinstance(value, bool):
        return value
    return None


def from_json(value: JsonValue, decimal: bool = False) -> Value:
    """A stored value; ``decimal``: the attribute's base type is decimal (stored as text)."""
    if isinstance(value, str):
        return Decimal(value) if decimal else value
    if isinstance(value, Mapping):
        return {k: from_json(v) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [from_json(item, decimal) for item in value]
    return value


def parameter_value(text: str) -> Value:
    """A ``--param`` value: JSON when it parses (a number with a fraction as a decimal),
    else the text itself."""

    def no_constant(name: str) -> Value:
        raise ValueError(name)

    try:
        loaded: Value = json.loads(text, parse_float=Decimal, parse_constant=no_constant)
    except ValueError:
        return text
    return loaded


def plain_json(value: Value) -> JsonValue:
    """A value without nodes or relationships as JSON: decimals as strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [plain_json(v) for v in value]
    if isinstance(value, dict):
        return {k: plain_json(v) for k, v in value.items()}
    assert not isinstance(value, Node | Rel), value
    return value


def kind(value: Value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | Decimal):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    if isinstance(value, Node):
        return "node"
    return "relationship"


def equals(left: Value, right: Value) -> bool | None:
    if left is None or right is None:
        return None
    if kind(left) != kind(right):
        return False
    if isinstance(left, list):
        assert isinstance(right, list)
        if len(left) != len(right):
            return False
        return _all_equal(zip(left, right, strict=True))
    if isinstance(left, dict):
        assert isinstance(right, dict)
        if left.keys() != right.keys():
            return False
        return _all_equal((left[k], right[k]) for k in left)
    if isinstance(left, Node):
        assert isinstance(right, Node)
        return left.id == right.id
    if isinstance(left, Rel):
        assert isinstance(right, Rel)
        return (left.source, left.edge.edge_id) == (right.source, right.edge.edge_id)
    return left == right


def _all_equal(pairs: Iterable[tuple[Value, Value]]) -> bool | None:
    unknown = False
    for a, b in pairs:
        found = equals(a, b)
        if found is False:
            return False
        unknown = unknown or found is None
    return None if unknown else True


def compare(left: Value, right: Value) -> int | None:
    """-1, 0 or 1 for comparable values; None otherwise (``<`` is then null)."""
    a, b = as_number(left), as_number(right)
    if a is not None and b is not None:
        return (a > b) - (a < b)
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    if isinstance(left, bool) and isinstance(right, bool):
        return (left > right) - (left < right)
    if isinstance(left, list) and isinstance(right, list):
        for a_item, b_item in zip(left, right, strict=False):
            order = compare(a_item, b_item)
            if order != 0:
                return order  # null when the first differing pair is not comparable
        return (len(left) > len(right)) - (len(left) < len(right))
    return None


_ORDER_RANK: Final = {
    "map": 0,
    "node": 1,
    "relationship": 2,
    "list": 3,
    "string": 4,
    "boolean": 5,
    "number": 6,
    "null": 7,
}

OrderKey = tuple[object, ...]


def order_key(value: Value) -> OrderKey:
    rank = _ORDER_RANK[kind(value)]
    if isinstance(value, list):
        return (rank, tuple(order_key(v) for v in value))
    if isinstance(value, dict):
        return (rank, tuple(sorted((k, order_key(v)) for k, v in value.items())))
    if isinstance(value, Node):
        return (rank, str(value.id))
    if isinstance(value, Rel):
        return (rank, str(value.source), str(value.edge.edge_id))
    if value is None:
        return (rank,)
    return (rank, value)


GroupKey = tuple[object, ...]


def group_key(value: Value) -> GroupKey:
    name = kind(value)
    if isinstance(value, list):
        return (name, tuple(group_key(v) for v in value))
    if isinstance(value, dict):
        return (name, tuple(sorted((k, group_key(v)) for k, v in value.items())))
    if isinstance(value, Node):
        return (name, value.id)
    if isinstance(value, Rel):
        return (name, value.source, value.edge.edge_id)
    return (name, value)
