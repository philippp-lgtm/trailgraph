"""Commands declared as data (contracts B11.1, B11.3, B9.12).

A ``CommandSpec`` names a command's path, kind, surface, action, summary,
parameters, examples and the error codes it may raise. ``build_click``
generates the click tree from the declarations and ``inventory`` describes
the same declarations, so help, parsing and the inventory cannot drift.
Every command gets ``--json``; ``--help`` shows the summary and the examples.
Global options are accepted before the subcommand as well as after it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import click

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState, json_option, leaf
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import EMPTY

ParamType = Literal[
    "string",
    "integer",
    "flag",
    "choice",
    "term",
    "entity_ref",
    "graph_ref",
    "assignment",
    "json_assignment",
    "filter",
    "file",
    "directory",
]
Kind = Literal["read", "write", "meta"]
Surface = Literal["model", "retrieve", "act", "trace", "meta"]


@dataclass(frozen=True, slots=True)
class ParamSpec:
    name: str  # option name with dashes ("--set") or argument name ("type")
    type: ParamType
    summary: str
    required: bool = False
    repeatable: bool = False
    positional: bool = False
    choices: tuple[str, ...] = ()
    default: JsonValue = None
    keyword: str | None = None  # the handler's keyword when the name would clash

    @property
    def dest(self) -> str:
        """The handler's keyword argument."""
        return self.keyword or self.name.lstrip("-").replace("-", "_")

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "repeatable": self.repeatable,
            "summary": self.summary,
        }
        if self.choices:
            data["choices"] = list(self.choices)
        if self.default is not None:
            data["default"] = self.default
        return data


@dataclass(frozen=True, slots=True)
class Example:
    command: str
    summary: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"command": self.command, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class CommandSpec:
    path: tuple[str, ...]
    kind: Kind
    surface: Surface
    action: str | None
    summary: str  # one line, at most 100 chars
    params: tuple[ParamSpec, ...]
    examples: tuple[Example, ...]
    errors: tuple[str, ...]
    handler: Callable[..., Result] = field(compare=False, repr=False)
    limits: Mapping[str, JsonValue] = field(default=EMPTY)
    description: str | None = None


def _click_param(param: ParamSpec, **extra: Any) -> click.Parameter:
    if param.positional:
        return click.Argument(
            [param.dest],
            required=param.required,
            nargs=-1 if param.repeatable else 1,
            type=click.INT if param.type == "integer" else click.STRING,
        )
    kwargs: dict[str, Any] = {"help": param.summary, "required": param.required}
    if param.type == "flag":
        kwargs.update(is_flag=True, default=False)
    elif param.type == "choice":
        kwargs.update(type=click.Choice(param.choices), default=param.default)
    elif param.type == "integer":
        kwargs.update(type=click.INT, default=param.default)
    elif param.type == "directory":
        kwargs.update(type=click.Path(file_okay=False), default=param.default)
    else:
        kwargs.update(default=param.default)
    if param.repeatable:
        kwargs.update(multiple=True, default=())
    if param.required:
        kwargs.pop("default", None)  # click treats an explicit default as given
    return click.Option([param.name, param.dest], **kwargs, **extra)


def _global_option(param: ParamSpec) -> click.Parameter:
    """The option on the root group; ``leaf`` hands its value to the command."""

    def keep(ctx: click.Context, _param: click.Parameter, value: Any) -> None:
        if value is not None and value is not False:
            state = ctx.find_object(CommandState)
            assert state is not None, "run() provides the CommandState"
            state.global_values[param.name] = value

    return _click_param(param, expose_value=False, callback=keep)


def _help(spec: CommandSpec) -> str:
    lines = [spec.summary]
    if spec.description:
        lines += ["", spec.description]
    lines += ["", "\b", "Examples:"] + [f"  {e.command}" for e in spec.examples]
    return "\n".join(lines)


def _command(spec: CommandSpec) -> click.Command:
    def call(state: CommandState, **kwargs: Any) -> Result:
        return spec.handler(state, **kwargs)

    callback = leaf(call)
    command = click.Command(
        spec.path[-1],
        callback=callback,
        params=[_click_param(p) for p in spec.params],
        help=_help(spec),
        short_help=spec.summary,
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    return json_option(command)


def build_click(
    group: click.Group,
    specs: Sequence[CommandSpec],
    *,
    group_help: Mapping[str, str] | None = None,
    global_options: Sequence[ParamSpec] = (),
) -> click.Group:
    """Attach one click command per spec under ``group``, creating subgroups by path.

    ``global_options`` are added to ``group`` too, so they may stand before the
    subcommand; every command that takes them must declare them itself.
    """
    for option in global_options:
        group.params.append(_global_option(option))
    for spec in specs:
        parent = group
        for part in spec.path[:-1]:
            child = parent.commands.get(part)
            if child is None:
                child = click.Group(part, help=(group_help or {}).get(part, f"{part} commands."))
                parent.add_command(child)
            assert isinstance(child, click.Group), f"{part} is a command and a group"
            parent = child
        parent.add_command(_command(spec))
    return group
