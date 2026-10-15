"""Running a click program under the envelope contract (contracts B9.1, B9.4, B9.10, B9.13).

``run`` owns every exit path: a command's ``Result``, a ``GraphCliError``,
a click usage error (``usage.*``, exit 2) and any other exception
(``internal.error``, exit 6; a trace on stderr only in debug mode). cli_kit
reads no environment: the composition root passes JSON mode and debug in
an ``Invocation``. ``--json`` is honoured anywhere in argv, so even a usage
error renders as an envelope.
"""

from __future__ import annotations

import functools
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Final

import click

from graph_cli.cli_kit.envelope import Result, envelope, render_json, render_text
from graph_cli.cli_kit.exit_codes import EXIT_OK, exit_code_for
from graph_cli.kernel.errors import GraphCliError, WarningRecord

JSON_FLAG: Final = "--json"


@dataclass(frozen=True, slots=True)
class Invocation:
    program: str
    json_default: bool = False  # GRAPH_CLI_OUTPUT=json or config [output] default
    debug: bool = False  # GRAPH_CLI_DEBUG=1


@dataclass
class CommandState:
    """The click context object: what the running command did and what it may use."""

    invocation: Invocation
    services: Any = None  # set by the program (lazy composition root)
    command: str | None = None
    result: Result | None = None
    warnings: list[WarningRecord] = field(default_factory=list)


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
) -> int:
    state = CommandState(invocation, services)
    json_mode = invocation.json_default or JSON_FLAG in argv
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
    document = envelope(
        state.command,
        program=invocation.program,
        result=state.result,
        error=error,
        warnings=[*state.warnings, *(error.warnings if error is not None else ())],
    )
    if json_mode:
        out.write(render_json(document))
    else:
        stdout, stderr = render_text(document, text=state.result.text if state.result else None)
        out.write(stdout)
        err.write(stderr)
    return exit_code_for(error.category) if error is not None else EXIT_OK


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
