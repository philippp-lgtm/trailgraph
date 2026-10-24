"""Running a click program under the envelope contract (contracts B9.1, B9.4, B9.10, B9.13).

``run`` owns every exit path: a command's ``Result``, a ``GraphCliError``,
a click usage error (``usage.*``, exit 2) and any other exception
(``internal.error``, exit 6; a trace on stderr only in debug mode). cli_kit
reads no environment: the composition root passes JSON mode and debug in
an ``Invocation``. ``--json`` is honoured anywhere in argv, so even a usage
error renders as an envelope. Global options given before the subcommand
reach the command as if given after it (``leaf``). After the envelope is
written, an optional ``after`` callback learns what was delivered
(``Completed``), for the access log; rendering and writing the envelope is
the ``render`` phase of the given ``Timings``. A JSON envelope larger than
the ``Invocation``'s spill limit goes to a file (``cli_kit.spill``). A
command whose answer was rendered elsewhere (``Result.forwarded``: the
server, B14.10) is written as it came, after the local warnings; its exit
code follows its error category.
"""

from __future__ import annotations

import functools
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Final

import click

from graph_cli.cli_kit.envelope import (
    Forwarded,
    Result,
    envelope,
    render_json,
    render_text,
    warning_lines,
)
from graph_cli.cli_kit.exit_codes import EXIT_OK, exit_code_for
from graph_cli.cli_kit.spill import SpillTarget, spill_json
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.timing import Timings

JSON_FLAG: Final = "--json"


@dataclass(frozen=True, slots=True)
class Invocation:
    program: str
    json_default: bool = False  # GRAPH_CLI_OUTPUT=json or config [output] default
    debug: bool = False  # GRAPH_CLI_DEBUG=1
    spill: SpillTarget | None = None  # large JSON answers go to a file (cli_kit.spill)


@dataclass
class CommandState:
    """The click context object: what the running command did and what it may use."""

    invocation: Invocation
    services: Any = None  # set by the program (lazy composition root)
    json_mode: bool = False  # how the answer will be written (run() decides)
    command: str | None = None
    result: Result | None = None
    warnings: list[WarningRecord] = field(default_factory=list)
    global_values: dict[str, Any] = field(default_factory=dict)  # option name -> value


@dataclass(frozen=True, slots=True)
class Completed:
    """A command whose envelope was written: the envelope and the bytes on stdout."""

    document: Mapping[str, JsonValue]
    json_mode: bool
    stdout_bytes: int
    exit_code: int
    spilled_bytes: int | None = None  # the answer's size when it went to a file
    text: str | None = None  # the command's own text rendering, if any (B14.5)


json_option = click.option(
    JSON_FLAG, "json_mode", is_flag=True, help="Write one JSON envelope to stdout."
)


def leaf(fn: Callable[..., Result]) -> Callable[..., None]:
    """Wrap a command callback: record its name and ``Result``; ``--json`` is run()'s."""

    @functools.wraps(fn)
    def callback(*args: Any, **kwargs: Any) -> None:
        ctx = click.get_current_context()
        state = ctx.find_object(CommandState)
        assert state is not None, "run() provides the CommandState"
        state.command = _command_name(ctx)
        _take_global_values(ctx, state, kwargs)
        kwargs.pop("json_mode", None)
        state.result = fn(state, *args, **kwargs)

    return callback


