"""Authored conditions (guards, rules) and their lowering to the expression AST (B7.15).

Authored form: a list of condition items, all of which must hold. An item is

- ``{attr: <name>, <op>: <value>, ...}`` with LinkML slot-condition operators
  ``value_presence`` (PRESENT|ABSENT), ``equals_string``, ``equals_number``,
  ``equals_string_in``, ``minimum_value``, ``maximum_value``, plus
  ``nonempty: true``; several operators on one item are combined with AND
- ``{count: {rel, direction?: out|in, where?: {record_status?, type?}}, <op>: n}``
  with ``equals_number``, ``minimum_value`` or ``maximum_value``
- ``{all_of: [...]}``, ``{any_of: [...]}``, ``{none_of: [...]}``

Compiled AST nodes: ``and or not implies eq ne lt le gt ge in exists nonempty``,
operands ``{"attr": name}``, ``{"const": value}``, ``{"self": "record_status"}``,
``{"count": {...}}``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.records import RECORD_STATUSES
from graph_cli.ontology_layer.reader import Reader, join
from graph_cli.ontology_layer.vocabulary import LOWER_NAME_RE
from graph_cli.ontology_layer.yaml_strict import YamlMap, YamlNode, YamlScalar

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
COMBINATORS: Final = ("all_of", "any_of", "none_of")

_COMPARE: Final = {"equals_number": "eq", "minimum_value": "ge", "maximum_value": "le"}


@dataclass(frozen=True, slots=True)
class AttrCond:
    attr: str
    ops: tuple[tuple[str, JsonValue], ...]
    path: str


@dataclass(frozen=True, slots=True)
class CountCond:
    rel: str
    direction: str
    record_status: tuple[str, ...]
    types: tuple[str, ...]
    ops: tuple[tuple[str, JsonValue], ...]
    path: str


@dataclass(frozen=True, slots=True)
class Combinator:
    kind: str
    items: tuple[Cond, ...]
    path: str


Cond = AttrCond | CountCond | Combinator


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
        if data is None:
            return None
        spec = reader.mapping(data["count"], join(path, "count"), ("rel", "direction", "where"))
        if spec is None:
            return None
        if "rel" not in spec:
            reader.invalid(join(path, "count.rel"), "count needs a relation")
            return None
        rel = reader.string(spec["rel"], join(path, "count.rel"))
        direction = reader.choice(
            spec.get("direction"), join(path, "count.direction"), ("out", "in")
        )
        where = reader.mapping(
            spec.get("where"), join(path, "count.where"), ("record_status", "type")
        )
        if rel is None or where is None:
            return None
        statuses = reader.names(where.get("record_status"), join(path, "count.where.record_status"))
        types = reader.names(where.get("type"), join(path, "count.where.type"))
        if statuses is None or types is None:
            return None
        bad = [s for s in statuses if s not in RECORD_STATUSES]
        if bad:
            reader.invalid(join(path, "count.where.record_status"), f"unknown status {bad[0]!r}")
            return None
        ops = _ops(reader, data, path, COUNT_OPS)
        if ops is None:
            return None
        if not ops:
            reader.invalid(path, "count needs equals_number, minimum_value or maximum_value")
            return None
        return CountCond(rel, direction or "out", statuses, types, ops, path)
    data = reader.mapping(node, path, ("attr", *ATTR_OPS))
    if data is None:
        return None
    attr = reader.name(data.get("attr"), join(path, "attr"), LOWER_NAME_RE)
    if attr is None:
        if "attr" not in data:
            reader.invalid(path, "a condition needs attr, count or a combinator")
        return None
    ops = _ops(reader, data, path, ATTR_OPS)
    if ops is None:
        return None
    if not ops:
        reader.invalid(path, "a condition needs at least one operator")
        return None
    return AttrCond(attr, ops, path)


def _ops(
    reader: Reader, data: dict[str, YamlNode], path: str, allowed: tuple[str, ...]
) -> tuple[tuple[str, JsonValue], ...] | None:
    result: list[tuple[str, JsonValue]] = []
    for op in allowed:
        if op not in data:
            continue
        node, op_path = data[op], join(path, op)
        value: JsonValue
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
            value = _int(node)
            if value is None:
                reader.invalid(op_path, "expected an integer")
        if value is None:
            return None
        result.append((op, value))
    return tuple(result)


def _int(node: YamlNode) -> int | None:
    value = node.as_int() if isinstance(node, YamlScalar) else None
    return value if value is not None and abs(value) <= MAX_SAFE_INT else None


def iter_conditions(conds: tuple[Cond, ...]) -> Iterator[AttrCond | CountCond]:
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
    if isinstance(cond, CountCond):
        count: dict[str, JsonValue] = {"rel": resolve_rel(cond.rel), "direction": cond.direction}
        where: dict[str, JsonValue] = {}
        if cond.record_status:
            where["record_status"] = sorted(cond.record_status)
        if cond.types:
            where["type"] = sorted(resolve_class(t) for t in cond.types)
        if where:
            count["where"] = where
        parts: list[JsonValue] = [
            {_COMPARE[op]: [{"count": count}, {"const": value}]} for op, value in cond.ops
        ]
        return parts[0] if len(parts) == 1 else {"and": parts}
    operand: JsonValue = {"attr": cond.attr}
    parts = []
    for op, value in cond.ops:
        if op == "value_presence":
            exists: JsonValue = {"exists": operand}
            parts.append(exists if value == "PRESENT" else {"not": exists})
        elif op == "nonempty":
            parts.append({"nonempty": operand})
        elif op in ("equals_string", "equals_number"):
            parts.append({"eq": [operand, {"const": value}]})
        elif op == "equals_string_in":
            parts.append({"in": [operand, {"const": value}]})
        else:
            parts.append({_COMPARE[op]: [operand, {"const": value}]})
    return parts[0] if len(parts) == 1 else {"and": parts}
