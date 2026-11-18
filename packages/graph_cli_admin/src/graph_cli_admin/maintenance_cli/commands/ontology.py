"""``graph-admin ontology validate|diff|release``."""

from __future__ import annotations

from pathlib import Path
from typing import Final, get_args

from graph_cli.access_control.terms import rule_entries
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import STRING, array, enum, obj, ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.diff import BumpCheck, derive_renames
from graph_cli.ontology_layer.migration import parse_migration
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    MIGRATION_ERRORS,
    admin_context,
)
from graph_cli_admin.maintenance_cli.output_schemas import (
    DIFF,
    REFERENCES,
    RELEASE,
    RELEASE_OPTIONAL,
    STRINGS,
)
from graph_cli_admin.services_admin import graphs, marking_names, ontology, renamed_terms
from graph_cli_admin.services_admin.migration import has_renames
from graph_cli_admin.services_admin.release import (
    catalog_affected,
    follow_published,
    policies_affected,
    release,
    republished_json,
)

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
    affected: tuple[WarningRecord, ...] = ()
    graph_data: dict[str, JsonValue] = {}
    if graph_ref is not None:
        ctx = admin_context(state, data_root)
        entry = ctx.resolve(graph_ref, "ontology diff")
        ctx.authorize(
            [("maint.graph_configure", f"graph:{entry.id}")],
            request=RequestMeta.of("ontology diff", {"graph": graph_ref}),
            graph=entry.id,
        )
        old = ontology.pinned_model(ctx.catalog, entry)
        graph = graphs.envelope_graph(entry)
        functions = ctx.catalog.read_functions(entry.id, sources=True)
        actions = ctx.catalog.read_actions(entry.id, sources=True)
        views = ctx.catalog.read_views(entry.id, sources=True)
        renames = derive_renames(old, new.model)
        migration = ontology.read_migration(Path(source))
        # what the release would do with the catalogs: publish them again (renames or a
        # migration file) or leave them broken (plan M8)
        recompile = migration is not None or has_renames(renames)
        followed_functions, followed_actions, followed_views = follow_published(
            functions, actions, views, new.model, new.pin, recompile=recompile
        )
        version = new.model.bundle_version
        entries = rule_entries(ctx.policy.current(entry.id).documents)
        hazards = renamed_terms.hazards(entries, entry.id, renames, (), new.model)
        affected = (
            *catalog_affected("functions", followed_functions.problems, version),
            *catalog_affected("actions", followed_actions.problems, version),
            *catalog_affected("views", followed_views.problems, version),
            *policies_affected(ctx.policy.current(entry.id), entry, new.model, actions),
            *renamed_terms.warning(hazards, version),
        )
        if new.pin != entry.pin:
            # a migration file that does not parse is the release's to refuse; its retypes
            # count here when it does
            doc = None if migration is None else parse_migration(migration).doc
            hop = Hop(entry.pin.version, version, old, new.model, renames, doc)
            preview: dict[str, JsonValue] = {"to": version, "pinned": False}
            affected += marking_names.check(ctx, entry, hop, preview)
        republished = republished_json(followed_functions, followed_actions, followed_views)
        if republished:
            graph_data["republished"] = republished
        references = renamed_terms.references(
            entries, entry.id, renames, new.model, functions=functions, actions=actions, views=views
        )
        if references:
            graph_data["references"] = references
    else:
        assert against is not None
        old = ontology.compile_source(Path(against), module).model
    report, bump = ontology.diff(old, new.model)
    renames_warnings = ontology.rename_warnings(old, new.model)  # refuses an invalid renamed_from
    return Result(
        data={**report.to_json(), "bump": bump, **graph_data},
        graph=graph,
        warnings=(
            *ontology.module_bump_warnings(old, new.model, report),
            *renames_warnings,
            *affected,
        ),
    )