def run(
    program: click.Command,
    argv: Sequence[str],
    invocation: Invocation,
    *,
    services: Any = None,
    out: IO[str],
    err: IO[str],
    after: Callable[[Completed], None] | None = None,
    timings: Timings | None = None,
) -> int:
    json_mode = invocation.json_default or JSON_FLAG in argv
    state = CommandState(invocation, services, json_mode)
    error: GraphCliError | None = None
    try:
        code = program.main(
            args=list(argv), prog_name=invocation.program, standalone_mode=False, obj=state
        )
        if isinstance(code, int) and state.result is None:
            return code  # --help, --version
        if state.result is not None:
            error = state.result.error
    except click.UsageError as exc:
        if exc.ctx is not None and state.command is None:
            state.command = _command_name(exc.ctx) or None
        error = GraphCliError(_usage_code(exc), exc.format_message())
        if not json_mode:
            exc.show(file=err)
            return exit_code_for(error.category)
    except click.Abort:
        error = GraphCliError("usage.invalid_input", "aborted")
    except GraphCliError as exc:
        error = exc
    except Exception as exc:
        if invocation.debug:
            err.write(traceback.format_exc())
        error = GraphCliError("internal.error", f"unexpected {type(exc).__name__}")
    forwarded = state.result.forwarded if state.result is not None and error is None else None
    with (timings or Timings()).phase("render"):
        if forwarded is not None:
            document, error_category = _forwarded(forwarded, state.warnings)
        else:
            document = envelope(
                state.command,
                program=invocation.program,
                result=state.result,
                error=error,
                warnings=[*state.warnings, *(error.warnings if error is not None else ())],
            )
            error_category = error.category if error is not None else None
        spilled = None
        if json_mode:
            stdout = render_json(document)
            if invocation.spill is not None:
                rendered = spill_json(document, stdout, invocation.spill)
                document, stdout, spilled = (
                    rendered.document,
                    rendered.stdout,
                    rendered.spilled_bytes,
                )
            out.write(stdout)
        elif forwarded is not None and forwarded.stdout is not None:
            stdout = forwarded.stdout
            out.write(stdout)
            local = [w.to_json() for w in state.warnings]
            err.write("".join(line + "\n" for line in warning_lines(local)))
            err.write(forwarded.stderr or "")
        else:
            stdout, stderr = render_text(document, text=state.result.text if state.result else None)
            out.write(stdout)
            err.write(stderr)
    exit_code = exit_code_for(error_category) if error_category is not None else EXIT_OK
    if after is not None:
        text = state.result.text if state.result is not None else None
        after(Completed(document, json_mode, len(stdout.encode()), exit_code, spilled, text))
    return exit_code


def _forwarded(
    forwarded: Forwarded, warnings: list[WarningRecord]
) -> tuple[Mapping[str, JsonValue], ErrorCategory | None]:
    """The forwarded envelope with the local warnings first, and its error category."""
    theirs = forwarded.document.get("warnings")
    document = {
        **forwarded.document,
        "warnings": [
            *(w.to_json() for w in warnings),
            *(theirs if isinstance(theirs, list) else []),
        ],
    }
    error = document.get("error")
    if not isinstance(error, Mapping):
        return document, None
    try:
        return document, ErrorCategory(str(error.get("category")))
    except ValueError:
        return document, ErrorCategory.INTERNAL


def _take_global_values(ctx: click.Context, state: CommandState, kwargs: dict[str, Any]) -> None:
    """Hand options given before the subcommand to the command's own parameters."""
    for option, value in state.global_values.items():
        param = next((p for p in ctx.command.params if option in p.opts), None)
        if param is None or param.name is None:
            raise click.NoSuchOption(option, ctx=ctx)
        given = kwargs.get(param.name)
        if isinstance(param, click.Option) and param.multiple:
            values = tuple(given or ())
            kwargs[param.name] = values if value in values else (value, *values)
        elif given is None or given is False:
            kwargs[param.name] = value
        elif given != value:
            raise click.UsageError(f"{option} is given twice with different values.", ctx=ctx)


def _command_name(ctx: click.Context) -> str:
    return " ".join(ctx.command_path.split()[1:])


def _usage_code(exc: click.UsageError) -> str:
    if isinstance(exc, click.MissingParameter):
        return "usage.missing_argument"
    if isinstance(exc, click.NoSuchOption) or exc.message.startswith("No such command"):
        return (
            "usage.unknown_command"
            if not isinstance(exc, click.NoSuchOption)
            else "usage.invalid_argument"
        )
    return "usage.invalid_argument"
