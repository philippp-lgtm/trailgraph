"""Command parameters of a request, and argv for the one click parser (B14.4, B14.10, B14.22).

``request_args`` is the remote client's half: the parsed values of a
command, keyed by the parameters' inventory names; ``api_args`` renames them
to the canonical API names a request carries. ``to_argv`` is the server's
half and turns them back into argv.

``args`` names a parameter by its canonical API name (``api_name``:
``"type"``, ``"set"``, ``"dry_run"``) or, within ``graph_cli.api/1``, by its
inventory name (``"--set"``, ``"--dry-run"``); one parameter under both names
is refused. Options come first, a repeatable option once per value, a flag
without value; then ``--`` and the positionals in declaration order, so a
positional that starts with ``-`` stays a positional. ``false`` and ``null``
mean "not given". Parsing, defaults and usage errors are the click
program's, exactly as for the CLI. A ``file`` parameter is resolved by the
caller (an upload); a ``directory`` parameter has no meaning on the server
unless the caller resolves it too (``directories``: the admin API's
uploaded directories).

Next to the CLI form (a string, or a list of strings for a repeatable
parameter, meaning what it means on the command line), structured values
are turned into the argv of that CLI form (G6): a pair parameter
(``ParamSpec.pairs``) takes ``{name: value}`` (``map``) or ``{name:
[value, ...]}`` (``multimap``), its values written as text or as JSON
(``ParamSpec.pair_encoding``); a filter item takes ``{field, op?, value}``.
A request body is parsed with float literals kept (``FloatLiteral``), so a
number such as ``1.10`` reaches the parser as written.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.changeset import FloatLiteral, contains_float, float_numbers
from graph_cli.kernel.errors import GraphCliError

END_OF_OPTIONS: Final = "--"
_FILTER_KEYS: Final = frozenset({"field", "op", "value"})
_OP_RE: Final = re.compile(r"[a-z]+")

FileValue = Callable[[ParamSpec, JsonValue], str]


def api_name(name: str) -> str:
    """The canonical API name of a parameter: its inventory name without leading dashes,
    ``-`` as ``_`` (B14.22 G5)."""
    return name.lstrip("-").replace("-", "_")


def request_args(spec: CommandSpec, values: Mapping[str, Any]) -> dict[str, JsonValue]:
    """The parsed values of ``spec`` (by handler keyword) by inventory name; what was not
    given (``None``, ``False``, an empty repeat) is left out."""
    return spec.given_args(values)


def api_args(args: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """``args`` by inventory name, renamed to the canonical names a request sends."""
    return {api_name(name): value for name, value in args.items()}


def to_argv(
    spec: CommandSpec,
    args: Mapping[str, JsonValue],
    file_value: FileValue,
    *,
    directories: bool = False,
) -> list[str]:
    """The argv after the program name; ``usage.invalid_argument`` for what cannot be one.
    With ``directories``, ``file_value`` resolves directory parameters as well."""
    given = _by_param(spec, args)
    options: list[str] = []
    positionals: list[str] = []
    for param in spec.params:
        if param.name not in given:
            continue
        key, value = given[param.name]
        if value is None or value is False:
            continue
        if param.type == "flag":
            if value is not True:
                raise _usage(key, "a flag takes true or false")
            options.append(param.name)
            continue
        if param.pairs is not None and isinstance(value, Mapping):
            texts = _pairs(spec, param, key, value)
        else:
            if param.repeatable and not isinstance(value, list):
                raise _usage(key, "takes a list of values")
            values = value if isinstance(value, list) and param.repeatable else [value]
            texts = [_text(param, key, item, file_value, directories) for item in values]
        if param.positional:
            positionals += texts
        else:
            for text in texts:
                options += [param.name, text]
    tail = [END_OF_OPTIONS, *positionals] if positionals else []
    return [*spec.path, *options, *tail]


def inventory_args(spec: CommandSpec, args: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """``args`` by inventory name, whichever name the request used for each parameter."""
    return {name: value for name, (_, value) in _by_param(spec, args).items()}


def _by_param(spec: CommandSpec, args: Mapping[str, JsonValue]) -> dict[str, tuple[str, JsonValue]]:
    """Inventory name -> (the key the request used, its value)."""
    names: dict[str, ParamSpec] = {}
    for param in spec.params:
        names[param.name] = names[api_name(param.name)] = param
    found: dict[str, tuple[str, JsonValue]] = {}
    for key, value in args.items():
        if key not in names:
            takes = ", ".join(sorted(api_name(p.name) for p in spec.params))
            raise _usage(key, f"unknown parameter; the command takes {takes}")
        param = names[key]
        if param.name in found:
            raise _usage(key, f"given as {api_name(param.name)} and as {param.name}; name it once")
        found[param.name] = (key, value)
    return found


def _text(
    param: ParamSpec, key: str, value: JsonValue, file_value: FileValue, directories: bool
) -> str:
    if param.type == "file" or (param.type == "directory" and directories):
        return file_value(param, float_numbers(value))
    if param.type == "directory":
        raise _usage(key, "a directory is not available on the server")
    if param.type == "filter" and isinstance(value, Mapping):
        return _filter(key, value)
    if isinstance(value, bool):
        raise _usage(key, "expected a value, not true or false")
    if param.type == "integer":
        if not isinstance(value, int):
            raise _usage(key, "expected an integer")
        return str(value)
    if not isinstance(value, str) or isinstance(value, FloatLiteral):
        raise _usage(key, "expected a string")
    return value


def _pairs(
    spec: CommandSpec, param: ParamSpec, key: str, value: Mapping[str, JsonValue]
) -> list[str]:
    """``name=value`` per pair, in the given order; a ``multimap`` name once per value."""
    texts: list[str] = []
    for name, item in value.items():
        if not name or "=" in name:
            raise _usage(key, f"{name!r} is no name: it must be non-empty and without =")
        if param.pairs == "map":
            items: list[JsonValue] = [item]
        elif isinstance(item, list):
            items = item
        else:
            raise _usage(key, f"{name!r}: expected a list of values ({{name: [value, ...]}})")
        for one in items:
            if param.pair_encoding == "json":
                texts.append(f"{name}={_json_text(one)}")
                continue
            text = _scalar(one)
            if text is None:
                sibling = f"{param.name}-json"
                hint = (
                    f"; a typed value goes to {api_name(sibling)}"
                    if any(p.name == sibling for p in spec.params)
                    else ""
                )
                raise _usage(key, f"{name!r}: expected a string, number or boolean{hint}")
            texts.append(f"{name}={text}")
    return texts


def _filter(key: str, value: Mapping[str, JsonValue]) -> str:
    """``{field, op?, value}`` as ``field=value`` or ``field:op=value``."""
    unknown = sorted(set(value) - _FILTER_KEYS)
    if unknown:
        raise _usage(
            key, f"unknown filter keys: {', '.join(unknown)}; a filter takes field, op, value"
        )
    field, op, operand = value.get("field"), value.get("op"), value.get("value")
    op = "eq" if op is None else op
    if not isinstance(field, str) or not field or "=" in field or ":" in field:
        raise _usage(key, "field: expected a field name without = or :")
    if not isinstance(op, str) or not _OP_RE.fullmatch(op):
        raise _usage(key, "op: expected an operator name such as eq, lt or in")
    if op == "exists":
        if not isinstance(operand, bool):
            raise _usage(key, "exists takes true or false")
        text = "true" if operand else "false"
    elif op == "in":
        if not isinstance(operand, list) or not operand:
            raise _usage(key, "in takes a non-empty list of values")
        items = [_scalar(item) for item in operand]
        if any(item is None or "," in item for item in items):
            raise _usage(key, "in takes strings, numbers or booleans, none containing ,")
        text = ",".join(item for item in items if item is not None)
    else:
        scalar = _scalar(operand)
        if scalar is None:
            raise _usage(key, "value: expected a string, number or boolean")
        text = scalar
    return f"{field}={text}" if op == "eq" else f"{field}:{op}={text}"


def _scalar(value: JsonValue) -> str | None:
    """A string as it is, a number as its JSON literal, a boolean as true or false; ``None``
    for anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | str):
        return str(value)
    return None


def _json_text(value: JsonValue) -> str:
    """``value`` as canonical JSON (C.3); a float literal as written, so the parser sees
    the number the CLI form would carry (and refuses it where the CLI form is refused)."""
    if not contains_float(value):
        return canonical_bytes(value).decode()
    if isinstance(value, FloatLiteral):
        return str(value)
    if isinstance(value, Mapping):
        members = (f"{_json_text(k)}:{_json_text(v)}" for k, v in sorted(value.items()))
        return "{" + ",".join(members) + "}"
    assert isinstance(value, list | tuple), "contains_float is only true for these"
    return "[" + ",".join(_json_text(item) for item in value) + "]"


def _usage(name: str, message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", f"{name}: {message}", details={"argument": name})
