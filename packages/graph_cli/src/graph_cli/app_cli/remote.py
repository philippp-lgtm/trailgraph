"""The remote mode of ``graph``: commands answered by graph-server (contracts B14.10).

The click tree is the same as locally, so argv is parsed here and usage
errors need no server. Every command the server runs (``remote`` is
``server`` or ``hooked``) gets a handler that sends its parsed parameters
by its canonical API name and returns the server's answer as it came
(``Result.forwarded``); ``client`` commands keep their own handler. A
write (not ``--dry-run``) always carries an idempotency key: the given one
or a generated ``r-<32 hex>``, so a write whose answer was lost can be run
again safely (``remote.outcome_unknown`` names the key and the command). The
key travels in the ``Idempotency-Key`` header, not in ``args`` (B14.22 G7).
Hooked commands add work around the call (``app_cli.remote_hooks``).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Final

from graph_cli.app_cli.commands.common import PROGRAM, services
from graph_cli.catalog.remote import RemoteSession, Write
from graph_cli.cli_kit.argv import END_OF_OPTIONS, api_args, request_args, to_argv
from graph_cli.cli_kit.envelope import Forwarded, Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import IDEMPOTENCY_KEY_RE

KEY_OPTION: Final = "--idempotency-key"
Hook = Callable[[RemoteSession, CommandSpec, CommandState, dict[str, JsonValue]], Result]


def remote_specs(specs: Sequence[CommandSpec]) -> tuple[CommandSpec, ...]:
    """The declarations with the remote handlers in place of the local ones."""
    from graph_cli.app_cli.remote_hooks import HOOKS

    found: list[CommandSpec] = []
    for spec in specs:
        if spec.remote == "client":
            found.append(spec)
        else:
            found.append(replace(spec, handler=_handler(spec, HOOKS.get(spec.path))))
    return tuple(found)


def _handler(spec: CommandSpec, hook: Hook | None) -> Callable[..., Result]:
    def handler(state: CommandState, **values: Any) -> Result:
        session = services(state).remote(state.warnings)
        args = request_args(spec, values)
        if hook is not None:
            return hook(session, spec, state, args)
        return forward(session, spec, state, args)

    return handler


def forward(
    session: RemoteSession,
    spec: CommandSpec,
    state: CommandState,
    args: dict[str, JsonValue],
    *,
    key: bool = True,
) -> Result:
    """Run ``spec`` on the server; ``key`` generates an idempotency key for a write."""
    write = write_of(spec, args, generate=key)
    header = None
    given = args.get(KEY_OPTION)
    # a malformed key stays in args, so the server refuses it exactly as the CLI does
    if isinstance(given, str) and IDEMPOTENCY_KEY_RE.fullmatch(given):
        header = given
        del args[KEY_OPTION]
    document, stdout, stderr = session.command(
        spec.path, api_args(args), text=not state.json_mode, write=write, idempotency_key=header
    )
    return Result(forwarded=Forwarded(document, stdout, stderr))


def write_of(spec: CommandSpec, args: dict[str, JsonValue], *, generate: bool) -> Write | None:
    """What a lost answer must name, for a write; adds a generated key to ``args``."""
    if spec.kind != "write" or args.get("--dry-run") is True:
        return None
    takes_key = any(p.name == KEY_OPTION for p in spec.params)
    if takes_key and generate and not isinstance(args.get(KEY_OPTION), str):
        args[KEY_OPTION] = new_key()
    key = args.get(KEY_OPTION)
    return Write(key if isinstance(key, str) else None, command_line(spec, args))


def new_key() -> str:
    return "r-" + secrets.token_hex(16)


def command_line(spec: CommandSpec, args: dict[str, JsonValue]) -> list[str]:
    """The arguments that repeat a command (after the program name), as a person types
    them: positionals first, ``--`` only when one looks like an option, so options can
    still be added at the end. An upload (of a file or a directory) is named by the
    argument it came from."""

    def named(param: ParamSpec, value: JsonValue) -> str:
        name = value.get("name") if isinstance(value, dict) else value
        return str(name)

    argv = to_argv(spec, args, named, directories=True)
    if END_OF_OPTIONS not in argv:
        return argv
    cut = argv.index(END_OF_OPTIONS)
    positionals = argv[cut + 1 :]
    if any(p.startswith("-") for p in positionals):
        return argv
    head = len(spec.path)
    return [*argv[:head], *positionals, *argv[head:cut]]


__all__ = ["PROGRAM", "forward", "remote_specs"]
