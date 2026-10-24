"""``graph-admin policy check|show|diff|apply|explain``: who may use which graph (B6.16)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    GRAPH_OPTION,
    admin_context,
    path,
)
from graph_cli_admin.services_admin import policy

FILE: Final = ParamSpec("file", "file", "The authored policy.", required=True, positional=True)
POLICY_GRAPH: Final = ParamSpec(
    "--graph", "graph_ref", "The graph of a graph policy (name or id).", keyword="graph_ref"
)
# Compiling an authored policy: unreadable file, wrong layer for --graph, invalid policy.
SOURCE_ERRORS: Final = (*GRAPH_ERRORS, "usage.missing_argument", "policy.invalid")


def check(state: CommandState, file: str, graph_ref: str | None, data_root: str | None) -> Result:
    outcome = policy.check(admin_context(state, data_root), file=Path(file), graph_ref=graph_ref)
    return Result(data=outcome.data, warnings=outcome.warnings)


def show(state: CommandState, graph_ref: str | None, data_root: str | None) -> Result:
    outcome = policy.show(admin_context(state, data_root), graph_ref=graph_ref)
    return Result(data=outcome.data)


def diff(state: CommandState, file: str, graph_ref: str | None, data_root: str | None) -> Result:
    outcome = policy.diff(admin_context(state, data_root), file=Path(file), graph_ref=graph_ref)
    return Result(data=outcome.data, warnings=outcome.warnings)


def apply(
    state: CommandState, file: str, graph_ref: str | None, reason: str, data_root: str | None
) -> Result:
    outcome = policy.apply(
        admin_context(state, data_root), file=Path(file), graph_ref=graph_ref, reason=reason
    )
    return Result(data=outcome.data, warnings=outcome.warnings)


def explain(
    state: CommandState,
    graph_ref: str,
    project: str | None,
    action: str | None,
    type_curie: str | None,
    file: str | None,
    token: str | None,
    data_root: str | None,
) -> Result:
    outcome = policy.explain(
        admin_context(state, data_root),
        graph_ref=graph_ref,
        project=project,
        action=action,
        type_curie=type_curie,
        file=path(file),
        token=token,
    )
    return Result(data=outcome.data, warnings=outcome.warnings)


SPECS: Final = (
    CommandSpec(
        path=("policy", "check"),
        kind="read",
        surface="meta",
        action=None,
        summary="Compile an authored policy and report every problem; installs nothing.",
        params=(FILE, POLICY_GRAPH, DATA_ROOT),
        examples=(
            Example("graph-admin policy check <file>", "Check the installation policy."),
            Example(
                "graph-admin policy check <file> --graph <graph> --json",
                "Check a graph policy, as JSON.",
            ),
        ),
        errors=SOURCE_ERRORS,
        handler=check,
        description="The file's id says its layer: installation (one per installation) or"
        " graph (needs --graph).",
        remote="hooked",
    ),
    CommandSpec(
        path=("policy", "show"),
        kind="read",
        surface="meta",
        action=None,
        summary="Show the installed compiled policy and its hash (the built-in one if none).",
        params=(POLICY_GRAPH, DATA_ROOT),
        examples=(
            Example("graph-admin policy show", "The installation policy."),
            Example("graph-admin policy show --graph <graph> --json", "A graph policy as JSON."),
        ),
        errors=GRAPH_ERRORS,
        handler=show,
    ),
    CommandSpec(
        path=("policy", "diff"),
        kind="read",
        surface="meta",
        action=None,
        summary="Compare an authored policy with the installed one, rule by rule.",
        params=(FILE, POLICY_GRAPH, DATA_ROOT),
        examples=(
            Example("graph-admin policy diff <file>", "Compare the installation policy."),
            Example(
                "graph-admin policy diff <file> --graph <graph> --json",
                "Compare a graph policy, as JSON.",
            ),
        ),
        errors=SOURCE_ERRORS,
        handler=diff,
        remote="hooked",
    ),
    CommandSpec(
        path=("policy", "apply"),
        kind="write",
        surface="act",
        action="maint.policy_change",
        summary="Install an authored policy (audited as maint.policy_change).",
        params=(
            FILE,
            POLICY_GRAPH,
            ParamSpec(
                "--reason",
                "string",
                "Why the policy changes; recorded in the audit.",
                required=True,
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin policy apply <file> --reason <reason>", "Install it."),
            Example(
                "graph-admin policy apply <file> --graph <graph> --reason <reason> --json",
                "Install a graph policy, as JSON.",
            ),
        ),
        errors=(*SOURCE_ERRORS, *CHANGE_ERRORS, "admin.policy_lockout"),
        handler=apply,
        description="Refused when it would stop you from changing policies again. Applying"
        " the installed policy again changes nothing.",
        remote="hooked",
    ),
    CommandSpec(
        path=("policy", "explain"),
        kind="read",
        surface="meta",
        action=None,
        summary="What an app caller from a project would get on a graph, and by which rule.",
        params=(
            GRAPH_OPTION,
            ParamSpec("--project", "string", "The project of the caller; without it: no project."),
            ParamSpec(
                "--token",
                "string",
                "The name of a token (never the token): a server request with it instead, its"
                " project verified; for an admin token the project admin's actions.",
            ),
            ParamSpec(
                "--action", "string", "One action, e.g. write.create; without it: every app action."
            ),
            ParamSpec(
                "--type",
                "term",
                "Decide on a type of the graph, e.g. files:File.",
                keyword="type_curie",
            ),
            ParamSpec("--file", "file", "Explain an authored policy instead of the installed one."),
            DATA_ROOT,
        ),
        examples=(
            Example(
                "graph-admin policy explain --graph <graph> --project <value>",
                "What a project's callers get.",
            ),
            Example(
                "graph-admin policy explain --graph <graph> --action write.create --type <type>",
                "One action on one type.",
            ),
            Example(
                "graph-admin policy explain --graph <graph> --token <value>",
                "What a token's requests get, e.g. whether a project admin maintains the graph.",
            ),
        ),
        errors=(*SOURCE_ERRORS, "admin.token_not_found"),
        handler=explain,
        description="Records nothing.",
        remote="hooked",
    ),
)
