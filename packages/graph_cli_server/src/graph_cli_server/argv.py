"""A command request's parameters as argv for the one click parser (contracts B14.4).

``args`` names parameters by their inventory names (``"type"``, ``"--set"``,
``"--dry-run"``). Options come first, a repeatable option once per value, a
flag without value; then ``--`` and the positionals in declaration order, so
a positional that starts with ``-`` stays a positional. ``false`` and
``null`` mean "not given". Parsing, defaults and usage errors are the click
program's, exactly as for the CLI. A ``file`` parameter is resolved by the
caller (an upload); a ``directory`` parameter has no meaning on the server.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

END_OF_OPTIONS: Final = "--"

FileValue = Callable[[ParamSpec, JsonValue], str]


def to_argv(spec: CommandSpec, args: Mapping[str, JsonValue], file_value: FileValue) -> list[str]:
    """The argv after the program name; ``usage.invalid_argument`` for what cannot be one."""
    known = {param.name for param in spec.params}
    unknown = sorted(set(args) - known)
    if unknown:
        raise _usage(unknown[0], f"unknown parameter; the command takes {', '.join(sorted(known))}")
    options: list[str] = []
    positionals: list[str] = []
    for param in spec.params:
        value = args.get(param.name)
        if value is None or value is False:
            continue
        if param.type == "flag":
            if value is not True:
                raise _usage(param.name, "a flag takes true or false")
            options.append(param.name)
            continue
        if param.repeatable and not isinstance(value, list):
            raise _usage(param.name, "takes a list of values")
        values = value if isinstance(value, list) and param.repeatable else [value]
        texts = [_text(param, item, file_value) for item in values]
        if param.positional:
            positionals += texts
        else:
            for text in texts:
                options += [param.name, text]
    tail = [END_OF_OPTIONS, *positionals] if positionals else []
    return [*spec.path, *options, *tail]


def _text(param: ParamSpec, value: JsonValue, file_value: FileValue) -> str:
    if param.type == "file":
        return file_value(param, value)
    if param.type == "directory":
        raise _usage(param.name, "a directory is not available on the server")
    if isinstance(value, bool):
        raise _usage(param.name, "expected a value, not true or false")
    if param.type == "integer":
        if not isinstance(value, int):
            raise _usage(param.name, "expected an integer")
        return str(value)
    if not isinstance(value, str):
        raise _usage(param.name, "expected a string")
    return value


def _usage(name: str, message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", f"{name}: {message}", details={"argument": name})
