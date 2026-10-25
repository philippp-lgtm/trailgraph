"""The ``graph-admin`` commands, declared as data (contracts B11.1)."""

from __future__ import annotations

from graph_cli.cli_kit.spec import CommandSpec


def all_specs() -> tuple[CommandSpec, ...]:
    from graph_cli_admin.maintenance_cli.commands import (
        audit,
        enroll,
        files,
        graph,
        init,
        meta,
        ontology,
        policy,
        self_update,
        skills,
        token,
        view,
        workspace,
    )

    return (
        *meta.SPECS,
        *init.SPECS,
        *graph.SPECS,
        *workspace.SPECS,
        *ontology.SPECS,
        *audit.SPECS,
        *files.SPECS,
        *policy.SPECS,
        *token.SPECS,
        *enroll.SPECS,
        *view.SPECS,
        *self_update.SPECS,
        *skills.SPECS,
    )
