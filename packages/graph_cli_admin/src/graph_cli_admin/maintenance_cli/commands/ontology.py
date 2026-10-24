"""``graph-admin ontology validate|diff|release``."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import RequestMeta
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    admin_context,
)
from graph_cli_admin.services_admin import graphs, ontology
from graph_cli_admin.services_admin.release import release

SOURCE: Final = ParamSpec(
    "source", "directory", "Directory with the authored ontology.", required=True, positional=True
)
MODULE: Final = ParamSpec(
    "--module", "string", "Main module id (the file <module>.yaml); needed if SOURCE holds several."
)


def validate(state: CommandState, source: str, module: str | None) -> Result:
    return Result(data=ontology.summary(ontology.compile_source(Path(source), module)))


def diff_command(
    state: CommandState,
    source: str,
    module: str | None,
    against: str | None,
    graph_ref: str | None,
    data_root: str | None,
) -> Result:
    if (against is None) == (graph_ref is None):
        raise GraphCliError(
            "usage.missing_argument",
            "give exactly one of --against or --graph",
            details={"argument": "against|graph"},
        )
    new = ontology.compile_source(Path(source), module)
    graph = None
    if graph_ref is not None:
        ctx = admin_context(state, data_root)
        entry = ctx.resolve(graph_ref, "ontology diff")
        ctx.authorize(
            [("maint.graph_configure", f"graph:{entry.id}")],
            request=RequestMeta.of("ontology diff", {"graph": graph_ref}),
            graph=entry.id,
        )
        old = ontology.pinned_model(ctx.catalog, entry)
        graph = {"id": str(entry.id), "name": entry.name, "state": entry.state}
    else:
        assert against is not None
        old = ontology.compile_source(Path(against), module).model
    report, bump = ontology.diff(old, new.model)
    return Result(data={**report.to_json(), "bump": bump}, graph=graph)


def release_command(
    state: CommandState, graph_ref: str, source: str, module: str | None, data_root: str | None
) -> Result:
    ctx = admin_context(state, data_root)
    outcome = release(ctx, graph_ref=graph_ref, source=Path(source), module=module)
    return Result(
        data=outcome.data,
        graph=graphs.envelope_graph(outcome.entry, outcome.seq),
        warnings=outcome.warnings,
    )


SPECS: Final = (
    CommandSpec(
        path=("ontology", "validate"),
        kind="read",
        surface="model",
        action=None,
        summary="Check an authored ontology; error findings fail with admin.model_invalid.",
        params=(SOURCE, MODULE),
        examples=(
            Example("graph-admin ontology validate <dir>", "Check an authored ontology."),
            Example(
                "graph-admin ontology validate <dir> --module <module> --json",
                "Check one module of several, as JSON.",
            ),
        ),
        errors=("admin.model_invalid",),
        handler=validate,
        remote="client",
    ),
    CommandSpec(
        path=("ontology", "diff"),
        kind="read",
        surface="model",
        action=None,
        summary="Classify the changes from an older version to SOURCE (patch, minor, major).",
        params=(
            SOURCE,
            MODULE,
            ParamSpec("--against", "directory", "Older authored version to compare with."),
            ParamSpec(
                "--graph",
                "graph_ref",
                "Compare with the release this graph is pinned to.",
                keyword="graph_ref",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin ontology diff <dir> --against <dir>", "Compare two versions."),
            Example(
                "graph-admin ontology diff <dir> --graph <graph> --json",
                "Compare with a graph's pinned release.",
            ),
        ),
        errors=(*GRAPH_ERRORS, "access.denied", "usage.missing_argument", "admin.model_invalid"),
        handler=diff_command,
        description="Reports whether the declared version bump suffices; release enforces it."
        " With --graph, authorized maint.graph_configure on the graph.",
        remote="hooked",
    ),
    CommandSpec(
        path=("ontology", "release"),
        kind="write",
        surface="act",
        action="maint.ontology_release",
        summary="Publish SOURCE as a release of GRAPH and pin the graph to it.",
        params=(
            ParamSpec(
                "graph",
                "graph_ref",
                "Graph name or id.",
                required=True,
                positional=True,
                keyword="graph_ref",
            ),
            SOURCE,
            MODULE,
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin ontology release <graph> <dir>", "Release and pin."),
            Example(
                "graph-admin ontology release <graph> <dir> --module <module> --json",
                "Release one module of several, as JSON.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            *CHANGE_ERRORS,
            "graph.archived",
            "admin.model_invalid",
            "admin.version_bump_insufficient",
            "admin.release_exists",
            "admin.migration_required",
        ),
        handler=release_command,
        description="The version bump must cover the changes against the pinned release;"
        " re-running a release that is already pinned changes nothing.",
        remote="hooked",
    ),
)
