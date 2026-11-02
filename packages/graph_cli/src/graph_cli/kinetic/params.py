"""Parameters shared by functions and actions: declaration, parsing and binding.

A function or an action declares named, typed parameters (``string integer
decimal boolean date datetime enum ref``, ``many``, ``default``). This module
holds what both kinds share:

- ``ParamDef`` and ``Problem`` (a defect of one file, as data),
- ``Parser``, the strict-YAML readers that every file format builds on, with
  the parameter grammar (``params``/``param``),
- ``bind_cli`` and ``bind_json``, which turn CLI strings and JSON into typed
  values, and ``placeholder``, a value of a parameter's type for checking
  without real values.

Everything is parametrized by the *kind* (``"function"`` or ``"action"``): the
codes are ``<kind>.parameter_missing|unknown|invalid`` and
``<kind>.invalid_value|missing_key|unknown_key``, and ``details`` carries
``{<kind>: name, ...}``.

Binding reads a value by its declared type and never guesses: ``bind_cli``
takes the text of repeated ``name=value`` pairs, ``bind_json`` takes JSON
values. Both return values in the form the query engine expects (a decimal as
``Decimal``, dates and datetimes in their canonical text) and apply defaults.
A ``ref`` stays a reference text (full id, alias or trailing hex digits):
resolving it to an entity and checking its class needs the graph. A parameter
that is optional without default (actions only) and not given is absent from
the result.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonObject, JsonValue
from graph_cli.kernel.errors import EMPTY, GraphCliError
from graph_cli.kernel.ids import parse_entity_ref, ref_text
from graph_cli.kernel.json_access import opt_str, req_bool, req_str
from graph_cli.kernel.yaml_strict import YamlMap, YamlNode, YamlScalar, YamlSeq
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import coerce_text
from graph_cli.query.cypher.errors import did_you_mean
from graph_cli.query.cypher.values import Value

PARAM_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_DETAIL: Final = 200  # parameter and column summaries, example questions

PARAM_TYPES: Final = (
    "string",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "ref",
)
_PARAM_KEYS: Final = ("type", "class", "enum", "many", "default", "summary")
_ACTION_PARAM_KEYS: Final = (*_PARAM_KEYS, "required")


@dataclass(frozen=True, slots=True)
class Problem:
    """One broken rule of one function or action. ``code`` is a ``<kind>.*`` code or the
    code of the engine error that was raised (``query.syntax``, ``term.unknown``, ...)."""

    name: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    details: Mapping[str, JsonValue] = field(default=EMPTY)
    kind: str = "function"

    @property
    def function(self) -> str:
        """The name, for callers that handle functions."""
        return self.name

    def to_json(self) -> dict[str, JsonValue]:
        return {
            self.kind: self.name,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ParamDef:
    name: str
    type: str  # one of PARAM_TYPES
    summary: str
    of_class: str | None = None  # ``class:``, only with type ref
    enum: str | None = None  # only with type enum
    many: bool = False
    default: JsonValue = None  # None = no default, the parameter is required
    optional: bool = False  # actions only: not required and without default

    @property
    def required(self) -> bool:
        return self.default is None and not self.optional

    @property
    def type_text(self) -> str:
        """The type as people read it: ``string``, ``ref org:OrgUnit``, ``list of date``."""
        kind = {"ref": f"ref {self.of_class}", "enum": f"enum {self.enum}"}.get(
            self.type, self.type
        )
        return f"list of {kind}" if self.many else kind

    @property
    def signature(self) -> str:
        """``name: type``; ``name?: type`` for an optional parameter."""
        return f"{self.name}{'' if self.required else '?'}: {self.type_text}"

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": self.name,
            "type": self.type,
            "class": self.of_class,
            "enum": self.enum,
            "many": self.many,
            "default": self.default,
            "summary": self.summary,
        }
        if self.optional:  # only when true: function catalogs stay as they were
            data["optional"] = True
        return data

    @classmethod
    def from_json(cls, data: JsonObject, path: str) -> ParamDef:
        return cls(
            name=req_str(data, "name", path),
            type=req_str(data, "type", path),
            summary=req_str(data, "summary", path),
            of_class=opt_str(data, "class", path),
            enum=opt_str(data, "enum", path),
            many=req_bool(data, "many", path),
            default=data.get("default"),
            optional=req_bool(data, "optional", path) if "optional" in data else False,
        )


def join_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def is_null(node: YamlNode) -> bool:
    return isinstance(node, YamlScalar) and node.is_null()


class Parser:
    """Collects the problems of one file; the readers return ``None`` for a bad value."""

    def __init__(self, kind: str, name: str) -> None:
        self.kind = kind
        self.name = name
        self.problems: list[Problem] = []

    def code(self, suffix: str) -> str:
        return f"{self.kind}.{suffix}"

    def add(
        self,
        code: str,
        path: str | None,
        message: str,
        *,
        line: int | None = None,
        **details: JsonValue,
    ) -> None:
        self.problems.append(Problem(self.name, code, message, path, line, details, self.kind))

    def invalid(self, path: str, message: str, node: YamlNode | None = None) -> None:
        self.add(self.code("invalid_value"), path, message, line=node.line if node else None)

    # Readers

    def mapping(
        self, node: YamlNode | None, path: str, allowed: Iterable[str]
    ) -> dict[str, YamlNode] | None:
        """A mapping with known keys only; unknown keys are reported and dropped."""
        if not isinstance(node, YamlMap):
            self.invalid(path or "(file)", "expected a mapping", node)
            return None
        keys = tuple(allowed)
        for key in node.items:
            if key not in keys:
                self.add(
                    self.code("unknown_key"),
                    join_path(path, key),
                    f"unknown key {key!r}",
                    line=node.key_lines[key],
                    expected=list(keys),
                )
        return {k: v for k, v in node.items.items() if k in keys}

    def require(self, found: Mapping[str, YamlNode], key: str, path: str, line: int) -> bool:
        if key in found:
            return True
        self.add(self.code("missing_key"), join_path(path, key), f"missing key {key!r}", line=line)
        return False

    def text(
        self, node: YamlNode | None, path: str, *, limit: int | None = None, one_line: bool = False
    ) -> str | None:
        if node is None:  # absent: reported as a missing key
            return None
        if not isinstance(node, YamlScalar) or node.is_null():
            self.invalid(path, "expected a string", node)
            return None
        value = node.text.strip()
        if not value:
            self.invalid(path, "must not be empty", node)
            return None
        if limit is not None and len(value) > limit:
            self.invalid(path, f"is {len(value)} characters; at most {limit}", node)
            return None
        if one_line and "\n" in value:
            self.invalid(path, "must be one line", node)
            return None
        return value

    def name_of(self, node: YamlNode | None, path: str, grammar: re.Pattern[str]) -> str | None:
        value = self.text(node, path)
        if value is not None and not grammar.match(value):
            self.invalid(path, f"{value!r} does not match {grammar.pattern}", node)
            return None
        return value

    def boolean(self, node: YamlNode | None, path: str) -> bool | None:
        value = node.as_bool() if isinstance(node, YamlScalar) else None
        if value is None:
            self.invalid(path, "expected true or false", node)
        return value

    def choice(self, node: YamlNode | None, path: str, options: tuple[str, ...]) -> str | None:
        value = self.text(node, path)
        if value is not None and value not in options:
            self.invalid(path, f"{value!r} is not one of {', '.join(options)}", node)
            return None
        return value

    def sequence(self, node: YamlNode | None, path: str) -> tuple[YamlNode, ...] | None:
        if node is None:
            return None
        if not isinstance(node, YamlSeq):
            self.invalid(path, "expected a list", node)
            return None
        if not node.items:
            self.invalid(path, "must not be empty", node)
            return None
        return node.items

    def literal(self, node: YamlNode, path: str) -> JsonValue:
        """A default or example value: a scalar or a list of scalars (YAML 1.2 core typing)."""
        if isinstance(node, YamlSeq):
            items = [self.literal(item, f"{path}[{i}]") for i, item in enumerate(node.items)]
            return None if any(i is None for i in items) else items
        if isinstance(node, YamlMap) or is_null(node):
            self.invalid(path, "expected a string, number, boolean or a list of them", node)
            return None
        boolean = node.as_bool()
        if boolean is not None:
            return boolean
        number = node.as_int()
        return node.text if number is None else number

    # Parameters

    def params(self, node: YamlNode) -> tuple[ParamDef, ...] | None:
        if not isinstance(node, YamlMap):
            if is_null(node):
                return ()
            self.invalid("params", "expected a mapping", node)
            return None
        result: list[ParamDef] = []
        for key, value in node.items.items():
            path = f"params.{key}"
            if not PARAM_RE.match(key):
                self.add(
                    self.code("invalid_value"),
                    path,
                    f"parameter name {key!r} does not match {PARAM_RE.pattern}",
                    line=node.key_lines[key],
                )
                continue
            found = self.param(key, value, path)
            if found is not None:
                result.append(found)
        return tuple(result)

    def param(self, name: str, node: YamlNode, path: str) -> ParamDef | None:
        keys = _ACTION_PARAM_KEYS if self.kind == "action" else _PARAM_KEYS
        body = self.mapping(node, path, keys)
        if body is None:
            return None
        self.require(body, "type", path, node.line)
        self.require(body, "summary", path, node.line)
        kind = (
            self.choice(body.get("type"), f"{path}.type", PARAM_TYPES) if "type" in body else None
        )
        summary = self.text(body.get("summary"), f"{path}.summary", limit=MAX_DETAIL)
        of_class = enum = None
        if "class" in body:
            of_class = self.text(body["class"], f"{path}.class")
        if "enum" in body:
            enum = self.text(body["enum"], f"{path}.enum")
        many = self.boolean(body["many"], f"{path}.many") if "many" in body else False
        default = None
        if "default" in body:
            default = self.literal(body["default"], f"{path}.default")
        required = None
        if "required" in body:
            required = self.boolean(body["required"], f"{path}.required")
        if required is not None and "default" in body:
            self.invalid(
                f"{path}.required",
                "a default already makes the parameter optional; drop required or the default",
                body["required"],
            )
        if kind == "ref" and "class" not in body:
            self.add(
                self.code("missing_key"), f"{path}.class", "a ref needs a class", line=node.line
            )
        if kind == "enum" and "enum" not in body:
            self.add(
                self.code("missing_key"), f"{path}.enum", "an enum needs enum:", line=node.line
            )
        if kind is not None and kind != "ref" and "class" in body:
            self.invalid(f"{path}.class", "only a ref has a class", body["class"])
        if kind is not None and kind != "enum" and "enum" in body:
            self.invalid(f"{path}.enum", "only an enum has enum:", body["enum"])
        if kind is None or summary is None or many is None:
            return None
        return ParamDef(
            name, kind, summary, of_class, enum, many, default, optional=required is False
        )


# Binding

_PLACEHOLDERS: Final[dict[str, JsonValue]] = {
    "string": "x",
    "integer": 0,
    "decimal": "0",
    "boolean": False,
    "date": "2000-01-01",
    "datetime": "2000-01-01T00:00:00.000Z",
    "ref": "00000000",
}


def bind_cli(
    kind: str,
    name: str,
    params: Sequence[ParamDef],
    pairs: Sequence[tuple[str, str]],
    model: CompiledModel,
) -> dict[str, Value]:
    """Values from ``name=value`` pairs; a ``many`` parameter repeats its name."""
    grouped: dict[str, list[str]] = {}
    for key, text in pairs:
        grouped.setdefault(key, []).append(text)
    _check_names(kind, name, params, grouped)
    bound: dict[str, Value] = {}
    for param in params:
        if param.name not in grouped:
            _put_default(bound, kind, name, param, model)
            continue
        texts = grouped[param.name]
        if not param.many and len(texts) > 1:
            raise _invalid(kind, name, param, "is given more than once")
        values = [
            _checked(kind, name, param, partial(_from_text, param, text, model)) for text in texts
        ]
        bound[param.name] = values if param.many else values[0]
    return bound


def bind_json(
    kind: str,
    name: str,
    params: Sequence[ParamDef],
    values: Mapping[str, JsonValue],
    model: CompiledModel,
) -> dict[str, Value]:
    """Values from a JSON object; a ``many`` parameter takes a list."""
    _check_names(kind, name, params, values)
    bound: dict[str, Value] = {}
    for param in params:
        if param.name not in values:
            _put_default(bound, kind, name, param, model)
            continue
        given = values[param.name]
        if not param.many:
            bound[param.name] = _checked(
                kind, name, param, partial(_from_json, param, given, model)
            )
        elif isinstance(given, list):
            bound[param.name] = [
                _checked(kind, name, param, partial(_from_json, param, v, model)) for v in given
            ]
        else:
            raise _invalid(kind, name, param, "expects a list")
    return bound


def placeholder(param: ParamDef, model: CompiledModel) -> Value:
    """A value of the parameter's type, for resolving a query without real values."""
    if param.type == "enum":
        names = _enum_names(param, model)
        one: JsonValue = names[0] if names else "x"
    else:
        one = _PLACEHOLDERS[param.type]
    value = _materialize(param, _scalar(one))
    return [value] if param.many else value


