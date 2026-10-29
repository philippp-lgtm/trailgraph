"""Parameter values of a function call, coerced by the declared type.

A function declares its parameters, so a value is read by that type and never
guessed: ``bind_cli`` takes the text of repeated ``name=value`` pairs,
``bind_json`` takes JSON values. Both return the query parameters in the form
the Cypher engine expects (a decimal as ``Decimal``, dates and datetimes in
their canonical text) and apply defaults. A ``ref`` stays a reference text
(full id, alias or trailing hex digits) here: resolving it to an entity and
checking its class needs the graph.

Errors are ``function.parameter_missing``, ``function.parameter_unknown`` and
``function.parameter_invalid``, each with the function and parameter name in
``details``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from functools import partial
from typing import Final

from graph_cli.kernel.canonical import MAX_SAFE_INT, JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import parse_entity_ref, ref_text
from graph_cli.kinetic.format import ParamDef
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import coerce_text
from graph_cli.query.cypher.errors import did_you_mean
from graph_cli.query.cypher.values import Value

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
    function: str,
    params: Sequence[ParamDef],
    pairs: Sequence[tuple[str, str]],
    model: CompiledModel,
) -> dict[str, Value]:
    """Values from ``name=value`` pairs; a ``many`` parameter repeats its name."""
    grouped: dict[str, list[str]] = {}
    for name, text in pairs:
        grouped.setdefault(name, []).append(text)
    _check_names(function, params, grouped)
    bound: dict[str, Value] = {}
    for param in params:
        if param.name not in grouped:
            bound[param.name] = _default(function, param, model)
            continue
        texts = grouped[param.name]
        if not param.many and len(texts) > 1:
            raise _invalid(function, param, "is given more than once")
        values = [
            _checked(function, param, partial(_from_text, param, text, model)) for text in texts
        ]
        bound[param.name] = values if param.many else values[0]
    return bound


def bind_json(
    function: str,
    params: Sequence[ParamDef],
    values: Mapping[str, JsonValue],
    model: CompiledModel,
) -> dict[str, Value]:
    """Values from a JSON object; a ``many`` parameter takes a list."""
    _check_names(function, params, values)
    bound: dict[str, Value] = {}
    for param in params:
        if param.name not in values:
            bound[param.name] = _default(function, param, model)
            continue
        given = values[param.name]
        if not param.many:
            bound[param.name] = _checked(function, param, partial(_from_json, param, given, model))
        elif isinstance(given, list):
            bound[param.name] = [
                _checked(function, param, partial(_from_json, param, v, model)) for v in given
            ]
        else:
            raise _invalid(function, param, "expects a list")
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


def _check_names(function: str, params: Sequence[ParamDef], given: Mapping[str, object]) -> None:
    known = [p.name for p in params]
    for name in given:
        if name not in known:
            raise GraphCliError(
                "function.parameter_unknown",
                f"{function} has no parameter {name!r}"
                + (f"; it takes {', '.join(known)}" if known else " and takes no parameters"),
                details={
                    "function": function,
                    "parameter": name,
                    "expected": list(known),
                    "did_you_mean": did_you_mean(name, known),
                },
            )


def _default(function: str, param: ParamDef, model: CompiledModel) -> Value:
    if param.default is None:
        raise GraphCliError(
            "function.parameter_missing",
            f"{function} needs the parameter {param.name!r} ({param.type_text})",
            details={"function": function, "parameter": param.name, "type": param.type_text},
        )
    if param.many and isinstance(param.default, list):
        return [
            _checked(function, param, partial(_from_json, param, v, model)) for v in param.default
        ]
    return _checked(function, param, partial(_from_json, param, param.default, model))


def _invalid(function: str, param: ParamDef, message: str) -> GraphCliError:
    return GraphCliError(
        "function.parameter_invalid",
        f"parameter {param.name!r} of {function} {message}",
        details={
            "function": function,
            "parameter": param.name,
            "type": param.type_text,
            "reason": message,
        },
    )


def _checked(function: str, param: ParamDef, read: Callable[[], str | int | bool]) -> Value:
    try:
        value = read()
    except ValueError as exc:
        raise _invalid(function, param, f"expects {param.type_text}: {exc}") from None
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
