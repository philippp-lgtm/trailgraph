"""The ``graph`` commands, declared as data (contracts B11.1)."""

from __future__ import annotations

from graph_cli.cli_kit.spec import CommandSpec


def all_specs() -> tuple[CommandSpec, ...]:
    from graph_cli.app_cli.commands import (
        apply,
        call,
        connect,
        describe,
        do,
        files,
        meta,
        read,
        self_update,
        skills,
        start,
        write,
    )

    return (
        *meta.SPECS,
        *describe.SPECS,
        *read.SPECS,
        *write.SPECS,
        *do.SPECS,
        *call.SPECS,
        *apply.SPECS,
        *files.SPECS,
        *skills.SPECS,
        *start.SPECS,
        *self_update.SPECS,
        *connect.SPECS,
    )