def json_value(value: Value) -> JsonValue:
    """A bound value as the JSON a change set carries (a decimal as its text)."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, list):
        return [json_value(v) for v in value]
    if value is None or isinstance(value, bool | int | str):
        return value
    raise GraphCliError("internal.error", f"a parameter value cannot be {type(value).__name__}")


def text_value(value: Value) -> str:
    """A bound scalar in the text form of the ``*_text`` change-set keys."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int | str):
        return str(value)
    raise GraphCliError("internal.error", f"no text form for {type(value).__name__}")


def _check_names(
    kind: str, name: str, params: Sequence[ParamDef], given: Mapping[str, object]
) -> None:
    known = [p.name for p in params]
    for key in given:
        if key not in known:
            raise GraphCliError(
                f"{kind}.parameter_unknown",
                f"{name} has no parameter {key!r}"
                + (f"; it takes {', '.join(known)}" if known else " and takes no parameters"),
                details={
                    kind: name,
                    "parameter": key,
                    "expected": list(known),
                    "did_you_mean": did_you_mean(key, known),
                },
            )


def _put_default(
    bound: dict[str, Value], kind: str, name: str, param: ParamDef, model: CompiledModel
) -> None:
    """The default of a parameter that was not given; nothing for an optional one."""
    if param.default is None:
        if param.optional:
            return
        raise GraphCliError(
            f"{kind}.parameter_missing",
            f"{name} needs the parameter {param.name!r} ({param.type_text})",
            details={kind: name, "parameter": param.name, "type": param.type_text},
        )
    if param.many and isinstance(param.default, list):
        bound[param.name] = [
            _checked(kind, name, param, partial(_from_json, param, v, model)) for v in param.default
        ]
        return
    read = partial(_from_json, param, param.default, model)
    bound[param.name] = _checked(kind, name, param, read)


