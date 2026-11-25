"""Commands declared as data (contracts B11.1, B11.3, B9.12).

A ``CommandSpec`` names a command's path, kind, surface, action, summary,
parameters, examples, the error codes it may raise and the schema of its
answer's ``data`` (``cli_kit.output_schemas``). ``build_click``
generates the click tree from the declarations and ``inventory`` describes
the same declarations, so help, parsing and the inventory cannot drift.
Every command gets ``--json``; ``--help`` shows the summary and the examples.
Global options are accepted before the subcommand as well as after it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

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
# B14: who runs a command when the program is a client of the server API. ``server``: the
# server runs it and the client passes it through; ``hooked``: the server runs it and the
# client adds work (uploads, downloads, origins); ``client``: only the client runs it;
# ``local``: only where the data lives, never over the API (graph-admin init and view).
Remote = Literal["server", "hooked", "client", "local"]
SERVED: Final = ("server", "hooked")  # the remote values the server runs
Surface = Literal["model", "retrieve", "act", "trace", "meta"]
# B14.22 G6: the structured form of a pair parameter (``name=value`` repeated). ``map``: a
# name at most once, ``{name: value}``; ``multimap``: a name may repeat, ``{name: [value]}``.
Pairs = Literal["map", "multimap"]
# How a structured pair value becomes the text after ``=``: ``text`` takes a string, number
# or boolean; ``json`` takes any JSON value and writes it as JSON.
PairValues = Literal["text", "json"]


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
    must_exist: bool = False  # a file or directory that must exist (a usage error otherwise)
    pairs: Pairs | None = None
    pair_values: PairValues | None = None  # default: json for json_assignment, else text

    @property
    def dest(self) -> str:
        """The handler's keyword argument."""
        return self.keyword or self.name.lstrip("-").replace("-", "_")

    @property
    def pair_encoding(self) -> PairValues:
        """How a structured value of this pair parameter becomes text (B14.22 G6)."""
        if self.pair_values is not None:
            return self.pair_values
        return "json" if self.type == "json_assignment" else "text"

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
        if self.pairs is not None:
            data["pairs"] = self.pairs
            data["pair_values"] = self.pair_encoding
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
    remote: Remote = "server"
    # B9.3: the JSON Schema (2020-12) of the answer's ``data``, ``$ref``s into shared ``$defs``
    output: Mapping[str, JsonValue] | None = None
    # False: the command is for programs (an agent harness), not for agents that work in a shell, so
    # the command table of skill 1's SKILL.md leaves it out; the generated reference, the
    # inventory and the OpenAPI document keep it
    skill_table: bool = True
    # False: the answer is never moved to a file when it is large (``cli_kit.spill``), for a command
    # whose answer is the data a program asked for and must read from stdout
    spill: bool = True
    # The kind of one invocation when it depends on the arguments (``call`` is a write for an
    # action and a read for a function or a view): the arguments by inventory name, as
    # ``given_args`` gives them. ``kind`` is then the strongest the command can be (a write), for
    # what is judged without arguments (the inventory); everything that decides per invocation
    # (the skill check, the idempotency key and the retry of the remote client, the context of
    # the command) asks ``kind_of``.
    kind_by_args: Callable[[Mapping[str, JsonValue]], Kind] | None = field(
        default=None, compare=False, repr=False
    )

    def kind_of(self, args: Mapping[str, JsonValue]) -> Kind:
        """The kind of an invocation with ``args`` (by inventory name)."""
        return self.kind if self.kind_by_args is None else self.kind_by_args(args)

    def given_args(self, values: Mapping[str, Any]) -> dict[str, JsonValue]:
        """The parsed values of a call (by handler keyword) by inventory name; what was not
        given (``None``, ``False``, an empty repeat) is left out."""
        args: dict[str, JsonValue] = {}
        for param in self.params:
            value = values.get(param.dest)
            if value is None or value is False or value == ():
                continue
            args[param.name] = list(value) if isinstance(value, tuple) else value
        return args


def _click_type(param: ParamSpec) -> click.ParamType[Any]:
    if param.type == "integer":
        return click.INT
    if param.type == "directory":
        return click.Path(file_okay=False, exists=param.must_exist)
    if param.type == "file" and param.must_exist:
        return click.Path(dir_okay=False, exists=True)
    return click.STRING


class _Positional(click.Argument):
    """A positional that shows in brackets in the usage line when it is not required (a custom
    metavar would otherwise hide that); errors keep the plain name."""

    def get_usage_pieces(self, ctx: click.Context) -> list[str]:
        (piece,) = super().get_usage_pieces(ctx)
        return [piece if self.required else f"[{piece}]"]


def _click_param(param: ParamSpec, **extra: Any) -> click.Parameter:
    if param.positional:
        return _Positional(
            [param.dest],
            required=param.required,
            nargs=-1 if param.repeatable else 1,
            type=_click_type(param),
            metavar=param.name.upper(),
        )
    kwargs: dict[str, Any] = {"help": param.summary, "required": param.required}
    if param.type == "flag":
        kwargs.update(is_flag=True, default=False)
    elif param.type == "choice":
        kwargs.update(type=click.Choice(param.choices), default=param.default)
    elif param.type in ("integer", "directory"):
        kwargs.update(type=_click_type(param), default=param.default)
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
        state.kind = (
            spec.kind if spec.kind_by_args is None else spec.kind_of(spec.given_args(kwargs))
        )
        state.spill = spec.spill
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
