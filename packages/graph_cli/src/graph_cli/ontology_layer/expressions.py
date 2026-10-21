"""Authored conditions (guards, rules) and their lowering to the expression AST (B7.15).

Authored form: a list of condition items, all of which must hold. An item is

- ``{attr: <name>, <op>: <value>, ...}`` with LinkML slot-condition operators
  ``value_presence`` (PRESENT|ABSENT), ``equals_string``, ``equals_number``,
  ``equals_string_in``, ``minimum_value``, ``maximum_value``, plus
  ``nonempty: true``; several operators on one item are combined with AND
- ``{count: {rel, direction?: out|in, where?: {record_status?, type?}}, <op>: n}``
  with ``equals_number``, ``minimum_value`` or ``maximum_value``
- ``{sum: {rel, direction?: out|in, attr, where?: {record_status?, type?}}, <op>: n}``
  with the operators of ``count`` and ``value_presence`` (the sum is absent
  when a counted end lacks the attribute)
- ``{all_of: [...]}``, ``{any_of: [...]}``, ``{none_of: [...]}``

``equals_number``, ``minimum_value`` and ``maximum_value`` take an integer or
``{attr: <name>}``, an attribute of the scope entity.

Compiled AST nodes: ``and or not implies eq ne lt le gt ge in exists nonempty``,
operands ``{"attr": name}``, ``{"const": value}``, ``{"self": "record_status"}``,
``{"count": {...}}``, ``{"sum": {...}}``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.records import RECORD_STATUSES
from graph_cli.kernel.yaml_strict import YamlMap, YamlNode, YamlScalar
from graph_cli.ontology_layer.reader import Reader, join
from graph_cli.ontology_layer.vocabulary import LOWER_NAME_RE

ATTR_OPS: Final = (
    "value_presence",
    "nonempty",
    "equals_string",
    "equals_number",
    "equals_string_in",
    "minimum_value",
    "maximum_value",
)
COUNT_OPS: Final = ("equals_number", "minimum_value", "maximum_value")
SUM_OPS: Final = ("value_presence", *COUNT_OPS)
COMBINATORS: Final = ("all_of", "any_of", "none_of")

_COMPARE: Final = {"equals_number": "eq", "minimum_value": "ge", "maximum_value": "le"}


@dataclass(frozen=True, slots=True)
class AttrRef:
    """``{attr: <name>}`` as the value of a numeric operator: an attribute of the scope."""

    attr: str


OpValue = JsonValue | AttrRef
Ops = tuple[tuple[str, OpValue], ...]


@dataclass(frozen=True, slots=True)
class AttrCond:
    attr: str
    ops: Ops
    path: str


@dataclass(frozen=True, slots=True)
class CountCond:
    rel: str
    direction: str
    record_status: tuple[str, ...]
    types: tuple[str, ...]
    ops: Ops
    path: str


@dataclass(frozen=True, slots=True)
class SumCond:
    rel: str
    direction: str
    attr: str
    record_status: tuple[str, ...]
    types: tuple[str, ...]
    ops: Ops
    path: str


@dataclass(frozen=True, slots=True)
class Combinator:
    kind: str
    items: tuple[Cond, ...]
    path: str


Cond = AttrCond | CountCond | SumCond | Combinator


def parse_conditions(reader: Reader, node: YamlNode | None, path: str) -> tuple[Cond, ...] | None:
    items = reader.sequence(node, path)
    if items is None:
        return None
    if not items:
        reader.invalid(path, "expected at least one condition")
        return None
    result = []
    for index, item in enumerate(items):
        cond = _parse_item(reader, item, f"{path}[{index}]")
        if cond is None:
            return None
        result.append(cond)
    return tuple(result)


def _parse_item(reader: Reader, node: YamlNode, path: str) -> Cond | None:
    keys = set(node.items) if isinstance(node, YamlMap) else set()
    if keys & set(COMBINATORS):
        data = reader.mapping(node, path, COMBINATORS)
        if data is None or len(data) != 1:
            reader.invalid(path, "a combinator item has exactly one of all_of, any_of, none_of")
            return None
        ((kind, inner),) = data.items()
        items = parse_conditions(reader, inner, join(path, kind))
        return None if items is None else Combinator(kind, items, path)
    if "count" in keys:
        data = reader.mapping(node, path, ("count", *COUNT_OPS))
        spec = None if data is None else _spec(reader, data["count"], join(path, "count"), False)
        if data is None or spec is None:
            return None
        ops = _ops(reader, data, path, COUNT_OPS)
        if ops is None:
            return None
        if not ops:
            reader.invalid(path, "count needs equals_number, minimum_value or maximum_value")
            return None
        rel, direction, _, statuses, types = spec
        return CountCond(rel, direction, statuses, types, ops, path)
    if "sum" in keys:
        data = reader.mapping(node, path, ("sum", *SUM_OPS))
        spec = None if data is None else _spec(reader, data["sum"], join(path, "sum"), True)
        if data is None or spec is None:
            return None
        ops = _ops(reader, data, path, SUM_OPS)
        if ops is None:
            return None
        if not ops:
            reader.invalid(
                path, "sum needs value_presence, equals_number, minimum_value or maximum_value"
            )
            return None
        rel, direction, summed, statuses, types = spec
        return SumCond(rel, direction, summed, statuses, types, ops, path)
    data = reader.mapping(node, path, ("attr", *ATTR_OPS))
    if data is None:
        return None
    attr = reader.name(data.get("attr"), join(path, "attr"), LOWER_NAME_RE)
    if attr is None:
        if "attr" not in data:
            reader.invalid(path, "a condition needs attr, count, sum or a combinator")
        return None
    ops = _ops(reader, data, path, ATTR_OPS)
    if ops is None:
        return None
    if not ops:
        reader.invalid(path, "a condition needs at least one operator")
        return None
    return AttrCond(attr, ops, path)


def _spec(
    reader: Reader, node: YamlNode, path: str, summed: bool
) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...]] | None:
    """``(rel, direction, attr, record_status, types)`` of a ``count`` or ``sum`` spec."""
    keys = ("rel", "direction", "attr", "where") if summed else ("rel", "direction", "where")
    spec = reader.mapping(node, path, keys)
    if spec is None:
        return None
    what = path.rpartition(".")[2]
    if "rel" not in spec:
        reader.invalid(join(path, "rel"), f"{what} needs a relation")
        return None
    if summed and "attr" not in spec:
        reader.invalid(join(path, "attr"), "sum needs an attribute")
        return None
    rel = reader.string(spec["rel"], join(path, "rel"))
    attr = reader.name(spec["attr"], join(path, "attr"), LOWER_NAME_RE) if summed else ""
    direction = reader.choice(spec.get("direction"), join(path, "direction"), ("out", "in"))
    where = reader.mapping(spec.get("where"), join(path, "where"), ("record_status", "type"))
    if rel is None or attr is None or where is None:
        return None
    statuses = reader.names(where.get("record_status"), join(path, "where.record_status"))
    types = reader.names(where.get("type"), join(path, "where.type"))
    if statuses is None or types is None:
        return None
    bad = [s for s in statuses if s not in RECORD_STATUSES]
    if bad:
        reader.invalid(join(path, "where.record_status"), f"unknown status {bad[0]!r}")
        return None
    return rel, direction or "out", attr, statuses, types


def _ops(
    reader: Reader, data: dict[str, YamlNode], path: str, allowed: tuple[str, ...]
) -> Ops | None:
    result: list[tuple[str, OpValue]] = []
    for op in allowed:
        if op not in data:
            continue
        node, op_path = data[op], join(path, op)
        value: OpValue
        if op == "value_presence":
            value = reader.choice(node, op_path, ("PRESENT", "ABSENT"))
        elif op == "nonempty":
            if not reader.boolean(node, op_path):
                reader.invalid(op_path, "nonempty only accepts true")
                return None
            value = True
        elif op == "equals_string":
            value = reader.string(node, op_path)
        elif op == "equals_string_in":
            names = reader.names(node, op_path)
            value = None if names is None else list(names)
        else:
            value = _number(reader, node, op_path)
        if value is None:
            return None
        result.append((op, value))
    return tuple(result)


def _number(reader: Reader, node: YamlNode, path: str) -> int | AttrRef | None:
    """An integer constant or ``{attr: <name>}``."""
    if isinstance(node, YamlMap):
        data = reader.mapping(node, path, ("attr",))
        if data is None:
            return None
        if "attr" not in data:
            reader.invalid(path, "expected {attr: <name>}")
            return None
        name = reader.name(data["attr"], join(path, "attr"), LOWER_NAME_RE)
        return None if name is None else AttrRef(name)
    value = _int(node)
    if value is None:
        reader.invalid(path, "expected an integer or {attr: <name>}")
    return value


def _int(node: YamlNode) -> int | None:
    value = node.as_int() if isinstance(node, YamlScalar) else None
    return value if value is not None and abs(value) <= MAX_SAFE_INT else None


def iter_conditions(conds: tuple[Cond, ...]) -> Iterator[AttrCond | CountCond | SumCond]:
    for cond in conds:
        if isinstance(cond, Combinator):
            yield from iter_conditions(cond.items)
        else:
            yield cond


def lower(
    conds: tuple[Cond, ...],
    resolve_rel: Callable[[str], str],
    resolve_class: Callable[[str], str],
) -> JsonValue:
    """Lower authored conditions (all must hold) to one AST node."""
    nodes = [_lower_one(c, resolve_rel, resolve_class) for c in conds]
    return nodes[0] if len(nodes) == 1 else {"and": nodes}


def _lower_one(
    cond: Cond, resolve_rel: Callable[[str], str], resolve_class: Callable[[str], str]
) -> JsonValue:
    if isinstance(cond, Combinator):
        inner = [_lower_one(c, resolve_rel, resolve_class) for c in cond.items]
        if cond.kind == "all_of":
            return inner[0] if len(inner) == 1 else {"and": inner}
        any_node: JsonValue = inner[0] if len(inner) == 1 else {"or": inner}
        return any_node if cond.kind == "any_of" else {"not": any_node}
    if isinstance(cond, CountCond | SumCond):
        kind = "count" if isinstance(cond, CountCond) else "sum"
        spec: dict[str, JsonValue] = {"rel": resolve_rel(cond.rel), "direction": cond.direction}
        if isinstance(cond, SumCond):
            spec["attr"] = cond.attr
        where: dict[str, JsonValue] = {}
        if cond.record_status:
            where["record_status"] = sorted(cond.record_status)
        if cond.types:
            where["type"] = sorted(resolve_class(t) for t in cond.types)
        if where:
            spec["where"] = where
        operand: JsonValue = {kind: spec}
    else:
        operand = {"attr": cond.attr}
    parts: list[JsonValue] = []
    for op, value in cond.ops:
        if op == "value_presence":
            exists: JsonValue = {"exists": operand}
            parts.append(exists if value == "PRESENT" else {"not": exists})
        elif op == "nonempty":
            parts.append({"nonempty": operand})
        elif op == "equals_string":
            parts.append({"eq": [operand, _rhs(value)]})
        elif op == "equals_string_in":
            parts.append({"in": [operand, _rhs(value)]})
        else:
            parts.append({_COMPARE[op]: [operand, _rhs(value)]})
    return parts[0] if len(parts) == 1 else {"and": parts}


def _rhs(value: OpValue) -> JsonValue:
    return {"attr": value.attr} if isinstance(value, AttrRef) else {"const": value}