def _invalid(kind: str, name: str, param: ParamDef, message: str) -> GraphCliError:
    return GraphCliError(
        f"{kind}.parameter_invalid",
        f"parameter {param.name!r} of {name} {message}",
        details={
            kind: name,
            "parameter": param.name,
            "type": param.type_text,
            "reason": message,
        },
    )


def _checked(kind: str, name: str, param: ParamDef, read: Callable[[], str | int | bool]) -> Value:
    try:
        value = read()
    except ValueError as exc:
        raise _invalid(kind, name, param, f"expects {param.type_text}: {exc}") from None
    return _materialize(param, value)


def _materialize(param: ParamDef, value: str | int | bool) -> Value:
    return Decimal(value) if param.type == "decimal" and isinstance(value, str) else value


def _scalar(value: JsonValue) -> str | int | bool:
    assert isinstance(value, str | int)  # bool is an int
    return value


def _enum_names(param: ParamDef, model: CompiledModel) -> tuple[str, ...]:
    found = model.enums.get(param.enum) if param.enum is not None else None
    return found.value_names() if found is not None else ()


def _enum_value(param: ParamDef, text: str, model: CompiledModel) -> str:
    names = _enum_names(param, model)
    if text not in names:
        raise ValueError(f"{text!r} is not one of {', '.join(names)}")
    return text


def _reference(text: str) -> str:
    parsed = parse_entity_ref(text, allow_abbrev=True)
    if parsed is None:
        raise ValueError(
            f"{text!r} is not an entity reference (full id, alias or 8-32 trailing hex digits); "
            "a name has to be looked up first"
        )
    return ref_text(parsed)


def _from_text(param: ParamDef, text: str, model: CompiledModel) -> str | int | bool:
    if param.type == "string":
        return text
    if param.type == "enum":
        return _enum_value(param, text, model)
    if param.type == "ref":
        return _reference(text)
    return _scalar(coerce_text(param.type, text))


def _from_json(param: ParamDef, value: JsonValue, model: CompiledModel) -> str | int | bool:
    kind = param.type
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_SAFE_INT:
            raise ValueError(f"{value!r} is not an integer")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{value!r} is not true or false")
        return value
    if kind == "decimal" and isinstance(value, int) and not isinstance(value, bool):
        return _scalar(coerce_text("decimal", str(value)))
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a string")
    return _from_text(param, value, model)