def release_command(
    state: CommandState,
    graph_ref: str,
    source: str,
    module: str | None,
    reason: str | None,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    outcome = release(
        ctx,
        graph_ref=graph_ref,
        source=Path(source),
        module=module,
        reason=reason,
        dry_run=dry_run,
    )
    return Result(
        data=outcome.data,
        graph=graphs.envelope_graph(outcome.entry, outcome.seq),
        warnings=outcome.warnings,
    )


VALIDATE_OUTPUT: Final = obj(  # services_admin.ontology.summary
    {
        "bundle": STRING,
        "version": STRING,
        "artifact_hash": STRING,
        "modules": STRINGS,
        "findings": array(ref("finding")),
    }
)
DIFF_OUTPUT: Final = obj(
    {**DIFF, "bump": enum(*get_args(BumpCheck))},
    {"republished": ref("republished"), "references": REFERENCES},  # only with --graph
)
RELEASE_OUTPUT: Final = obj({"graph_id": STRING, **RELEASE}, RELEASE_OPTIONAL)


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
        output=VALIDATE_OUTPUT,
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
                "Compare with the release this graph is pinned to; warns"
                " (admin.functions_affected, admin.actions_affected, admin.views_affected) about"
                " published functions, actions and views the release would break.",
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
        errors=(
            *GRAPH_ERRORS,
            "access.denied",
            "usage.missing_argument",
            "admin.model_invalid",
            "ontology.rename_invalid",
            "ontology.marking_name_reused",
        ),
        handler=diff_command,
        output=DIFF_OUTPUT,
        description="Reports whether the declared version bump suffices; release enforces it."
        " A renamed_from that names nothing, whose source still exists, whose target existed"
        " before or that two terms share is ontology.rename_invalid."
        " With --graph, authorized maint.graph_configure on the graph; published functions and"
        " actions and views the release would break and deny rules it would fail open are"
        " warnings, and the answer lists where renamed terms are named (references) and the"
        " catalogs the release"
        " would publish again (republished); a reused name of a marked attribute is"
        " ontology.marking_name_reused, as for the release.",
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
            ParamSpec("--reason", "string", "Why it is released; recorded in every audit event."),
            ParamSpec(
                "--dry-run",
                "flag",
                "Run every check and show the release and its migration without writing.",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin ontology release <graph> <dir>", "Release and pin."),
            Example(
                "graph-admin ontology release <graph> <dir> --dry-run --json",
                "Show what the release and its migration would change.",
            ),
            Example(
                "graph-admin ontology release <graph> <dir> --reason <reason>",
                "Release with a reason (recorded with the migration).",
            ),
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
            "ontology.rename_invalid",
            "ontology.marking_name_reused",
            "admin.version_bump_insufficient",
            "admin.release_exists",
            "admin.migration_required",
            *MIGRATION_ERRORS,
            "admin.functions_broken",
            "admin.actions_broken",
            "admin.views_broken",
        ),
        handler=release_command,
        output=RELEASE_OUTPUT,
        description="The version bump must cover the changes against the pinned release, and"
        " every published function, action and view must still resolve under the new one (any"
        " bump: admin.functions_broken, admin.actions_broken, admin.views_broken; fix it with"
        " graph push; a release with"
        " renames or a migration file publishes the catalogs again by itself when their sources"
        " still compile against the new version); a deny rule of the installed policy that"
        " names a renamed term and no new one is admin.policy_renamed_term; a release that"
        " would show values stored under a marked attribute under a name declared without that"
        " marking is ontology.marking_name_reused (a removed marking warns"
        " admin.marking_removed); re-running a"
        " release that is already pinned changes nothing. A major release may carry a data"
        " migration, the file migration.yaml in SOURCE: it is part of the release (the same"
        " version with another file is admin.release_exists), needs maint.migrate (maintainers"
        " only), and rewrites the stored data in place in one transaction under the graph lock,"
        " before the pin switches; the data must fit the new version after it"
        " (admin.migration_required). The file is required whenever the release rewrites data,"
        " renames included. An interrupted release is finished by running it again"
        " (admin.migration_incomplete for any other release meanwhile). --dry-run makes every"
        " check and shows the migration (counts per rename and step, samples, findings) without"
        " writing; --reason is recorded with the release and the migration.",
        remote="hooked",
    ),
)
