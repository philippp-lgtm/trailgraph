"""The command inventory ``graph_cli.inventory/1`` (contracts B11.2, B11.3, B11.6-B11.8).

A pure function of the declarations and the package version: sorted by
path, no timestamps, paths or ids. It is the only input of skill
generators, so it lists every command an agent can run and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from graph_cli.cli_kit.exit_codes import EXIT_OK, exit_code_for
from graph_cli.cli_kit.spec import CommandSpec, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.error_codes import ErrorCategory, lookup_error_code

INVENTORY_SCHEMA: Final = "graph_cli.inventory/1"
# Every command can fail on usage, an unavailable backend and a bug.
COMMON_ERRORS: Final = ("usage.invalid_argument", "backend.unavailable", "internal.error")
# B14.10: what every command the server runs can meet in the remote mode; a write also
# a lost answer.
REMOTE_ERRORS: Final = (
    "remote.unreachable",
    "remote.unauthorized",
    "remote.protocol",
    "access.project_mismatch",
)
REMOTE_WRITE_ERRORS: Final = ("remote.outcome_unknown",)


def _codes(spec: CommandSpec, remote: bool = False) -> list[str]:
    extra: tuple[str, ...] = ()
    if remote and spec.remote != "client":
        extra = (*REMOTE_ERRORS, *(REMOTE_WRITE_ERRORS if spec.kind == "write" else ()))
    return sorted({*spec.errors, *COMMON_ERRORS, *extra})


def _exit(code: str) -> int:
    found = lookup_error_code(code)
    if found is None or found.category is ErrorCategory.WARNING:
        raise ValueError(f"{code!r} is not a registered error code")
    return exit_code_for(found.category)


def _command(spec: CommandSpec, remote: bool) -> dict[str, JsonValue]:
    codes = _codes(spec, remote)
    command: dict[str, JsonValue] = {
        "path": list(spec.path),
        "kind": spec.kind,
        "surface": spec.surface,
        "action": spec.action,
        "summary": spec.summary,
        "args": [p.to_json() for p in spec.params if p.positional],
        "options": [p.to_json() for p in spec.params if not p.positional],
        "examples": [e.to_json() for e in spec.examples],
        "errors": list(codes),
        "exit_codes": sorted({EXIT_OK, *(_exit(c) for c in codes)}),
        "limits": dict(spec.limits),
    }
    if remote:
        command["remote"] = spec.remote
    return command


def build_inventory(
    specs: Sequence[CommandSpec],
    *,
    program: str,
    version: str,
    surface: str,
    global_options: Sequence[ParamSpec] = (),
    remote: bool = False,
) -> dict[str, JsonValue]:
    """``remote`` adds each command's ``remote`` value (B14: the app surface)."""
    ordered = sorted(specs, key=lambda s: s.path)
    codes = sorted({c for spec in ordered for c in _codes(spec, remote)})
    return {
        "schema": INVENTORY_SCHEMA,
        "program": program,
        "program_version": version,
        "surface": surface,
        "global_options": [p.to_json() for p in global_options],
        "commands": [_command(spec, remote) for spec in ordered],
        "actions": sorted({spec.action for spec in ordered if spec.action is not None}),
        "error_codes": [
            {"code": code, "category": _category(code), "exit": _exit(code)} for code in codes
        ],
    }


def _category(code: str) -> str:
    found = lookup_error_code(code)
    assert found is not None
    return found.category.value
