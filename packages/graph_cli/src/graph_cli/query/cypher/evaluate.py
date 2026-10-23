"""Expressions of a resolved query over one row, with openCypher's null logic.

``AND``, ``OR``, ``XOR`` and ``NOT`` are three-valued; comparisons, string
predicates and arithmetic with a null operand are null; ``WHERE`` keeps a
row only when its condition is true. Wrong operand types, division by zero
and integer overflow are ``query.invalid_value``.

Aggregates are computed by the executor per group; ``evaluate`` reads
their values from ``aggregates`` (keyed by the aggregate node's ``id()``).
Pattern predicates call back into the executor through ``exists``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    localcontext,
)
from typing import Final

from graph_cli.kernel.clock import format_timestamp
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.ast import (
    Binary,
    Case,
    Const,
    CountStar,
    Expr,
    FunctionCall,
    HasLabels,
    IsNull,
    ListExpr,
    MapExpr,
    Parameter,
    PatternPart,
    PatternPredicate,
    Pos,
    Property,
    Unary,
    Variable,
)
from graph_cli.query.cypher.errors import invalid_value
from graph_cli.query.cypher.resolve import EDGE_ID, is_aggregate, short_names
from graph_cli.query.cypher.values import (
    MAX_INTEGER,
    MIN_INTEGER,
    Node,
    Number,
    Rel,
    Value,
    as_number,
    compare,
    equals,
    from_json,
    kind,
)

Row = Mapping[str, Value]
_ORDERING: Final = {"<": (-1,), "<=": (-1, 0), ">": (1,), ">=": (1, 0)}
# The system fields in the order properties() and keys() list them.
NODE_FIELDS: Final = (
    "id",
    "alias",
    "type",
    "rev",
    "record_status",
    "created_at",
    "recorded_at",
    "tx_id",
    "tx_seq",
    "valid_from",
    "valid_to",
    "content_hash",
    "ontology_version",
)
_NUMBER_TEXT: Final = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
_ROUNDING: Final = {"ceil": ROUND_CEILING, "floor": ROUND_FLOOR, "round": ROUND_HALF_UP}


class Evaluator:
    def __init__(
        self,
        model: CompiledModel,
        parameters: Mapping[str, Value],
        exists: Callable[[PatternPart, Row], bool | None],
    ) -> None:
        self.model = model
        self.parameters = dict(parameters)
        self.exists = exists
        # labels() and type() return names as a query writes them (local when unique)
        self.class_names = short_names(model.classes)
        self.relation_names = short_names(model.relations)

    def condition(self, e: Expr, row: Row) -> bool:
        """``WHERE``: true keeps the row, false and null drop it."""
        value = self.evaluate(e, row)
        if value is None or isinstance(value, bool):
            return value is True
        raise invalid_value(e.pos, f"WHERE needs a boolean, got a {kind(value)}")

    def evaluate(self, e: Expr, row: Row, aggregates: Mapping[int, Value] | None = None) -> Value:
        match e:
            case Const(value):
                return value
            case Parameter(name):
                return self.parameters[name]
            case Variable(name):
                return row[name]
            case Property(subject, key):
                return self.property(self.evaluate(subject, row, aggregates), key, e.pos)
            case ListExpr(items):
                return [self.evaluate(i, row, aggregates) for i in items]
            case MapExpr(entries):
                return {k: self.evaluate(v, row, aggregates) for k, v in entries}
            case Unary(op, operand):
                return self.unary(op, self.evaluate(operand, row, aggregates), e.pos)
            case Binary("and" | "or" | "xor" as op, left, right):
                return self.logic(op, e, row, aggregates)
            case Binary(op, left, right):
                return self.binary(
                    op,
                    self.evaluate(left, row, aggregates),
                    self.evaluate(right, row, aggregates),
                    e,
                )
            case IsNull(operand, negated):
                return (self.evaluate(operand, row, aggregates) is None) != negated
            case CountStar() | FunctionCall() if is_aggregate(e):
                assert aggregates is not None, "aggregate outside a projection"
                return aggregates[id(e)]
            case FunctionCall(name, args):
                return self.function(name, [self.evaluate(a, row, aggregates) for a in args], e.pos)
            case Case(subject, whens, default):
                return self.case(subject, whens, default, row, aggregates)
            case HasLabels(subject, groups):
                labeled = self.evaluate(subject, row, aggregates)
                if labeled is None:
                    return None
                if not isinstance(labeled, Node):
                    raise invalid_value(e.pos, f"only nodes have labels, got a {kind(labeled)}")
                node_type = labeled.revision.type
                return all(any(self.model.is_a(node_type, t) for t in g) for g in groups)
            case PatternPredicate(part):
                return self.exists(part, row)
        raise AssertionError(f"unknown expression {e!r}")

    # Properties

    def property(self, subject: Value, key: str, pos: Pos) -> Value:
        if subject is None:
            return None
        if isinstance(subject, Node):
            return self.node_field(subject, key)
        if isinstance(subject, Rel):
            return self.rel_field(subject, key)
        if isinstance(subject, dict):
            return subject.get(key)
        raise invalid_value(pos, f"a {kind(subject)} has no property {key!r}")

    def node_field(self, node: Node, key: str) -> Value:
        revision = node.revision
        match key:
            case "id":
                return str(revision.id)
            case "type":
                return str(revision.type)
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
        cls = self.model.classes.get(revision.type)
        attribute = None if cls is None else cls.attributes.get(key)
        decimal = attribute is not None and (
            self.model.value_base(attribute.kind, attribute.type) == "decimal"
        )
        return from_json(revision.attrs.get(key), decimal)

    def rel_field(self, rel: Rel, key: str) -> Value:
        if key == EDGE_ID:
            return str(rel.edge.edge_id)
        relation = self.model.relations.get(rel.edge.rel)
        qualifier = None if relation is None else relation.qualifiers.get(key)
        decimal = qualifier is not None and (
            self.model.value_base(qualifier.kind, qualifier.type) == "decimal"
        )
        return from_json(rel.edge.qualifiers.get(key), decimal)

    # Operators

    def logic(self, op: str, e: Binary, row: Row, aggregates: Mapping[int, Value] | None) -> Value:
        # Both sides always: a non-boolean is an error even when the other decides.
        left = _boolean(self.evaluate(e.left, row, aggregates), e.left.pos)
        right = _boolean(self.evaluate(e.right, row, aggregates), e.right.pos)
        if op == "and" and False in (left, right):
            return False
        if op == "or" and True in (left, right):
            return True
        if None in (left, right):
            return None
        return True if op == "and" else False if op == "or" else left != right

    def unary(self, op: str, value: Value, pos: Pos) -> Value:
        if value is None:
            return None
        if op == "not":
            return not _boolean(value, pos)
        number = as_number(value)
        if number is None:
            raise invalid_value(pos, f"unary {op} needs a number, got a {kind(value)}")
        return _checked(-number if op == "-" else number, pos)

    def binary(self, op: str, left: Value, right: Value, e: Binary) -> Value:
        if op == "=":
            return equals(left, right)
        if op == "<>":
            same = equals(left, right)
            return None if same is None else not same
        if op in _ORDERING:
            order = compare(left, right)
            return None if order is None else order in _ORDERING[op]
        if op == "in":
            return _contains(left, right, e.right.pos)
        if op in ("starts_with", "ends_with", "contains"):
            if not isinstance(left, str) or not isinstance(right, str):
                return None
            if op == "starts_with":
                return left.startswith(right)
            return left.endswith(right) if op == "ends_with" else right in left
        return _arithmetic(op, left, right, e.pos)

    def case(
        self,
        subject: Expr | None,
        whens: tuple[tuple[Expr, Expr], ...],
        default: Expr | None,
        row: Row,
        aggregates: Mapping[int, Value] | None,
    ) -> Value:
        tested = None if subject is None else self.evaluate(subject, row, aggregates)
        for when, then in whens:
            value = self.evaluate(when, row, aggregates)
            hit = equals(tested, value) if subject is not None else value
            if hit is True:
                return self.evaluate(then, row, aggregates)
        return None if default is None else self.evaluate(default, row, aggregates)

    # Functions (aggregates are the executor's)

    def function(self, name: str, args: list[Value], pos: Pos) -> Value:
        if name == "coalesce":
            return next((a for a in args if a is not None), None)
        if any(a is None for a in args):
            return None
        value = args[0]
        match name:
            case "labels":
                if not isinstance(value, Node):
                    raise invalid_value(pos, f"labels() needs a node, got a {kind(value)}")
                cls = self.model.classes.get(value.revision.type)
                types = [str(value.revision.type), *(cls.ancestors if cls else ())]
                names: list[Value] = [self.class_names.get(t, t) for t in types]
                return names
            case "type":
                if not isinstance(value, Rel):
                    raise invalid_value(pos, f"type() needs a relationship, got a {kind(value)}")
                return self.relation_names.get(value.edge.rel, str(value.edge.rel))
            case "keys":
                return list(self.properties(value, name, pos))
            case "properties":
                return self.properties(value, name, pos)
            case "size":
                if not isinstance(value, list | str):
                    raise invalid_value(
                        pos, f"size() needs a list or a string, got a {kind(value)}"
                    )
                return len(value)
            case "head" | "last":
                if not isinstance(value, list):
                    raise invalid_value(pos, f"{name}() needs a list, got a {kind(value)}")
                return (value[0] if name == "head" else value[-1]) if value else None
            case "tostring":
                return _to_string(value, pos)
            case "tointeger":
                return _to_integer(value, pos)
            case "tofloat":
                return _to_decimal(value, pos)
            case "abs" | "ceil" | "floor" | "round":
                return _rounding(name, args, pos)
        strings = _strings(name, args, pos)
        match name:
            case "tolower":
                return strings[0].lower()
            case "toupper":
                return strings[0].upper()
            case "trim":
                return strings[0].strip()
            case "split":
                text, separator = strings
                parts: list[Value] = [*(text.split(separator) if separator else text)]
                return parts
            case "replace":
                text, old, new = strings
                return text.replace(old, new)
        assert name == "substring", name
        return _substring(strings[0], args[1:], pos)

    def properties(self, value: Value, name: str, pos: Pos) -> dict[str, Value]:
        """``properties()``: system fields in their fixed order, then the set attributes or
        qualifiers by name; ``keys()`` is its keys."""
        if isinstance(value, dict):
            return dict(sorted(value.items()))
        if isinstance(value, Node):
            fields = [*NODE_FIELDS, *sorted(value.revision.attrs)]
            found = {k: self.node_field(value, k) for k in fields}
        elif isinstance(value, Rel):
            fields = [EDGE_ID, *sorted(value.edge.qualifiers)]
            found = {k: self.rel_field(value, k) for k in fields}
        else:
            raise invalid_value(
                pos, f"{name}() needs a node, a relationship or a map, got a {kind(value)}"
            )
        return {k: v for k, v in found.items() if v is not None}


def _to_string(value: Value, pos: Pos) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | Decimal):
        return str(value)
    raise invalid_value(
        pos, f"tostring() needs a number, a boolean or a string, got a {kind(value)}"
    )


def _to_integer(value: Value, pos: Pos) -> int | None:
    """``toInteger()``: numbers truncate toward zero, text that is no number is null."""
    if isinstance(value, bool | int):
        return int(value)
    if not isinstance(value, Decimal | str):
        raise invalid_value(
            pos, f"tointeger() needs a number, a boolean or a string, got a {kind(value)}"
        )
    number = _parse_number(value) if isinstance(value, str) else value
    if number is None:
        return None
    whole = number.to_integral_value(rounding=ROUND_DOWN)
    if not MIN_INTEGER <= whole <= MAX_INTEGER:
        raise invalid_value(pos, "integer overflow")
    return int(whole)


def _to_decimal(value: Value, pos: Pos) -> Decimal | None:
    """``toFloat()``: there are no floats, so an exact decimal; text that is no number is
    null."""
    if isinstance(value, str):
        return _parse_number(value)
    number = as_number(value)
    if number is None:
        raise invalid_value(pos, f"tofloat() needs a number or a string, got a {kind(value)}")
    return Decimal(number)


def _parse_number(text: str) -> Decimal | None:
    text = text.strip()
    return Decimal(text) if _NUMBER_TEXT.fullmatch(text) else None


def _rounding(name: str, args: list[Value], pos: Pos) -> Number:
    """``abs``, ``ceil``, ``floor``, ``round`` (half away from zero, optional precision);
    the result is an integer for an integer, else a decimal."""
    number = as_number(args[0])
    if number is None:
        raise invalid_value(pos, f"{name}() needs a number, got a {kind(args[0])}")
    if name == "abs":
        return _checked(-number if number < 0 else number, pos)
    precision = args[1] if len(args) > 1 else 0
    if not isinstance(precision, int) or isinstance(precision, bool):
        raise invalid_value(pos, f"round() needs an integer precision, got a {kind(precision)}")
    exact = Decimal(number)
    exponent = exact.as_tuple().exponent
    if isinstance(exponent, int) and exponent >= -precision and precision >= 0:
        return number  # nothing to round
    try:
        with localcontext() as context:
            context.prec = max(context.prec, len(exact.as_tuple().digits) + 1)
            rounded = exact.quantize(Decimal(1).scaleb(-precision), rounding=_ROUNDING[name])
    except DecimalException:
        raise invalid_value(pos, f"round() precision {precision} is out of range") from None
    if isinstance(number, int):
        return _checked(int(rounded), pos)
    return rounded.quantize(Decimal(1)) if precision < 0 else rounded  # 1.2E+3 as 1200


def _strings(name: str, args: list[Value], pos: Pos) -> list[str]:
    """The string arguments of a string function (``substring``: the first only)."""
    found: list[str] = []
    for arg in args[:1] if name == "substring" else args:
        if not isinstance(arg, str):
            raise invalid_value(pos, f"{name}() needs a string, got a {kind(arg)}")
        found.append(arg)
    return found


def _substring(text: str, bounds: list[Value], pos: Pos) -> str:
    """``substring(text, start[, length])``, counted from 0."""
    numbers: list[int] = []
    for bound in bounds:
        if not isinstance(bound, int) or isinstance(bound, bool) or bound < 0:
            raise invalid_value(
                pos, "substring() needs a start and a length that are integers >= 0"
            )
        numbers.append(bound)
    start = numbers[0]
    return text[start:] if len(numbers) == 1 else text[start : start + numbers[1]]


def _boolean(value: Value, pos: Pos) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise invalid_value(pos, f"expected a boolean, got a {kind(value)}")


def _contains(item: Value, items: Value, pos: Pos) -> bool | None:
    if items is None:
        return None
    if not isinstance(items, list):
        raise invalid_value(pos, f"IN needs a list, got a {kind(items)}")
    unknown = False
    for candidate in items:
        found = equals(item, candidate)
        if found is True:
            return True
        unknown = unknown or found is None
    return None if unknown else False


def _checked(value: Number, pos: Pos) -> Number:
    if isinstance(value, int) and not MIN_INTEGER <= value <= MAX_INTEGER:
        raise invalid_value(pos, "integer overflow")
    return value


def _arithmetic(op: str, left: Value, right: Value, pos: Pos) -> Value:
    if left is None or right is None:
        return None
    if op == "+" and isinstance(left, str) and isinstance(right, str):
        return left + right
    if op == "+" and (isinstance(left, list) or isinstance(right, list)):
        joined: list[Value] = [*left] if isinstance(left, list) else [left]
        joined += right if isinstance(right, list) else [right]
        return joined
    a, b = as_number(left), as_number(right)
    if a is None or b is None:
        raise invalid_value(pos, f"{op} needs numbers, got a {kind(left)} and a {kind(right)}")
    try:
        if isinstance(a, int) and isinstance(b, int):
            return _checked(_integer(op, a, b, pos), pos)
        x, y = Decimal(a), Decimal(b)
        result = {"+": x + y, "-": x - y, "*": x * y}.get(op)
        if result is None:
            result = x / y if op == "/" else x % y
        return result
    except (DivisionByZero, InvalidOperation, ZeroDivisionError):
        raise invalid_value(pos, "division by zero") from None


def _integer(op: str, a: int, b: int, pos: Pos) -> int:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if b == 0:
        raise invalid_value(pos, "division by zero")
    quotient = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)  # truncates toward zero
    return quotient if op == "/" else a - b * quotient
