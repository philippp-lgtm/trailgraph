"""The ``graph`` commands, declared as data (contracts B11.1)."""

from __future__ import annotations

from graph_cli.cli_kit.spec import CommandSpec


def all_specs() -> tuple[CommandSpec, ...]:
    from graph_cli.app_cli.commands import (
        apply,
        connect,
        do,
        files,
        meta,
        read,
        self_update,
        skills,
        write,
    )

    return (
        *meta.SPECS,
        *read.SPECS,
        *write.SPECS,
        *do.SPECS,
        *apply.SPECS,
        *files.SPECS,
        *skills.SPECS,
        *self_update.SPECS,
        *connect.SPECS,
    )
